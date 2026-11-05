"""SQLite 连接工厂：WAL / PRAGMA / 建表 / 轻量迁移 / 事务上下文。

约定（对应架构文档 §7.6 / §7.10）：
    * 只有 ``storage/repository.py`` 写 SQL，其他层一律调用 Service → Repository；
    * ``sqlite3`` 同步调用在 asyncio 场景下由调用方用 ``run_in_executor`` 桥接；
    * schema 版本用 ``PRAGMA user_version``，避免新增元数据表。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from site_group_monitor.utils.errors import StorageError

SCHEMA_VERSION = 6


class Database:
    """SQLite 数据库管理对象。

    Args:
        db_path: 数据库文件路径（自动创建父目录）。
        wal: 是否开启 WAL 日志模式。
        busy_timeout_ms: 锁等待毫秒数。
    """

    def __init__(self, db_path: str | Path, *, wal: bool = True, busy_timeout_ms: int = 5000) -> None:
        self.db_path = Path(db_path)
        self.wal = wal
        self.busy_timeout_ms = busy_timeout_ms
        self._lock = threading.Lock()
        self._wal_ready = False
        # 连接池：Windows 上 WAL 库每次 close() 都会触发检查点 + fsync，
        # 实测单次 ~100ms；原先「每次读写都 connect/close」导致 500 站导入要 100 秒以上。
        # 这里复用连接，只在真正需要时才新建。内存库必须共用同一连接（否则每次都是新库）。
        self._is_memory = str(self.db_path) == ":memory:"
        # 按线程池化：sqlite3 连接默认禁止跨线程使用，因此每个线程各自持有若干连接。
        # key = threading.get_ident()，value = 该线程的空闲连接栈。
        self._pool: dict[int, list[sqlite3.Connection]] = {}
        self._pool_lock = threading.Lock()
        # 内存库专用单连接（含独占锁）
        self._mem_conn: sqlite3.Connection | None = None
        self._mem_busy = False
        self._mem_cond = threading.Condition()
        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 连接
    def connect(self) -> sqlite3.Connection:
        """创建并完成 PRAGMA 初始化的新连接。

        Returns:
            ``sqlite3.Connection``，``row_factory`` 已设为 ``sqlite3.Row``。
        """
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=max(1.0, self.busy_timeout_ms / 1000.0),
            isolation_level=None,  # 手动控制事务
            detect_types=0,
        )
        conn.row_factory = sqlite3.Row
        # foreign_keys / busy_timeout 是「每连接」级 PRAGMA，必须每次设置。
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        # journal_mode 是「数据库文件持久化」属性，只需设置一次。
        # 每次连接都执行 PRAGMA journal_mode=WAL 要拿排他锁做一次 WAL 切换检查，
        # 实测单次 ~13ms；批量导入时每行 3 次连接 = 3 次 PRAGMA，500 行就要 20s+。
        if self.wal and str(self.db_path) != ":memory:":
            if not self._wal_ready:
                conn.execute("PRAGMA journal_mode = WAL")
                self._wal_ready = True
            conn.execute("PRAGMA synchronous = NORMAL")
        else:
            conn.execute("PRAGMA journal_mode = MEMORY")
        return conn

    def _acquire(self) -> sqlite3.Connection:
        """从连接池取一个连接（无空闲则新建）。

        Returns:
            已配置好 PRAGMA 的连接。
        """
        # 内存库每次 connect 都会得到一个全新的空库，因此必须复用同一条底层连接。
        # 但 SQLite 一条连接不能同时开两个事务，所以用条件变量保证「同一时刻只借给一个人」。
        if self._is_memory:
            self._mem_cond.acquire()
            while self._mem_busy:
                self._mem_cond.wait()
            if self._mem_conn is None:
                self._mem_conn = self.connect()
            self._mem_busy = True
            self._mem_cond.release()
            return self._mem_conn
        with self._pool_lock:
            stack = self._pool.get(threading.get_ident())
            if stack:
                return stack.pop()
        return self.connect()

    def _release(self, conn: sqlite3.Connection) -> None:
        """归还连接（不关闭，避免 WAL 检查点开销）。

        连接只能还回**创建它的线程**的池里，否则下次被别的线程取走会触发
        ``SQLite objects created in a thread can only be used in that same thread``。

        Args:
            conn: 使用完毕的连接。
        """
        if self._is_memory:
            self._mem_cond.acquire()
            self._mem_busy = False
            self._mem_cond.notify()
            self._mem_cond.release()
            return
        with self._pool_lock:
            stack = self._pool.setdefault(threading.get_ident(), [])
            # 单线程池上限保护，避免长跑进程无限持有连接
            if len(stack) < 4:
                stack.append(conn)
                return
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover
            pass

    def close_all(self) -> None:
        """关闭池内全部连接（进程退出 / 测试清理时调用）。"""
        with self._pool_lock:
            pools, self._pool = self._pool, {}
        for stack in pools.values():
            for conn in stack:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover
                    pass
        if self._mem_conn is not None:
            try:
                self._mem_conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass
            self._mem_conn = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """写事务上下文（失败自动回滚）。

        Yields:
            已开启 ``BEGIN IMMEDIATE`` 的连接。
        """
        conn = self._acquire()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - 统一包装为 StorageError
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:  # pragma: no cover
                pass
            raise StorageError(f"写事务失败: {exc}", path=str(self.db_path)) from exc
        finally:
            self._release(conn)

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """只读连接上下文。

        Yields:
            只读用途的连接（调用方无需提交）。
        """
        conn = self._acquire()
        try:
            yield conn
        finally:
            self._release(conn)

    # ------------------------------------------------------------------ 建库
    def _schema_sql(self) -> str:
        """读取随包的 schema.sql（兼容 wheels 与源码目录两种形态）。"""
        candidates = [
            Path(__file__).resolve().parent / "schema.sql",
            Path(__file__).resolve().parents[2] / "site_group_monitor" / "storage" / "schema.sql",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        raise StorageError("找不到 schema.sql，请检查安装完整性")

    def init_db(self) -> dict[str, Any]:
        """初始化数据库（幂等），返回简要信息。

        注意：``executescript()`` 会隐式提交并结束当前事务，因此**不能**包在
        :meth:`tx` 里（否则随后的 ``COMMIT`` 会报 "no transaction is active"）。
        这里使用独立连接执行，DDL 全部为 ``IF NOT EXISTS``，天然幂等。

        Returns:
            ``{"tables": [...], "schema_version": int}``。
        """
        with self._lock:
            with self.read() as conn:
                conn.executescript(self._schema_sql())
        # executescript 的 CREATE TABLE IF NOT EXISTS 不会给已有 proxies 表补新列，
        # 这里显式补齐 source_ref（v3->v4 的列变更），对全新库为 no-op。
        self._add_column_if_missing("proxies", "source_ref", "TEXT NOT NULL DEFAULT ''")
        self._add_column_if_missing("proxies", "last_egress_ip", "TEXT NOT NULL DEFAULT ''")
        self._add_column_if_missing("proxies", "last_location", "TEXT NOT NULL DEFAULT ''")
        self._add_column_if_missing("proxies", "last_latency_ms", "INTEGER")
        self._add_column_if_missing("proxies", "last_probe_at", "TEXT")
        self._add_column_if_missing("proxies", "last_probe_error", "TEXT NOT NULL DEFAULT ''")
        self._add_column_if_missing(
            "proxy_api_sources", "rotate_per_check", "INTEGER NOT NULL DEFAULT 0"
        )
        # 建库即视作已是最新版：否则后续每次 migrate() 都会重跑
        # v1->v2 的重复数据清理，纯属白做功。
        if self.schema_version() < SCHEMA_VERSION:
            self.set_schema_version(SCHEMA_VERSION)
        return {"tables": self.list_tables(), "schema_version": self.schema_version()}

    def list_tables(self) -> list[str]:
        """列出当前库中的用户表名。

        Returns:
            表名列表。
        """
        with self.read() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return [str(row["name"]) for row in rows]

    def schema_version(self) -> int:
        """读取 ``PRAGMA user_version``。

        Returns:
            版本号整数。
        """
        with self.read() as conn:
            row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def set_schema_version(self, version: int = SCHEMA_VERSION) -> None:
        """写入 schema 版本号（迁移后调用）。

        Args:
            version: 目标版本号。
        """
        with self.read() as conn:
            conn.execute(f"PRAGMA user_version = {int(version)}")

    def migrate(self) -> list[str]:
        """轻量迁移：按 ``user_version`` 逐级补齐缺失表/列。

        Returns:
            已执行的迁移步骤描述列表。
        """
        steps: list[str] = []
        current = self.schema_version()
        if current >= SCHEMA_VERSION:
            return steps
        # v1 -> v2：change_events 加唯一键前，先清掉历史重复行。
        # 旧版本没有 ux_ce_site_type_date，同一天重跑 diff 会留下重复事件；
        # 不先清理，CREATE UNIQUE INDEX 会直接报 UNIQUE constraint failed。
        if current < 2:
            removed = self._dedupe_change_events()
            if removed:
                steps.append(f"清理 change_events 重复事件 {removed} 条")
        # v2 -> v3：check_results 加 index_http_status 列（百度检索页状态码，
        # 与站点健康字段 http_status 分离，避免污染）。executescript 的
        # CREATE TABLE IF NOT EXISTS 不会给已有表加新列，必须显式 ALTER。
        if current < 3:
            added = self._add_column_if_missing(
                "check_results", "index_http_status", "INTEGER"
            )
            if added:
                steps.append("check_results 增加 index_http_status 列")
        # v3 -> v4：proxies 增加 source_ref 列（区分 API 源的代理归属）。
        # 同时补建 proxy_api_sources 表（executescript 的 CREATE TABLE IF NOT EXISTS
        # 会补齐缺失的新表，但列需用 ALTER 显式补齐）。
        if current < 4:
            added = self._add_column_if_missing(
                "proxies", "source_ref", "TEXT NOT NULL DEFAULT ''"
            )
            if added:
                steps.append("proxies 增加 source_ref 列")
        # v4 -> v5：代理探测字段（出口 IP / 地理位置 / 延迟）。
        if current < 5:
            probe_cols = [
                ("last_egress_ip", "TEXT NOT NULL DEFAULT ''"),
                ("last_location", "TEXT NOT NULL DEFAULT ''"),
                ("last_latency_ms", "INTEGER"),
                ("last_probe_at", "TEXT"),
                ("last_probe_error", "TEXT NOT NULL DEFAULT ''"),
            ]
            added_any = False
            for col, decl in probe_cols:
                if self._add_column_if_missing("proxies", col, decl):
                    added_any = True
            if added_any:
                steps.append("proxies 增加出口 IP / 位置 / 延迟列")
        # v5 -> v6：API 源每次检测换新 IP。
        if current < 6:
            added = self._add_column_if_missing(
                "proxy_api_sources", "rotate_per_check", "INTEGER NOT NULL DEFAULT 0"
            )
            if added:
                steps.append("proxy_api_sources 增加 rotate_per_check 列")
        # 跑一遍完整 DDL（幂等），再把版本号写入
        # 同 init_db：executescript 自带提交语义，不套 tx()
        with self._lock:
            with self.read() as conn:
                conn.executescript(self._schema_sql())
        if self.schema_version() < SCHEMA_VERSION:
            self.set_schema_version(SCHEMA_VERSION)
        steps.append(f"user_version {current} -> {SCHEMA_VERSION}")
        return steps

    def _add_column_if_missing(
        self, table: str, column: str, decl: str
    ) -> bool:
        """幂等地给已有表追加一列（ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS）。

        Args:
            table: 表名。
            column: 列名。
            decl: 列定义（如 ``INTEGER``）。

        Returns:
            本次是否执行了 ALTER（已存在则返回 False）。
        """
        with self._lock:
            with self.read() as conn:
                cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column in cols:
                    return False
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                return True

    def _dedupe_change_events(self) -> int:
        """删除 ``change_events`` 中 (site_id, event_type, stat_date) 的重复行。

        保留每组的 ``MIN(id)``（最早一条），删除其余。

        Returns:
            删除的行数；表不存在时返回 0。
        """
        try:
            with self.db_tx_for_migration() as conn:
                cursor = conn.execute(
                    "DELETE FROM change_events WHERE id NOT IN ("
                    "  SELECT MIN(id) FROM change_events GROUP BY site_id, event_type, stat_date"
                    ")"
                )
                return int(cursor.rowcount or 0)
        except sqlite3.OperationalError:
            return 0

    @contextmanager
    def db_tx_for_migration(self):
        """迁移专用写事务（``change_events`` 可能尚不存在）。

        Yields:
            已开启 ``BEGIN IMMEDIATE`` 的连接。
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:  # noqa: BLE001 - 迁移失败静默回滚
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ 运维
    def backup_to(self, target: str | Path) -> Path:
        """在线热备份（``VACUUM INTO``）。

        Args:
            target: 目标文件路径。

        Returns:
            备份文件绝对路径。
        """
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            target_path.unlink()
        with self.read() as conn:
            try:
                conn.execute("VACUUM INTO ?", (str(target_path),))
            except sqlite3.Error as exc:  # pragma: no cover - 老版本 SQLite 兜底
                raise StorageError(f"备份失败: {exc}", path=str(target_path)) from exc
        return target_path

    def vacuum(self) -> None:
        """整理空间（报告后调用，控制库体积）。"""
        with self.read() as conn:
            conn.execute("VACUUM")

    def stats(self) -> dict[str, Any]:
        """返回库级统计信息（体积/行数/日志模式）。

        Returns:
            统计字典。
        """
        info: dict[str, Any] = {
            "path": str(self.db_path),
            "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0,
            "tables": {},
        }
        with self.read() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()
            info["journal_mode"] = str(mode[0]) if mode else "unknown"
            for table in self.list_tables():
                row = conn.execute(f"SELECT COUNT(*) AS cnt FROM {table}").fetchone()
                info["tables"][table] = int(row["cnt"]) if row else 0
        return info


def default_database(project_root: str | Path | None = None) -> Database:
    """按当前配置创建默认 :class:`Database`。

    Args:
        project_root: 预留参数，便于未来多实例；当前使用 settings 中的路径。

    Returns:
        :class:`Database` 实例（未自动建表，需调用 ``init_db``）。
    """
    from site_group_monitor.config.settings import get_settings

    settings = get_settings()
    return Database(settings.db_path, wal=settings.storage.wal,
                    busy_timeout_ms=settings.storage.busy_timeout_ms)
