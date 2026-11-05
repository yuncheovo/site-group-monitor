"""Repository：全站唯一的 SQL 出口（对应架构文档 §7.6）。

所有 SQL 都收敛在本文件的 7 个仓储类里；Service / Web / CLI 一律通过 Service 调用这里。
对外返回 :mod:`site_group_monitor.models.entities` 中定义的实体，不把 sqlite3.Row 泄漏到上层。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Sequence

from site_group_monitor.models.entities import (
    ChangeEvent,
    CheckResult,
    DailySnapshot,
    ProxyApiSource,
    ProxyEndpoint,
    Site,
)
from site_group_monitor.storage.database import Database
from site_group_monitor.utils.timeutil import utcnow_iso

# 站点列表允许的排序字段 -> SQL 表达式（白名单，防 SQL 注入）
_SITE_SORT_MAP: dict[str, str] = {
    "domain": "s.domain ASC",
    "group_name": "s.group_name ASC, s.domain ASC",
    "index_status": "cr.index_status IS NULL, cr.index_status ASC, s.domain ASC",
    "http_status": "cr.http_status IS NULL, cr.http_status DESC, s.domain ASC",
    "response_ms": "cr.response_ms IS NULL, cr.response_ms DESC, s.domain ASC",
    "ssl_days_left": "cr.ssl_days_left IS NULL, cr.ssl_days_left ASC, s.domain ASC",
    "last_checked_at": "s.last_checked_at IS NULL, s.last_checked_at DESC, s.domain ASC",
    "created_at": "s.created_at DESC, s.domain ASC",
}


def _health_condition(health: str, ssl_warn_days: int = 15) -> str:
    """构造健康度过滤的 SQL 表达式（与 CheckResult.health 的判定口径保持一致）。

    Args:
        health: ``healthy`` / ``warn`` / ``abnormal`` / ``unknown``。
        ssl_warn_days: SSL 剩余天数预警阈值。

    Returns:
        可直接拼到 WHERE 中的布尔表达式字符串。
    """
    abnormal = (
        "(cr.http_status IS NOT NULL AND cr.http_status >= 400) OR "
        "COALESCE(json_extract(cr.anomalies, '$.redirect_anomaly'), 0) = 1"
    )
    unknown = (
        "COALESCE(cr.index_status, 'unknown_need_manual_check') = 'unknown_need_manual_check' OR "
        "COALESCE(cr.error_type, 'none') NOT IN ('none', '')"
    )
    not_warn = (
        f"NOT ({abnormal}) AND NOT ({unknown}) AND ("
        "COALESCE(json_extract(cr.anomalies, '$.mixed_content'), 0) = 1 OR "
        "COALESCE(cr.response_ms, 0) > 3000)"
    )
    mapping = {
        "abnormal": abnormal,
        "unknown": unknown,
        "warn": not_warn,
        "healthy": f"NOT ({abnormal}) AND NOT ({unknown}) AND NOT ({not_warn})",
    }
    return f"({mapping.get(health, unknown)})"


class BaseRepository:
    """仓储基类，持有 :class:`Database` 并统一行转换。"""

    def __init__(self, db: Database) -> None:
        """构造仓储。

        Args:
            db: 数据库管理对象。
        """
        self.db = db

    @staticmethod
    def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        """``sqlite3.Row`` 迭代器转为普通字典列表。

        Args:
            rows: 查询结果。

        Returns:
            字典列表。
        """
        return [dict(row) for row in rows]


# ===========================================================================
# 1) 站点
# ===========================================================================
class SiteRepository(BaseRepository):
    """站点主表仓储。"""

    def insert(self, site: Site) -> int:
        """插入新站点。

        Args:
            site: 站点实体。

        Returns:
            新记录 id。
        """
        row = site.to_row()
        row.setdefault("created_at", utcnow_iso())
        row.setdefault("updated_at", utcnow_iso())
        columns = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        with self.db.tx() as conn:
            cursor = conn.execute(f"INSERT INTO sites ({columns}) VALUES ({placeholders})", list(row.values()))
            return int(cursor.lastrowid or 0)

    def upsert_by_domain(self, site: Site) -> int:
        """按域名幂等写入（存在则更新元信息，不覆盖检测相关字段）。

        Args:
            site: 站点实体（``domain`` 已归一化）。

        Returns:
            站点 id。
        """
        existing = self.get_by_domain(site.domain)
        now = utcnow_iso()
        if existing is None:
            # 注意：不能写成 insert(Site(**{**site.model_dump(), **payload}))——
            # to_row() 已把 tags 序列化成 JSON 字符串，再塞回实体会被 pydantic 判为
            # "Input should be a valid list"。这里直接按行写入。
            row = site.to_row()
            row["created_at"] = site.created_at or now
            row["updated_at"] = now
            columns = ", ".join(row.keys())
            placeholders = ", ".join(["?"] * len(row))
            with self.db.tx() as conn:
                cursor = conn.execute(
                    f"INSERT INTO sites ({columns}) VALUES ({placeholders})", list(row.values())
                )
                return int(cursor.lastrowid or 0)
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE sites SET root_url=?, name=?, group_name=?, tags=?, notes=?, enabled=?, updated_at=? "
                "WHERE id=?",
                (
                    site.root_url or existing.root_url,
                    site.name or existing.name,
                    site.group_name or existing.group_name,
                    json.dumps(site.tags or existing.tags, ensure_ascii=False),
                    site.notes or existing.notes,
                    int(site.enabled),
                    now,
                    existing.id,
                ),
            )
        return int(existing.id or 0)

    def bulk_upsert_by_domain(self, sites: Sequence[Site]) -> tuple[int, int]:
        """批量按域名幂等写入（单连接单事务，消灭 N+1）。

        语义与 :meth:`upsert_by_domain` 完全一致（存在则只更新元信息，不覆盖
        检测相关字段），但把「查询存在性 + 写入」合并成一次 ``SELECT IN`` +
        一次 ``executemany``，批量导入 500 行由 ~60s 降到 <0.2s。

        Args:
            sites: 站点实体序列（``domain`` 已归一化且无重复）。

        Returns:
            ``(added, updated)`` 元组。
        """
        if not sites:
            return (0, 0)
        now = utcnow_iso()
        domains = [site.domain for site in sites]

        added = 0
        updated = 0
        with self.db.tx() as conn:
            existing_ids: dict[str, int] = {}
            # 分片查询，规避 SQLite 变量上限（默认 999）
            for start in range(0, len(domains), 400):
                chunk = domains[start : start + 400]
                placeholders = ", ".join(["?"] * len(chunk))
                rows = conn.execute(
                    f"SELECT id, domain FROM sites WHERE domain IN ({placeholders})", chunk
                ).fetchall()
                existing_ids.update({row["domain"]: int(row["id"]) for row in rows})

            insert_rows: list[list[Any]] = []
            update_rows: list[list[Any]] = []
            for site in sites:
                existing_id = existing_ids.get(site.domain)
                if existing_id is None:
                    row = site.to_row()
                    row["created_at"] = site.created_at or now
                    row["updated_at"] = now
                    insert_rows.append(list(row.values()))
                    added += 1
                else:
                    update_rows.append(
                        [
                            site.root_url,
                            site.name,
                            site.group_name,
                            json.dumps(site.tags, ensure_ascii=False),
                            site.notes,
                            int(site.enabled),
                            now,
                            existing_id,
                        ]
                    )
                    updated += 1

            if insert_rows:
                columns = [
                    "domain", "root_url", "name", "group_name", "tags", "enabled",
                    "baseline_done", "notes", "created_at", "updated_at", "last_checked_at",
                ]
                placeholders = ", ".join(["?"] * len(columns))
                conn.executemany(
                    f"INSERT INTO sites ({', '.join(columns)}) VALUES ({placeholders})",
                    insert_rows,
                )
            if update_rows:
                conn.executemany(
                    "UPDATE sites SET root_url=?, name=?, group_name=?, tags=?, notes=?, "
                    "enabled=?, updated_at=? WHERE id=?",
                    update_rows,
                )
        return (added, updated)

    def update(self, site: Site) -> bool:
        """按 id 全量更新站点（含 root_url / tags / notes / enabled）。

        Args:
            site: 带 id 的站点实体。

        Returns:
            是否更新到记录。
        """
        if site.id is None:
            return False
        row = site.to_row()
        row["updated_at"] = utcnow_iso()
        assignments = ", ".join(f"{key}=?" for key in row)
        with self.db.tx() as conn:
            cursor = conn.execute(f"UPDATE sites SET {assignments} WHERE id=?", [*row.values(), site.id])
            return cursor.rowcount > 0

    def get_by_id(self, site_id: int) -> Site | None:
        """按 id 查询站点。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM sites WHERE id=?", (site_id,)).fetchone()
        return Site.from_row(dict(row)) if row else None

    def get_by_domain(self, domain: str) -> Site | None:
        """按域名查询站点。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM sites WHERE domain=?", (domain,)).fetchone()
        return Site.from_row(dict(row)) if row else None

    def exists(self, domain: str) -> bool:
        """判断域名是否已存在。"""
        return self.get_by_domain(domain) is not None

    def list_all(self) -> list[Site]:
        """返回全部站点（不分页，用于调度取任务列表）。"""
        with self.db.read() as conn:
            rows = conn.execute("SELECT * FROM sites ORDER BY domain ASC").fetchall()
        return [Site.from_row(dict(row)) for row in rows]

    def list_enabled(self, limit: int | None = None) -> list[Site]:
        """返回启用中的站点（按域名升序）。

        Args:
            limit: 可选条数上限。
        """
        sql = "SELECT * FROM sites WHERE enabled=1 ORDER BY domain ASC"
        params: tuple[Any, ...] = ()
        if limit:
            sql += " LIMIT ?"
            params = (int(limit),)
        with self.db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Site.from_row(dict(row)) for row in rows]

    def sample_incremental(self, ratio: float, seed_limit: int | None = None) -> list[Site]:
        """按比例抽样站点，用于白天增量健康抽检。

        Args:
            ratio: 抽样比例（0-1）。
            seed_limit: 可选的额外限制。

        Returns:
            抽中的站点列表。
        """
        ratio = min(1.0, max(0.0, float(ratio or 0.0)))
        if ratio <= 0:
            return []
        with self.db.read() as conn:
            total_row = conn.execute("SELECT COUNT(*) AS cnt FROM sites WHERE enabled=1").fetchone()
            total = int(total_row["cnt"]) if total_row else 0
            if total == 0:
                return []
            take = max(1, int(total * ratio))
            if seed_limit:
                take = min(take, int(seed_limit))
            rows = conn.execute(
                "SELECT * FROM sites WHERE enabled=1 "
                "ORDER BY COALESCE(last_checked_at, '') ASC, domain ASC LIMIT ?",
                (take,),
            ).fetchall()
        return [Site.from_row(dict(row)) for row in rows]

    def list(
        self,
        *,
        group: str | None = None,
        tag: str | None = None,
        keyword: str | None = None,
        enabled: bool | None = None,
        index_status: str | None = None,
        health: str | None = None,
        page: int = 1,
        size: int = 50,
        sort: str = "domain",
        desc: bool = False,
        ssl_warn_days: int = 15,
    ) -> tuple[list[Site], int]:
        """分页查询站点列表（左连最新检测结果，供仪表盘表格使用）。

        Args:
            group: 分组过滤。
            tag: 标签过滤（tags JSON 内含）。
            keyword: 域名/备注名模糊搜索。
            enabled: 是否只看启用。
            index_status: 收录三态过滤（含 latest）。
            health: 健康度过滤（healthy/warn/abnormal/unknown）。
            page: 页码（1 起）。
            size: 每页条数。
            sort: 排序字段名（白名单）。
            desc: 是否倒序。

        Returns:
            ``(站点列表, 总数)``。
        """
        where: list[str] = []
        params: list[Any] = []
        if group:
            where.append("s.group_name = ?")
            params.append(group)
        if tag:
            where.append("s.tags LIKE ?")
            params.append(f'%"{tag}"%')
        if keyword:
            where.append("(s.domain LIKE ? OR s.name LIKE ? OR s.notes LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
        if enabled is not None:
            where.append("s.enabled = ?")
            params.append(int(enabled))
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        latest_join = (
            " LEFT JOIN check_results cr ON cr.id = ("
            "   SELECT c2.id FROM check_results c2 WHERE c2.site_id = s.id "
            "   ORDER BY c2.checked_at DESC, c2.id DESC LIMIT 1)"
        )
        # 基础列 + 最新结果左连
        base_columns = (
            "SELECT s.*, cr.index_status AS index_status, cr.index_reason AS index_reason, "
            "cr.http_status AS http_status, cr.response_ms AS response_ms, "
            "cr.ssl_days_left AS ssl_days_left, cr.title AS title, cr.checked_at AS checked_at, "
            "cr.anomalies AS anomalies, cr.error_type AS error_type "
            "FROM sites s" + latest_join
        )
        # index_status / health 依赖最新检测结果（cr.*），因此必须放在 left join 之后的 WHERE 里，
        # 且 COUNT 查询必须使用**同一套**条件，否则总数与实际行数不一致。
        post_where: list[str] = []
        post_params: list[Any] = []
        if index_status:
            post_where.append("COALESCE(cr.index_status, 'unknown_need_manual_check') = ?")
            post_params.append(index_status)
        if health:
            post_where.append(_health_condition(health, ssl_warn_days))

        all_where = list(where)
        all_where.extend(post_where)
        all_params = [*params, *post_params]
        full_where_sql = (" WHERE " + " AND ".join(all_where)) if all_where else ""
        base_select = base_columns + full_where_sql

        order_by = _SITE_SORT_MAP.get(sort, _SITE_SORT_MAP["domain"])
        if desc:
            order_by = "s.domain DESC"

        with self.db.read() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM sites s" + latest_join + full_where_sql, all_params
            ).fetchone()
            total = int(total_row["cnt"]) if total_row else 0
            page = max(1, int(page))
            size = max(1, min(int(size), 1000))
            rows = conn.execute(
                f"{base_select} ORDER BY {order_by} LIMIT ? OFFSET ?",
                [*all_params, size, (page - 1) * size],
            ).fetchall()

        sites: list[Site] = []
        for row in rows:
            data = dict(row)
            for json_key in ("anomalies",):
                raw = data.pop(json_key, None)
                if raw:
                    try:
                        data[json_key] = json.loads(raw)
                    except json.JSONDecodeError:
                        data[json_key] = {}
            sites.append(Site.from_row(data))
        return sites, total

    def list_groups(self) -> list[dict[str, Any]]:
        """返回分组统计列表。

        Returns:
            ``[{"group_name": ..., "count": ...}, ...]``。
        """
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT group_name, COUNT(*) AS count FROM sites GROUP BY group_name ORDER BY group_name"
            ).fetchall()
        return [{"group_name": str(row["group_name"]), "count": int(row["count"])} for row in rows]

    def count_all(self) -> int:
        """站点总数。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM sites").fetchone()
        return int(row["cnt"]) if row else 0

    def count_enabled(self) -> int:
        """启用中的站点数。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM sites WHERE enabled=1").fetchone()
        return int(row["cnt"]) if row else 0

    def remove(self, identifier: str | int) -> bool:
        """按域名或 id 删除站点（级联清理明细/快照/事件）。

        Args:
            identifier: domain 或 id。

        Returns:
            是否删除成功。
        """
        column = "domain" if isinstance(identifier, str) and not identifier.isdigit() else "id"
        with self.db.tx() as conn:
            cursor = conn.execute(f"DELETE FROM sites WHERE {column}=?", (identifier,))
            return cursor.rowcount > 0

    def set_enabled(self, site_id: int, enabled: bool) -> bool:
        """启停站点。"""
        with self.db.tx() as conn:
            cursor = conn.execute(
                "UPDATE sites SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), utcnow_iso(), site_id),
            )
            return cursor.rowcount > 0

    def mark_baseline(self, site_id: int, done: bool = True) -> None:
        """标记站点是否已建立基线。"""
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE sites SET baseline_done=?, updated_at=? WHERE id=?",
                (int(done), utcnow_iso(), site_id),
            )

    def touch_checked(self, site_id: int, checked_at: str | None = None) -> None:
        """更新站点的最近检测时间。"""
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE sites SET last_checked_at=?, updated_at=? WHERE id=?",
                (checked_at or utcnow_iso(), utcnow_iso(), site_id),
            )


# ===========================================================================
# 2) 检测明细
# ===========================================================================
class CheckResultRepository(BaseRepository):
    """``check_results`` 表仓储。"""

    def insert(self, result: CheckResult) -> int:
        """写入一条检测明细。

        Args:
            result: 检测结果实体。

        Returns:
            新记录 id。
        """
        row = result.to_row()
        row.setdefault("checked_at", utcnow_iso())
        columns = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        with self.db.tx() as conn:
            cursor = conn.execute(
                f"INSERT INTO check_results ({columns}) VALUES ({placeholders})", list(row.values())
            )
            return int(cursor.lastrowid or 0)

    def insert_many(self, results: Sequence[CheckResult]) -> int:
        """批量写入检测明细（单事务）。

        Args:
            results: 结果列表。

        Returns:
            写入条数。
        """
        if not results:
            return 0
        rows = [item.to_row() for item in results]
        for row in rows:
            row.setdefault("checked_at", utcnow_iso())
        columns = ", ".join(rows[0].keys())
        placeholders = ", ".join(["?"] * len(rows[0]))
        with self.db.tx() as conn:
            conn.executemany(
                f"INSERT INTO check_results ({columns}) VALUES ({placeholders})",
                [list(row.values()) for row in rows],
            )
        return len(rows)

    def latest(self, site_id: int) -> CheckResult | None:
        """查询某站最近一条检测结果。"""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM check_results WHERE site_id=? ORDER BY checked_at DESC, id DESC LIMIT 1",
                (site_id,),
            ).fetchone()
        return CheckResult.from_row(dict(row)) if row else None

    def latest_map(self, site_ids: Sequence[int]) -> dict[int, CheckResult]:
        """批量取多个站点的最新结果。

        Args:
            site_ids: 站点 id 列表。

        Returns:
            ``{site_id: CheckResult}``。
        """
        if not site_ids:
            return {}
        placeholders = ",".join(["?"] * len(site_ids))
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT cr.* FROM check_results cr JOIN ("
                "  SELECT site_id, MAX(id) AS mid FROM check_results "
                f"  WHERE site_id IN ({placeholders}) GROUP BY site_id"
                ") latest ON latest.mid = cr.id",
                list(site_ids),
            ).fetchall()
        return {int(row["site_id"]): CheckResult.from_row(dict(row)) for row in rows}

    def latest_all(self) -> list[CheckResult]:
        """返回每个站点的最新一条结果（全站，用于快照生成）。"""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT cr.* FROM check_results cr JOIN ("
                "  SELECT site_id, MAX(id) AS mid FROM check_results GROUP BY site_id"
                ") latest ON latest.mid = cr.id"
            ).fetchall()
        return [CheckResult.from_row(dict(row)) for row in rows]

    def history(self, site_id: int, limit: int = 90) -> list[CheckResult]:
        """查询单站历史检测记录。

        Args:
            site_id: 站点 id。
            limit: 返回条数上限。

        Returns:
            按时间倒序的结果列表。
        """
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM check_results WHERE site_id=? ORDER BY checked_at DESC, id DESC LIMIT ?",
                (site_id, max(1, int(limit))),
            ).fetchall()
        return [CheckResult.from_row(dict(row)) for row in rows]

    def list_by_task(self, task_id: str) -> list[CheckResult]:
        """按任务 id 取本轮全部结果（diff 流程的输入）。"""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM check_results WHERE task_id=? ORDER BY id ASC", (task_id,)
            ).fetchall()
        return [CheckResult.from_row(dict(row)) for row in rows]

    def list_between(self, start_iso: str, end_iso: str) -> list[CheckResult]:
        """按 UTC ISO 时间区间查询结果（字符串比较即可，ISO8601 字典序=时间序）。"""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM check_results WHERE checked_at >= ? AND checked_at < ? ORDER BY id ASC",
                (start_iso, end_iso),
            ).fetchall()
        return [CheckResult.from_row(dict(row)) for row in rows]

    def delete_before(self, cutoff_iso: str) -> int:
        """清理过期明细（retention）。

        Args:
            cutoff_iso: 截止 UTC ISO 时间（早于该值的记录会被删除）。

        Returns:
            删除条数。
        """
        with self.db.tx() as conn:
            cursor = conn.execute("DELETE FROM check_results WHERE checked_at < ?", (cutoff_iso,))
            return cursor.rowcount


# ===========================================================================
# 3) 每日快照
# ===========================================================================
class SnapshotRepository(BaseRepository):
    """``daily_snapshots`` 表仓储（趋势与 diff 的唯一数据源）。"""

    def upsert(self, snapshot: DailySnapshot) -> None:
        """按 ``(site_id, stat_date)`` 幂等写入快照。

        Args:
            snapshot: 快照实体。
        """
        row = {
            "site_id": snapshot.site_id,
            "stat_date": snapshot.stat_date,
            "index_status": str(snapshot.index_status),
            "http_status": snapshot.http_status,
            "response_ms": snapshot.response_ms,
            "ssl_days_left": snapshot.ssl_days_left,
            "title": snapshot.title,
            "tdk_title_hash": snapshot.tdk_title_hash,
            "is_baseline": int(snapshot.is_baseline),
            "created_at": snapshot.created_at or utcnow_iso(),
        }
        columns = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        updates = ", ".join(f"{key}=excluded.{key}" for key in row if key not in ("site_id", "stat_date"))
        with self.db.tx() as conn:
            conn.execute(
                f"INSERT INTO daily_snapshots ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT(site_id, stat_date) DO UPDATE SET {updates}",
                list(row.values()),
            )

    def upsert_many(self, snapshots: Sequence[DailySnapshot]) -> int:
        """批量写入快照（单事务）。

        Args:
            snapshots: 快照列表。

        Returns:
            写入条数。
        """
        for snapshot in snapshots:
            self.upsert(snapshot)
        return len(snapshots)

    def get(self, site_id: int, stat_date: str) -> DailySnapshot | None:
        """取某站某天的快照。"""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM daily_snapshots WHERE site_id=? AND stat_date=?", (site_id, stat_date)
            ).fetchone()
        return DailySnapshot.from_row(dict(row)) if row else None

    def previous(self, site_id: int, stat_date: str, *, skip_baseline: bool = True) -> DailySnapshot | None:
        """取指定日期**之前**最近的一条快照。

        Args:
            site_id: 站点 id。
            stat_date: 基准日期键。
            skip_baseline: 是否跳过基线快照（基线不参与 diff）。

        Returns:
            快照实体或 None。
        """
        sql = "SELECT * FROM daily_snapshots WHERE site_id=? AND stat_date < ?"
        if skip_baseline:
            sql += " AND is_baseline=0"
        sql += " ORDER BY stat_date DESC LIMIT 1"
        with self.db.read() as conn:
            row = conn.execute(sql, (site_id, stat_date)).fetchone()
        return DailySnapshot.from_row(dict(row)) if row else None

    def has_history_before(self, site_id: int, stat_date: str) -> bool:
        """判断该站在指定日期之前是否存在过快照（决定是否为首站基线）。"""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM daily_snapshots WHERE site_id=? AND stat_date < ? LIMIT 1",
                (site_id, stat_date),
            ).fetchone()
        return row is not None

    def by_date(self, stat_date: str) -> list[DailySnapshot]:
        """取某天的全部快照。"""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_snapshots WHERE stat_date=? ORDER BY site_id ASC", (stat_date,)
            ).fetchall()
        return [DailySnapshot.from_row(dict(row)) for row in rows]

    def by_date_map(self, stat_date: str) -> dict[int, DailySnapshot]:
        """取某天快照的 ``{site_id: snapshot}`` 映射。"""
        return {int(item.site_id): item for item in self.by_date(stat_date)}

    def latest_date(self) -> str | None:
        """返回存在快照的最大日期键。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT MAX(stat_date) AS max_date FROM daily_snapshots").fetchone()
        return str(row["max_date"]) if row and row["max_date"] else None

    def trend(self, days: int = 30, end_date: str | None = None) -> list[dict[str, Any]]:
        """按日聚合趋势（命中 ``idx_ds_date`` / ``idx_ds_status``）。

        Args:
            days: 统计天数。
            end_date: 结束日期键（含），缺省为库中最大日期。

        Returns:
            ``[{"stat_date","total","indexed","not_indexed","unknown","rate"}, ...]`` 升序。
        """
        days = max(1, int(days))
        end = end_date or self.latest_date()
        start: str | None = None
        if end:
            from site_group_monitor.utils.timeutil import shift_days

            start = shift_days(end, -(days - 1))
        sql = (
            "SELECT stat_date, index_status, COUNT(*) AS cnt FROM daily_snapshots"
        )
        params: list[Any] = []
        if start and end:
            sql += " WHERE stat_date BETWEEN ? AND ?"
            params.extend([start, end])
        sql += " GROUP BY stat_date, index_status"
        counter: dict[str, dict[str, int]] = {}
        with self.db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        for row in rows:
            date_key = str(row["stat_date"])
            bucket = counter.setdefault(
                date_key, {"stat_date": date_key, "total": 0, "indexed": 0, "not_indexed": 0, "unknown": 0}
            )
            bucket["total"] += int(row["cnt"])
            status = str(row["index_status"])
            if status == "indexed":
                bucket["indexed"] += int(row["cnt"])
            elif status == "not_indexed":
                bucket["not_indexed"] += int(row["cnt"])
            else:
                bucket["unknown"] += int(row["cnt"])
        result: list[dict[str, Any]] = []
        for date_key in sorted(counter):
            bucket = counter[date_key]
            checkable = bucket["indexed"] + bucket["not_indexed"]
            rate = round(bucket["indexed"] / checkable, 4) if checkable else 0.0
            result.append({**bucket, "rate": rate})
        return result

    def count_unknown_streak(self, site_id: int, before_date: str, max_days: int = 7) -> int:
        """统计某站在某日期之前连续 unknown 的天数（含该日期之前的相邻天）。

        Args:
            site_id: 站点 id。
            before_date: 基准日期键（不含当日）。
            max_days: 最多回溯天数。

        Returns:
            连续 unknown 天数（含缺失天的容忍：允许跳过无快照的日期）。
        """
        from site_group_monitor.utils.timeutil import shift_days

        streak = 0
        cursor_date = before_date
        for _ in range(max(1, max_days)):
            cursor_date = shift_days(cursor_date, -1)
            snapshot = self.get(site_id, cursor_date)
            if snapshot is None:
                continue
            if str(snapshot.index_status) == "unknown_need_manual_check":
                streak += 1
            else:
                break
        return streak

    def delete_before(self, cutoff_date: str) -> int:
        """清理过期快照（retention）。

        Args:
            cutoff_date: 截止日期键（早于该日期的记录会被删除）。

        Returns:
            删除条数。
        """
        with self.db.tx() as conn:
            cursor = conn.execute("DELETE FROM daily_snapshots WHERE stat_date < ?", (cutoff_date,))
            return cursor.rowcount


# ===========================================================================
# 4) 代理池
# ===========================================================================
class ProxyRepository(BaseRepository):
    """``proxies`` 表仓储。"""

    def save(self, proxy: ProxyEndpoint) -> int:
        """按 ``(protocol, host, port)`` 幂等保存代理。

        Args:
            proxy: 代理实体。

        Returns:
            代理 id。
        """
        existing = self.find(proxy.protocol, proxy.host, proxy.port)
        row = proxy.to_row()
        if existing is None:
            row["created_at"] = proxy.created_at or utcnow_iso()
            columns = ", ".join(row.keys())
            placeholders = ", ".join(["?"] * len(row))
            with self.db.tx() as conn:
                cursor = conn.execute(
                    f"INSERT INTO proxies ({columns}) VALUES ({placeholders})", list(row.values())
                )
                return int(cursor.lastrowid or 0)
        assignments = ", ".join(f"{key}=?" for key in row)
        with self.db.tx() as conn:
            conn.execute(f"UPDATE proxies SET {assignments} WHERE id=?", [*row.values(), existing.id])
        return int(existing.id or 0)

    def find(self, protocol: str, host: str, port: int) -> ProxyEndpoint | None:
        """按三元组查找代理。"""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM proxies WHERE protocol=? AND host=? AND port=?", (protocol, host, int(port))
            ).fetchone()
        return ProxyEndpoint.from_row(dict(row)) if row else None

    def find_host_port(self, host: str, port: int) -> ProxyEndpoint | None:
        """按主机与端口查找（忽略协议，避免 http/https 重复入库）。"""
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM proxies WHERE host=? AND port=? ORDER BY id DESC",
                (host, int(port)),
            ).fetchone()
        return ProxyEndpoint.from_row(dict(row)) if row else None

    def get_by_id(self, proxy_id: int) -> ProxyEndpoint | None:
        """按 id 查找代理。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM proxies WHERE id=?", (proxy_id,)).fetchone()
        return ProxyEndpoint.from_row(dict(row)) if row else None

    def list_all(self, include_disabled: bool = True) -> list[ProxyEndpoint]:
        """列出全部代理。

        Args:
            include_disabled: 是否包含已禁用的。
        """
        sql = "SELECT * FROM proxies"
        if not include_disabled:
            sql += " WHERE disabled=0"
        sql += " ORDER BY health_score DESC, id ASC"
        with self.db.read() as conn:
            rows = conn.execute(sql).fetchall()
        return [ProxyEndpoint.from_row(dict(row)) for row in rows]

    def list_enabled(self) -> list[ProxyEndpoint]:
        """列出可用代理（未禁用且健康度过阈值过滤在上层做）。"""
        return self.list_all(include_disabled=False)

    def list_by_source(self, source: str, source_ref: str = "") -> list[ProxyEndpoint]:
        """按来源类型与来源引用列出代理。

        Args:
            source: 来源类型（manual/tunnel/batch/api）。
            source_ref: 来源引用（api 模式下为 api 源 id）。

        Returns:
            代理实体列表。
        """
        if source_ref:
            sql = "SELECT * FROM proxies WHERE source=? AND source_ref=? ORDER BY id ASC"
            params: list[Any] = [source, source_ref]
        else:
            sql = "SELECT * FROM proxies WHERE source=? ORDER BY id ASC"
            params = [source]
        with self.db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ProxyEndpoint.from_row(dict(row)) for row in rows]

    def remove(self, identifier: str | int) -> bool:
        """按 id 或 host 删除代理。"""
        column = "host" if isinstance(identifier, str) and not identifier.isdigit() else "id"
        with self.db.tx() as conn:
            cursor = conn.execute(f"DELETE FROM proxies WHERE {column}=?", (identifier,))
            return cursor.rowcount > 0

    def set_disabled(self, proxy_id: int, disabled: bool) -> bool:
        """启停代理。"""
        with self.db.tx() as conn:
            cursor = conn.execute("UPDATE proxies SET disabled=? WHERE id=?", (int(disabled), proxy_id))
            return cursor.rowcount > 0

    def save_probe(
        self,
        proxy_id: int,
        *,
        egress_ip: str,
        location: str,
        latency_ms: int | None,
        error: str,
    ) -> None:
        """回写一次出口探测结果（IP / 位置 / 延迟）。"""
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE proxies SET last_egress_ip=?, last_location=?, last_latency_ms=?, "
                "last_probe_at=?, last_probe_error=? WHERE id=?",
                (
                    egress_ip or "",
                    location or "",
                    latency_ms,
                    utcnow_iso(),
                    error or "",
                    proxy_id,
                ),
            )

    def save_state(self, proxy: ProxyEndpoint) -> None:
        """回写代理健康度与统计（由 ProxyPool 反馈调用）。"""
        if proxy.id is None:
            return
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE proxies SET health_score=?, success_count=?, fail_count=?, consecutive_fail=?, "
                "disabled=?, last_ok_at=?, last_fail_at=? WHERE id=?",
                (
                    proxy.health_score,
                    proxy.success_count,
                    proxy.fail_count,
                    proxy.consecutive_fail,
                    int(proxy.disabled),
                    proxy.last_ok_at,
                    proxy.last_fail_at,
                    proxy.id,
                ),
            )

    def revive_disabled(self, older_than_iso: str) -> int:
        """复活长期被禁用且最后一次失败时间早于给定时间的代理。

        Args:
            older_than_iso: 时间上界。

        Returns:
            复活条数。
        """
        with self.db.tx() as conn:
            cursor = conn.execute(
                "UPDATE proxies SET disabled=0, consecutive_fail=0, health_score=0.5 "
                "WHERE disabled=1 AND COALESCE(last_fail_at, '') <= ?",
                (older_than_iso,),
            )
            return cursor.rowcount

    def stats(self) -> dict[str, Any]:
        """代理池统计（供总览卡片「代理可用率」）。

        Returns:
            统计字典。
        """
        stats = {
            "total": 0,
            "enabled": 0,
            "disabled": 0,
            "healthy": 0,
            "avg_health": 0.0,
            "success_count": 0,
            "fail_count": 0,
            "empty": True,
        }
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN disabled=0 THEN 1 ELSE 0 END) AS enabled, "
                "SUM(CASE WHEN disabled=1 THEN 1 ELSE 0 END) AS disabled, "
                "SUM(CASE WHEN disabled=0 AND health_score >= 0.7 THEN 1 ELSE 0 END) AS healthy, "
                "AVG(health_score) AS avg_health, "
                "SUM(success_count) AS success_count, "
                "SUM(fail_count) AS fail_count "
                "FROM proxies"
            ).fetchone()
            if row:
                stats["total"] = int(row["total"] or 0)
                stats["enabled"] = int(row["enabled"] or 0)
                stats["disabled"] = int(row["disabled"] or 0)
                stats["healthy"] = int(row["healthy"] or 0)
                stats["avg_health"] = round(float(row["avg_health"] or 0.0), 3)
                stats["success_count"] = int(row["success_count"] or 0)
                stats["fail_count"] = int(row["fail_count"] or 0)
        stats["empty"] = stats["enabled"] == 0
        return stats


# ===========================================================================
# 4.5) 代理 API 源（API 动态获取）
# ===========================================================================
class ProxyApiSourceRepository(BaseRepository):
    """``proxy_api_sources`` 表仓储。"""

    def save(self, source: ProxyApiSource) -> int:
        """按 ``url`` 幂等保存 API 源（存在则更新，否则插入）。

        Args:
            source: API 源实体。

        Returns:
            源 id。
        """
        existing = self.get_by_url(source.url)
        row = source.to_row()
        if existing is None:
            row["created_at"] = source.created_at or utcnow_iso()
            columns = ", ".join(row.keys())
            placeholders = ", ".join(["?"] * len(row))
            with self.db.tx() as conn:
                cursor = conn.execute(
                    f"INSERT INTO proxy_api_sources ({columns}) VALUES ({placeholders})", list(row.values())
                )
                new_id = int(cursor.lastrowid or 0)
            source.id = new_id
            return new_id
        # 更新时保留原有 created_at
        row.pop("created_at", None)
        assignments = ", ".join(f"{key}=?" for key in row)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE proxy_api_sources SET {assignments} WHERE id=?", [*row.values(), existing.id]
            )
        source.id = int(existing.id or 0)
        return int(existing.id or 0)

    def get_by_url(self, url: str) -> ProxyApiSource | None:
        """按 url 查找 API 源。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM proxy_api_sources WHERE url=?", (url,)).fetchone()
        return ProxyApiSource.from_row(dict(row)) if row else None

    def get_by_id(self, source_id: int) -> ProxyApiSource | None:
        """按 id 查找 API 源。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM proxy_api_sources WHERE id=?", (source_id,)).fetchone()
        return ProxyApiSource.from_row(dict(row)) if row else None

    def list_all(self, include_disabled: bool = True) -> list[ProxyApiSource]:
        """列出全部 API 源。

        Args:
            include_disabled: 是否包含已禁用的。
        """
        sql = "SELECT * FROM proxy_api_sources"
        if not include_disabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id ASC"
        with self.db.read() as conn:
            rows = conn.execute(sql).fetchall()
        return [ProxyApiSource.from_row(dict(row)) for row in rows]

    def remove(self, source_id: int) -> bool:
        """按 id 删除 API 源。"""
        with self.db.tx() as conn:
            cursor = conn.execute("DELETE FROM proxy_api_sources WHERE id=?", (source_id,))
            return cursor.rowcount > 0

    def update_policy(self, source_id: int, rotate_per_check: bool) -> bool:
        """只更新 IP 切换策略，不覆盖账号、密码等源配置。"""
        with self.db.tx() as conn:
            cursor = conn.execute(
                "UPDATE proxy_api_sources SET rotate_per_check=? WHERE id=?",
                (int(bool(rotate_per_check)), source_id),
            )
            return cursor.rowcount > 0

    def update_fetch_result(self, source_id: int, fetched_count: int, error: str) -> None:
        """回写一次拉取结果。"""
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE proxy_api_sources SET last_fetch_at=?, last_fetch_count=?, last_error=? WHERE id=?",
                (utcnow_iso(), int(fetched_count), error, source_id),
            )


# ===========================================================================
# 5) 变化事件
# ===========================================================================
class ChangeEventRepository(BaseRepository):
    """``change_events`` 表仓储。"""

    def insert(self, event: ChangeEvent) -> int:
        """写入一条变化事件。

        Args:
            event: 事件实体。

        Returns:
            新记录 id。
        """
        row = event.to_row()
        row.setdefault("created_at", utcnow_iso())
        columns = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        with self.db.tx() as conn:
            cursor = conn.execute(
                f"INSERT INTO change_events ({columns}) VALUES ({placeholders})", list(row.values())
            )
            return int(cursor.lastrowid or 0)

    def insert_many(self, events: Sequence[ChangeEvent]) -> int:
        """批量写入事件（单事务，**幂等**）。

        依赖 ``ux_ce_site_type_date`` 唯一索引做 ``ON CONFLICT DO NOTHING``：
        同一 ``(site_id, event_type, stat_date)`` 已存在时跳过，不报错也不重复。
        这样「同一天重复跑 diff / 定时任务重试 / 手动补跑」都不会造成
        同一条告警被推送多次。

        Args:
            events: 事件列表。

        Returns:
            实际新写入的条数（被跳过的重复项不计入）。
        """
        if not events:
            return 0
        rows = [event.to_row() for event in events]
        for row in rows:
            row.setdefault("created_at", utcnow_iso())
        columns = ", ".join(rows[0].keys())
        placeholders = ", ".join(["?"] * len(rows[0]))
        with self.db.tx() as conn:
            before = conn.total_changes
            conn.executemany(
                f"INSERT INTO change_events ({columns}) VALUES ({placeholders}) "
                "ON CONFLICT(site_id, event_type, stat_date) DO NOTHING",
                [list(row.values()) for row in rows],
            )
            return int(conn.total_changes - before)

    def list_by_date(self, stat_date: str, event_type: str | None = None) -> list[ChangeEvent]:
        """查询某天的全部事件。

        Args:
            stat_date: 日期键。
            event_type: 可选事件类型过滤。
        """
        sql = (
            "SELECT ce.*, s.domain AS domain FROM change_events ce "
            "LEFT JOIN sites s ON s.id = ce.site_id WHERE ce.stat_date=?"
        )
        params: list[Any] = [stat_date]
        if event_type:
            sql += " AND ce.event_type=?"
            params.append(event_type)
        sql += " ORDER BY ce.id DESC"
        with self.db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [ChangeEvent.from_row(dict(row)) for row in rows]

    def list_recent(self, site_id: int, limit: int = 20) -> list[ChangeEvent]:
        """查询某站最近的事件（详情抽屉用）。"""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT ce.*, s.domain AS domain FROM change_events ce "
                "LEFT JOIN sites s ON s.id = ce.site_id WHERE ce.site_id=? "
                "ORDER BY ce.created_at DESC, ce.id DESC LIMIT ?",
                (site_id, max(1, int(limit))),
            ).fetchall()
        return [ChangeEvent.from_row(dict(row)) for row in rows]

    def exists_recent(
        self, site_id: int, event_type: str, stat_date: str, window_days: int = 1
    ) -> bool:
        """判断近期是否已有同类事件（告警去重冷却）。

        Args:
            site_id: 站点 id。
            event_type: 事件类型。
            stat_date: 基准日期键。
            window_days: 回溯天数窗口。

        Returns:
            存在返回 True。
        """
        from site_group_monitor.utils.timeutil import shift_days

        start = shift_days(stat_date, -(max(0, int(window_days))))
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM change_events WHERE site_id=? AND event_type=? "
                "AND stat_date >= ? AND stat_date <= ? LIMIT 1",
                (site_id, event_type, start, stat_date),
            ).fetchone()
        return row is not None

    def mark_notified(self, event_ids: Sequence[int]) -> int:
        """标记事件已通知。

        Args:
            event_ids: 事件 id 列表。

        Returns:
            更新条数。
        """
        if not event_ids:
            return 0
        placeholders = ",".join(["?"] * len(event_ids))
        with self.db.tx() as conn:
            cursor = conn.execute(
                f"UPDATE change_events SET notified=1 WHERE id IN ({placeholders})", list(event_ids)
            )
            return cursor.rowcount

    def unnotified(self, stat_date: str) -> list[ChangeEvent]:
        """取某天尚未通知的事件（邮件告警用）。"""
        return [item for item in self.list_by_date(stat_date) if not item.notified]

    def counts_by_type(self, stat_date: str) -> dict[str, int]:
        """按类型统计某天事件数量。"""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT event_type, COUNT(*) AS cnt FROM change_events WHERE stat_date=? GROUP BY event_type",
                (stat_date,),
            ).fetchall()
        return {str(row["event_type"]): int(row["cnt"]) for row in rows}

    def diff_trend(self, days: int = 30, end_date: str | None = None) -> list[dict[str, Any]]:
        """返回每日 new/lost 事件数量（diff 柱状图数据）。

        Args:
            days: 天数。
            end_date: 结束日期键。

        Returns:
            ``[{"stat_date","new_indexed","lost_indexed"}, ...]``。
        """
        days = max(1, int(days))
        if end_date is None:
            end_date = self._max_date()
        start: str | None = None
        if end_date:
            from site_group_monitor.utils.timeutil import shift_days

            start = shift_days(end_date, -(days - 1))
        sql = (
            "SELECT stat_date, event_type, COUNT(*) AS cnt FROM change_events "
            "WHERE event_type IN ('new_indexed','lost_indexed')"
        )
        params: list[Any] = []
        if start and end_date:
            sql += " AND stat_date BETWEEN ? AND ?"
            params.extend([start, end_date])
        sql += " GROUP BY stat_date, event_type"
        bucket: dict[str, dict[str, Any]] = {}
        with self.db.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        for row in rows:
            date_key = str(row["stat_date"])
            entry = bucket.setdefault(
                date_key, {"stat_date": date_key, "new_indexed": 0, "lost_indexed": 0}
            )
            entry[str(row["event_type"])] = int(row["cnt"])
        return [bucket[key] for key in sorted(bucket)]

    def _max_date(self) -> str | None:
        """库中最新事件日期。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT MAX(stat_date) AS max_date FROM change_events").fetchone()
        return str(row["max_date"]) if row and row["max_date"] else None

    def delete_before(self, cutoff_date: str) -> int:
        """清理过期事件（retention）。"""
        with self.db.tx() as conn:
            cursor = conn.execute("DELETE FROM change_events WHERE stat_date < ?", (cutoff_date,))
            return cursor.rowcount


# ===========================================================================
# 6) 审计日志
# ===========================================================================
class AuditRepository(BaseRepository):
    """``audit_logs`` 表仓储。"""

    def log(
        self,
        actor: str,
        action: str,
        target: str = "",
        detail: dict[str, Any] | None = None,
        level: str = "info",
        request_id: str = "",
    ) -> int:
        """写一条审计日志。

        Args:
            actor: ``cli`` / ``web`` / ``scheduler`` / ``system``。
            action: 动作名，如 ``site.add``。
            target: 作用对象（域名 / id）。
            detail: 结构化详情。
            level: 日志级别。
            request_id: 请求追踪号。

        Returns:
            新记录 id。
        """
        with self.db.tx() as conn:
            cursor = conn.execute(
                "INSERT INTO audit_logs (ts, actor, action, target, detail, level, request_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    utcnow_iso(),
                    actor,
                    action,
                    target,
                    json.dumps(detail or {}, ensure_ascii=False),
                    level,
                    request_id,
                ),
            )
            return int(cursor.lastrowid or 0)

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        keyword: str | None = None,
        since_iso: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """按条件检索审计日志。

        Args:
            actor: 来源过滤。
            action: 动作名模糊匹配。
            keyword: target / detail 模糊搜索。
            since_iso: 起始时间（UTC ISO）。
            page: 页码。
            size: 每页条数。

        Returns:
            ``(记录列表, 总数)``。
        """
        where: list[str] = []
        params: list[Any] = []
        if actor:
            where.append("actor = ?")
            params.append(actor)
        if action:
            where.append("action LIKE ?")
            params.append(f"%{action}%")
        if keyword:
            where.append("(target LIKE ? OR detail LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if since_iso:
            where.append("ts >= ?")
            params.append(since_iso)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        page = max(1, int(page))
        size = max(1, min(int(size), 500))
        with self.db.read() as conn:
            total_row = conn.execute(f"SELECT COUNT(*) AS cnt FROM audit_logs{where_sql}", params).fetchone()
            total = int(total_row["cnt"]) if total_row else 0
            rows = conn.execute(
                f"SELECT * FROM audit_logs{where_sql} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                [*params, size, (page - 1) * size],
            ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            try:
                data["detail"] = json.loads(data.get("detail") or "{}")
            except json.JSONDecodeError:
                data["detail"] = {}
            records.append(data)
        return records, total

    def delete_before(self, cutoff_iso: str) -> int:
        """清理过期审计日志。"""
        with self.db.tx() as conn:
            cursor = conn.execute("DELETE FROM audit_logs WHERE ts < ?", (cutoff_iso,))
            return cursor.rowcount


# ===========================================================================
# 7) 运行批次
# ===========================================================================
class RunRepository(BaseRepository):
    """``runs`` 表仓储（可观测性）。"""

    def start(self, task_id: str, mode: str, config: dict[str, Any] | None = None) -> int:
        """记录一轮任务开始。

        Args:
            task_id: 任务 UUID。
            mode: ``full`` / ``incremental`` / ``manual_single``。
            config: 本轮生效的关键配置快照。

        Returns:
            run_id。
        """
        with self.db.tx() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (task_id, mode, started_at, status, config_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET started_at=excluded.started_at, status='running'",
                (task_id, mode, utcnow_iso(), "running", json.dumps(config or {}, ensure_ascii=False)),
            )
            return int(cursor.lastrowid or 0)

    def finish(self, run_id: int, stats: dict[str, Any]) -> bool:
        """记录一轮任务结束。

        Args:
            run_id: 批次 id。
            stats: 统计字段（total/indexed/not_indexed/unknown/failed/concurrency/proxies_used/duration_ms/status）。

        Returns:
            是否更新成功。
        """
        status = str(stats.get("status", "success"))
        with self.db.tx() as conn:
            cursor = conn.execute(
                "UPDATE runs SET finished_at=?, status=?, total=?, indexed=?, not_indexed=?, unknown=?, "
                "failed=?, concurrency=?, proxies_used=?, duration_ms=? WHERE id=?",
                (
                    stats.get("finished_at") or utcnow_iso(),
                    status,
                    int(stats.get("total", 0)),
                    int(stats.get("indexed", 0)),
                    int(stats.get("not_indexed", 0)),
                    int(stats.get("unknown", 0)),
                    int(stats.get("failed", 0)),
                    int(stats.get("concurrency", 0)),
                    int(stats.get("proxies_used", 0)),
                    int(stats.get("duration_ms", 0)),
                    run_id,
                ),
            )
            return cursor.rowcount > 0

    def get_by_task(self, task_id: str) -> dict[str, Any] | None:
        """按 task_id 查询批次。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE task_id=?", (task_id,)).fetchone()
        return self._as_dict(row)

    def latest(self) -> dict[str, Any] | None:
        """返回最近一轮任务。"""
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
        return self._as_dict(row)

    def list_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        """返回最近 N 轮任务。"""
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ?", (max(1, int(limit)),)
            ).fetchall()
        return [item for item in (self._as_dict(row) for row in rows) if item]

    def set_status(self, run_id: int, status: str) -> bool:
        """更新批次状态（如 cancelled）。"""
        with self.db.tx() as conn:
            cursor = conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
            return cursor.rowcount > 0

    @staticmethod
    def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        """把 runs 行转成字典（解析 config_json）。"""
        if row is None:
            return None
        data = dict(row)
        try:
            data["config"] = json.loads(data.pop("config_json") or "{}")
        except json.JSONDecodeError:
            data["config"] = {}
        return data


# ===========================================================================
# 聚合容器
# ===========================================================================
class Repositories:
    """仓储容器：一次持有全部 Repository，便于依赖注入。

    Attributes:
        db: 数据库管理对象。
        sites: 站点仓储。
        results: 检测明细仓储。
        snapshots: 每日快照仓储。
        proxies: 代理仓储。
        proxies_api: 代理 API 源仓储。
        events: 变化事件仓储。
        audit: 审计仓储。
        runs: 批次仓储。
    """

    def __init__(self, db: Database) -> None:
        """构造全部仓储。

        Args:
            db: 数据库管理对象。
        """
        self.db = db
        self.sites = SiteRepository(db)
        self.results = CheckResultRepository(db)
        self.snapshots = SnapshotRepository(db)
        self.proxies = ProxyRepository(db)
        self.proxies_api = ProxyApiSourceRepository(db)
        self.events = ChangeEventRepository(db)
        self.audit = AuditRepository(db)
        self.runs = RunRepository(db)
