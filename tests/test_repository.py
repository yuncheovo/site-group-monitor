"""Repository 层单测（临时 SQLite，验证幂等/约束/聚合查询）。"""

from __future__ import annotations

from datetime import timedelta

from site_group_monitor.models.entities import CheckResult, DailySnapshot, ProxyEndpoint, Site
from site_group_monitor.models.enums import IndexStatus, RunMode
from site_group_monitor.storage.database import Database
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.utils.timeutil import shift_days, to_iso, utcnow, utcnow_iso


def _make_site(repos: Repositories, domain: str, group: str = "default") -> Site:
    """写入一个站点并返回带 id 的实体。

    Args:
        repos: 仓储容器。
        domain: 域名。
        group: 分组。

    Returns:
        站点实体。
    """
    site = Site(domain=domain, group_name=group, created_at=utcnow_iso(), updated_at=utcnow_iso())
    site_id = repos.sites.insert(site)
    site.id = site_id
    return site


def _make_result(repos: Repositories, site_id: int, status: str, task_id: str = "task-1",
                 when_offset_days: int = 0) -> int:
    """写入一条检测结果。

    Args:
        repos: 仓储容器。
        site_id: 站点 id。
        status: 收录三态。
        task_id: 任务 UUID。
        when_offset_days: 相对当前的天偏移（负数表示过去）。

    Returns:
        结果 id。
    """
    result = CheckResult(
        site_id=site_id,
        task_id=task_id,
        run_mode=RunMode.FULL.value,
        checked_at=to_iso(utcnow() - timedelta(days=when_offset_days)),
        index_status=status,
        index_reason="test",
        http_status=200,
        response_ms=120,
    )
    result.tdk.title = f"title-{site_id}"
    result.tdk.apply_hashes()
    result.ssl.days_left = 30
    return repos.results.insert(result)


def test_init_db_creates_seven_tables(tmp_db: Database) -> None:
    """初始化应建出 7 张业务表，且重复执行幂等。"""
    tables = tmp_db.list_tables()
    expected = {
        "sites", "check_results", "daily_snapshots",
        "proxies", "change_events", "audit_logs", "runs",
    }
    assert expected.issubset(set(tables))
    # 重复初始化幂等
    tmp_db.init_db()
    assert expected.issubset(set(tmp_db.list_tables()))
    assert tmp_db.schema_version() >= 1


def test_site_upsert_is_idempotent(repos: Repositories) -> None:
    """按域名 upsert 必须幂等（只更新元信息，不重复建记录）。"""
    first = _make_result_target(repos)
    second_id = repos.sites.upsert_by_domain(
        Site(domain="example.com", name="改名了", group_name="g2", updated_at=utcnow_iso())
    )
    assert second_id == first.id
    assert repos.sites.count_all() == 1
    stored = repos.sites.get_by_domain("example.com")
    assert stored is not None
    assert stored.name == "改名了"


def _make_result_target(repos: Repositories) -> Site:
    """辅助：创建 example.com 站点。"""
    site = Site(domain="example.com", created_at=utcnow_iso(), updated_at=utcnow_iso())
    site.id = repos.sites.insert(site)
    return site


def test_check_result_roundtrip(repos: Repositories) -> None:
    """CheckResult 写入后读出应还原嵌套 TdkInfo / SslInfo / JSON 字段。"""
    site = _make_site(repos, "roundtrip.com")
    result = CheckResult(
        site_id=int(site.id or 0),
        task_id="t1",
        checked_at=utcnow_iso(),
        index_status=IndexStatus.INDEXED.value,
        redirect_chain=[{"url": "https://roundtrip.com/", "status": 301}],
        anomalies={"mixed_content": True},
    )
    result.tdk.title = "标题"
    result.tdk.description = "描述"
    result.ssl.days_left = 9
    result.ssl.issuer = "CN=Test CA"
    result_id = repos.results.insert(result)

    loaded = repos.results.latest(int(site.id or 0))
    assert loaded is not None
    assert loaded.id == result_id
    assert loaded.tdk.title == "标题"
    assert loaded.tdk.description == "描述"
    assert loaded.ssl.days_left == 9
    assert loaded.redirect_chain[0]["status"] == 301
    assert loaded.anomalies.get("mixed_content") is True


def test_snapshot_unique_constraint_upsert(repos: Repositories) -> None:
    """``(site_id, stat_date)`` 冲突时必须 upsert 而不是抛错。"""
    site = _make_site(repos, "snapshot.com")
    today = "2024-05-06"
    repos.snapshots.upsert(DailySnapshot(site_id=int(site.id or 0), stat_date=today,
                                         index_status=IndexStatus.INDEXED.value, created_at=utcnow_iso()))
    repos.snapshots.upsert(DailySnapshot(site_id=int(site.id or 0), stat_date=today,
                                         index_status=IndexStatus.NOT_INDEXED.value,
                                         created_at=utcnow_iso()))
    rows = repos.snapshots.by_date(today)
    assert len(rows) == 1
    assert str(rows[0].index_status) == IndexStatus.NOT_INDEXED.value


def test_previous_snapshot_skips_baseline(repos: Repositories) -> None:
    """previous() 默认跳过基线快照。"""
    site = _make_site(repos, "prev.com")
    site_id = int(site.id or 0)
    repos.snapshots.upsert(DailySnapshot(site_id=site_id, stat_date="2024-05-01", is_baseline=1,
                                         index_status=IndexStatus.INDEXED.value, created_at=utcnow_iso()))
    repos.snapshots.upsert(DailySnapshot(site_id=site_id, stat_date="2024-05-02",
                                         index_status=IndexStatus.INDEXED.value, created_at=utcnow_iso()))
    assert repos.snapshots.previous(site_id, "2024-05-03") is not None
    assert repos.snapshots.has_history_before(site_id, "2024-05-02") is True


def test_trend_aggregation(repos: Repositories) -> None:
    """趋势聚合按日统计三态数量并算收录率（排除 unknown）。"""
    today = "2024-05-06"
    first = _make_site(repos, "a.com")
    second = _make_site(repos, "b.com")
    third = _make_site(repos, "c.com")
    for site, status in ((first, IndexStatus.INDEXED.value),
                         (second, IndexStatus.NOT_INDEXED.value),
                         (third, IndexStatus.UNKNOWN.value)):
        repos.snapshots.upsert(DailySnapshot(site_id=int(site.id or 0), stat_date=today,
                                             index_status=status, created_at=utcnow_iso()))
    trend = repos.snapshots.trend(days=30, end_date=today)
    assert len(trend) == 1
    point = trend[0]
    assert point["total"] == 3
    assert point["indexed"] == 1
    assert point["not_indexed"] == 1
    assert point["unknown"] == 1
    assert point["rate"] == 0.5  # 1 / (1+1)，unknown 不计入分母


def test_change_events_filters_and_counts(repos: Repositories) -> None:
    """事件按日期与类型筛选、计数正确。"""
    from site_group_monitor.models.entities import ChangeEvent
    from site_group_monitor.models.enums import EventType

    site = _make_site(repos, "event.com")
    site_id = int(site.id or 0)
    today = "2024-05-06"
    repos.events.insert_many([
        ChangeEvent(site_id=site_id, event_type=EventType.NEW_INDEXED.value, stat_date=today,
                    created_at=utcnow_iso()),
        ChangeEvent(site_id=site_id, event_type=EventType.LOST_INDEXED.value, stat_date=today,
                    created_at=utcnow_iso()),
        ChangeEvent(site_id=site_id, event_type=EventType.LOST_INDEXED.value,
                    stat_date=shift_days(today, -1), created_at=utcnow_iso()),
    ])
    assert len(repos.events.list_by_date(today)) == 2
    counts = repos.events.counts_by_type(today)
    assert counts.get(EventType.NEW_INDEXED.value) == 1
    assert counts.get(EventType.LOST_INDEXED.value) == 1
    marked = repos.events.mark_notified([int(item.id or 0) for item in repos.events.list_by_date(today)])
    assert marked == 2
    assert repos.events.unnotified(today) == []


def test_proxy_repository_roundtrip(repos: Repositories) -> None:
    """代理仓储：幂等保存、禁停用、健康度反馈、统计。"""
    proxy = ProxyEndpoint(protocol="http", host="1.2.3.4", port=8080, username="u",
                          password_enc="p", created_at=utcnow_iso())
    proxy_id = repos.proxies.save(proxy)
    proxy.id = proxy_id
    repos.proxies.save(proxy)
    listed = repos.proxies.list_all()
    assert len(listed) == 1
    assert listed[0].masked_url.startswith("http://***:***@1.2.3.4:8080")

    proxy.mark_fail("captcha")
    proxy.disabled = True
    repos.proxies.save_state(proxy)
    stats = repos.proxies.stats()
    assert stats["total"] == 1
    assert stats["enabled"] == 0
    assert stats["empty"] is True


def test_cascade_delete_cleans_children(repos: Repositories) -> None:
    """删除站点应级联清理明细 / 快照 / 事件（外键 ON DELETE CASCADE）。"""
    site = _make_site(repos, "cascade.com")
    site_id = int(site.id or 0)
    _make_result(repos, site_id, IndexStatus.INDEXED.value)
    repos.snapshots.upsert(DailySnapshot(site_id=site_id, stat_date="2024-05-06",
                                         index_status=IndexStatus.INDEXED.value, created_at=utcnow_iso()))
    from site_group_monitor.models.entities import ChangeEvent

    repos.events.insert(ChangeEvent(site_id=site_id, event_type="new_indexed",
                                    stat_date="2024-05-06", created_at=utcnow_iso()))
    assert repos.results.latest(site_id) is not None
    assert repos.sites.remove(site_id) is True
    assert repos.results.latest(site_id) is None
    assert repos.snapshots.by_date("2024-05-06") == []
    assert repos.events.list_by_date("2024-05-06") == []


def test_runs_lifecycle(repos: Repositories) -> None:
    """runs 表：start → finish → latest 查询。"""
    run_id = repos.runs.start("task-x", "full", {"concurrency": 6})
    run = repos.runs.get_by_task("task-x")
    assert run is not None and run["status"] == "running"
    repos.runs.finish(run_id, {"status": "success", "total": 10, "indexed": 8, "duration_ms": 1000})
    latest = repos.runs.latest()
    assert latest is not None
    assert latest["status"] == "success"
    assert latest["indexed"] == 8


def test_retention_cleanup(repos: Repositories) -> None:
    """过期数据清理应删掉早于截止日期的记录。"""
    site = _make_site(repos, "old.com")
    site_id = int(site.id or 0)
    _make_result(repos, site_id, IndexStatus.INDEXED.value, when_offset_days=400)
    old_date = shift_days(utcnow_iso()[:10], -400)
    repos.snapshots.upsert(DailySnapshot(site_id=site_id, stat_date=old_date,
                                         index_status=IndexStatus.INDEXED.value, created_at=utcnow_iso()))
    # 截止日期取「100 天前」，保证 400 天前的记录一定早于它（避免硬编码日期随时间失效）
    cutoff_date = shift_days(utcnow_iso()[:10], -100)
    cutoff_iso = to_iso(utcnow() - timedelta(days=100))
    assert repos.snapshots.delete_before(cutoff_date) == 1
    assert repos.results.delete_before(cutoff_iso) == 1
