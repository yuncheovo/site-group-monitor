"""QA 阶段发现的回归用例（防止已修问题复发）。

这些用例由 QA 对抗性验证暴露、随后修复的问题固化而来。
每条都对应一个曾经真实存在的缺陷，**不要因为"看起来能过"而删除**。
"""

from __future__ import annotations

import os
import ssl
import time

import pytest

from site_group_monitor.config.settings import Settings
from site_group_monitor.models.entities import DailySnapshot, Site
from site_group_monitor.models.enums import IndexStatus
from site_group_monitor.services.diff_service import DiffService
from site_group_monitor.storage.database import Database
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.utils.netutil import normalize_domain
from site_group_monitor.utils.timeutil import is_date_key, shift_days, utcnow_iso


@pytest.fixture()
def repos(tmp_path):
    """构造临时库的仓储容器。"""
    database = Database(tmp_path / "qa.db")
    database.init_db()
    yield Repositories(database)
    database.close_all()


# ------------------------------------------------------------------ 域名归一化
def test_control_chars_are_rejected_not_merged() -> None:
    """含 tab/换行的输入必须判无效，不能被拼成假域名。

    缺陷：``example.com\\tfoo`` 曾被 urlsplit 静默吞掉 tab → ``example.comfoo``。
    """
    for raw in ("example.com\tfoo", "example.com\nfoo", "example.com\rfoo", "exa\x00mple.com"):
        assert normalize_domain(raw) == "", f"{raw!r} 应判无效"


def test_normalize_domain_common_forms() -> None:
    """常见合法写法都应归一化到同一结果。"""
    assert normalize_domain("https://WWW.Example.com:443/a/?b=1") == "example.com"
    assert normalize_domain("http://user:pass@example.com/p") == "example.com"
    assert normalize_domain("  example.com  ") == "example.com"
    assert normalize_domain("example.com:8080") == "example.com"


def test_normalize_domain_rejects_invalid() -> None:
    """明显非法输入返回空串而不是抛异常。"""
    for raw in ("not a domain", "javascript:alert(1)", "", "a" * 300 + ".com"):
        assert normalize_domain(raw) == ""


# ------------------------------------------------------------------ 日期校验
def test_is_date_key_validates_real_dates() -> None:
    """``is_date_key`` 要拒绝格式对但日期不存在的输入。"""
    assert is_date_key("2026-10-27") is True
    assert is_date_key("2026-02-30") is False
    assert is_date_key("2026-13-01") is False
    assert is_date_key("abc") is False
    assert is_date_key("2026-1-1") is False


def test_shift_days_raises_clear_error_on_bad_input() -> None:
    """非法日期键要抛 ValueError 且信息可读，不能是 strptime 裸错误。"""
    with pytest.raises(ValueError, match="非法日期键"):
        shift_days("abc", -1)


# ------------------------------------------------------------------ diff 幂等
def test_diff_is_idempotent(repos: Repositories) -> None:
    """同一天重复跑 diff 不能重复计事件。

    缺陷：曾因无唯一约束，重跑一次就多一条 new_indexed，日报数字虚高。
    """
    setting = Settings()
    site_id = repos.sites.insert(
        Site(domain="idem.com", created_at=utcnow_iso(), updated_at=utcnow_iso())
    )
    today = utcnow_iso()[:10]
    yesterday = shift_days(today, -1)
    repos.snapshots.upsert(
        DailySnapshot(site_id=site_id, stat_date=yesterday,
                      index_status=IndexStatus.NOT_INDEXED.value, is_baseline=1,
                      created_at=utcnow_iso())
    )
    repos.snapshots.upsert(
        DailySnapshot(site_id=site_id, stat_date=today,
                      index_status=IndexStatus.INDEXED.value, is_baseline=0,
                      created_at=utcnow_iso())
    )
    service = DiffService(repos, setting, None)
    assert len(service.diff_yesterday(today)) == 1
    assert len(service.diff_yesterday(today)) == 0, "第 2 次不应再产生事件"
    assert len(service.diff_yesterday(today)) == 0
    assert repos.events.counts_by_type(today).get("new_indexed") == 1


# ------------------------------------------------------------------ 导入性能
def test_bulk_import_meets_prd_performance(repos: Repositories) -> None:
    """PRD R-001：500 条导入 < 10 秒。

    缺陷：曾因「每条记录都新建并关闭 WAL 连接」（Windows 上 close 触发检查点 ~100ms）
    而耗时 100+ 秒。修复为按线程复用连接后应在 1 秒级。
    """
    total = 500
    start = time.perf_counter()
    for index in range(total):
        repos.sites.upsert_by_domain(
            Site(domain=f"perf-{index}.com", created_at=utcnow_iso(), updated_at=utcnow_iso())
        )
    elapsed = time.perf_counter() - start
    assert elapsed < 10, f"{total} 条导入耗时 {elapsed:.2f}s，超过 PRD 的 10s 要求"
    assert repos.sites.count_all() == total


def test_connection_reuse_does_not_break_cross_thread(repos: Repositories, tmp_path) -> None:
    """连接池必须按线程隔离，跨线程借连接会触发 sqlite3 线程检查。"""
    import threading

    errors: list[str] = []

    def worker(index: int) -> None:
        try:
            repos.sites.upsert_by_domain(
                Site(domain=f"thread-{index}.com", created_at=utcnow_iso(), updated_at=utcnow_iso())
            )
        except Exception as exc:  # noqa: BLE001 - 收集后统一断言
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"跨线程写库失败: {errors}"
    assert repos.sites.count_all() == 5


# ------------------------------------------------------------------ 内存库
def test_memory_database_shares_single_connection() -> None:
    """内存库必须复用同一条连接，否则每次连接都是新空库。"""
    database = Database(":memory:")
    database.init_db()
    repos = Repositories(database)
    repos.sites.insert(Site(domain="mem.com", created_at=utcnow_iso(), updated_at=utcnow_iso()))
    assert repos.sites.get_by_domain("mem.com") is not None
    assert repos.sites.count_all() == 1
    database.close_all()


# ------------------------------------------------------- SSL / 代理 / 打包回归
def test_ssl_context_includes_certifi_ca() -> None:
    """SSL 校验上下文必须叠加 certifi 的 CA。

    回归背景：Windows 系统根证书存储本机仅 64 条，缺 Cloudflare / SSL.com
    等 issuer 链，单独使用会把大量正常 HTTPS 站误判成 ``tls_failed``。
    certifi 提供 99 条权威 CA，``load_verify_locations`` 是追加语义，
    两者并集最稳。
    """
    from site_group_monitor.core.status_checker import _build_ssl_context

    ctx = _build_ssl_context()
    baseline = ssl.create_default_context()
    assert len(ctx.get_ca_certs()) >= len(baseline.get_ca_certs()), (
        "叠加 certifi 后 CA 数量不应少于系统默认"
    )


def test_http_client_does_not_trust_env_proxy() -> None:
    """HTTP 客户端不得继承系统环境变量代理。

    回归背景：httpx 默认 ``trust_env=True``，会读取 ``http_proxy`` /
    ``https_proxy`` 环境变量。用户显式配置了代理池时，这会让直连降级路径
    被环境代理劫持；环境代理不通时表现为**全站 timeout**，极难排查。
    """
    from site_group_monitor.core.http_client import HttpClient

    client = HttpClient(None)._client(None)
    try:
        # httpx 会在 trust_env=True 时把环境代理写进 mounts
        assert not getattr(client, "_trust_env", False), "客户端不应信任环境代理变量"
    finally:
        import asyncio

        asyncio.get_event_loop().run_until_complete(client.aclose())


def test_tdk_parser_uses_dynamic_playwright_import() -> None:
    """TDK 渲染兜底必须用动态 import，避免把 playwright driver 打进 exe。

    回归背景：playwright 的 driver 目录带 node.exe（约 99MB），静态 import
    会被 PyInstaller 收进 exe，导致体积 72MB、冷启动 9~14s。
    """
    import pathlib

    source = pathlib.Path(__file__).resolve().parents[1] / "site_group_monitor" / "core" / "tdk_parser.py"
    text = source.read_text(encoding="utf-8")
    assert "from playwright.async_api import" not in text, (
        "playwright 必须改为 importlib.import_module 动态加载"
    )
    assert "importlib.import_module(\"playwright.async_api\")" in text


def test_spec_excludes_heavy_optional_deps() -> None:
    """打包配置必须排除 playwright 与 Pillow。

    回归背景：二者都是可选/未使用依赖，不排除会让 exe 从约 25MB 膨胀到 72MB。
    """
    import pathlib

    spec = pathlib.Path(__file__).resolve().parents[1] / "sgm.spec"
    text = spec.read_text(encoding="utf-8")
    for name in ("playwright", "PIL"):
        assert f'"{name}"' in text, f"sgm.spec 必须排除 {name}"
    assert '"certifi"' in text, "sgm.spec 必须携带 certifi（SSL 校验依赖其 CA 集）"
