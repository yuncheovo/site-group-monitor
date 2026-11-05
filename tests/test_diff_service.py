"""diff 规则单测：基线不误报、新增/掉收录判定、unknown 不参与 diff、unknown 持续告警。"""

from __future__ import annotations

from typing import Any

from site_group_monitor.models.entities import CheckResult, DailySnapshot, Site
from site_group_monitor.models.enums import IndexStatus, RunMode
from site_group_monitor.services.diff_service import DiffService
from site_group_monitor.storage.database import Database
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.utils.timeutil import shift_days, utcnow_iso


def _delay_days(repos: Repositories) -> Any:
    """返回用于设置 AlertService 阈值的辅助占位（保持测试简洁）。"""
    return repos


def _seed(repos: Repositories, domain: str, status: str, stat_date: str, *, baseline: bool = False,
          http_status: int | None = 200) -> int:
    """写入一条检测结果 + 对应快照，返回 site_id。

    Args:
        repos: 仓储容器。
        domain: 域名。
        status: 收录三态。
        stat_date: 日期键。
        baseline: 是否标记为基线。
        http_status: HTTP 状态码。

    Returns:
        站点 id。
    """
    site = Site(domain=domain, created_at=utcnow_iso(), updated_at=utcnow_iso())
    site.id = repos.sites.insert(site)
    site_id = int(site.id)
    result = CheckResult(
        site_id=site_id, task_id="t", run_mode=RunMode.FULL.value, checked_at=utcnow_iso(),
        index_status=status, http_status=http_status,
    )
    result.tdk.title = f"title-{domain}"
    result.tdk.apply_hashes()
    result.ssl.days_left = 40
    repos.results.insert(result)
    repos.snapshots.upsert(
        DailySnapshot(
            site_id=site_id, stat_date=stat_date, index_status=status,
            http_status=http_status, tdk_title_hash=result.tdk.title_hash,
            is_baseline=int(baseline), created_at=utcnow_iso(),
        )
    )
    return site_id


class _SettingsStub:
    """极简配置桩，避免依赖真实配置文件。"""

    def timezone_name(self) -> str:
        """返回时区名。"""
        return "Asia/Shanghai"

    class alert:  # noqa: N801 - 模拟配置对象的嵌套结构
        """告警配置值。"""

        unknown_persistent_days = 3
        ssl_warn_days = 15
        dedup_hours = 12
        index_rate_threshold = 0.7
        quiet_hours = ["23:00", "07:30"]
        enabled = True


def _service(tmp_db: Database) -> DiffService:
    """构造基于临时库的 diff 服务。"""
    repos = Repositories(tmp_db)
    return DiffService(repos, _SettingsStub())


def test_first_run_creates_baseline_without_events(tmp_db: Database) -> None:
    """首轮无任何历史 → is_baseline=1，且**不产生任何 diff 事件**（零误报）。"""
    repos = Repositories(tmp_db)
    service = _service(tmp_db)
    stat_date = "2024-05-06"
    result = CheckResult(site_id=0, task_id="t", run_mode=RunMode.FULL.value,
                         checked_at=utcnow_iso(), index_status=IndexStatus.INDEXED.value)
    site = Site(domain="brand-new.com", created_at=utcnow_iso(), updated_at=utcnow_iso())
    site.id = repos.sites.insert(site)
    result.site_id = int(site.id)
    repos.results.insert(result)

    snapshots = service.build_daily_snapshots(stat_date, [result])
    assert len(snapshots) == 1
    assert snapshots[0].is_baseline == 1
    events = service.diff_yesterday(stat_date)
    assert events == []


def test_new_indexed_event(tmp_db: Database) -> None:
    """昨日 not_indexed → 今日 indexed 产生 new_indexed 事件。"""
    service = _service(tmp_db)
    today = "2024-05-06"
    yesterday = shift_days(today, -1)
    _seed(Repositories(tmp_db), "up.com", IndexStatus.NOT_INDEXED.value, yesterday, baseline=True)
    repos = Repositories(tmp_db)
    site_ids = {site.domain: int(site.id or 0) for site in repos.sites.list_all()}
    today_result = CheckResult(site_id=site_ids["up.com"], task_id="t2",
                               run_mode=RunMode.FULL.value, checked_at=utcnow_iso(),
                               index_status=IndexStatus.INDEXED.value, http_status=200)
    repos.results.insert(today_result)
    service.build_daily_snapshots(today, [today_result])

    events = service.diff_yesterday(today)
    assert len(events) == 1
    assert str(events[0].event_type) == "new_indexed"
    assert events[0].old_value == IndexStatus.NOT_INDEXED.value
    assert events[0].new_value == IndexStatus.INDEXED.value


def test_lost_indexed_event(tmp_db: Database) -> None:
    """昨日 indexed → 今日 not_indexed 产生 lost_indexed 事件。"""
    service = _service(tmp_db)
    today = "2024-05-06"
    yesterday = shift_days(today, -1)
    _seed(Repositories(tmp_db), "down.com", IndexStatus.INDEXED.value, yesterday, baseline=True)
    repos = Repositories(tmp_db)
    today_result = CheckResult(site_id=1, task_id="t3", run_mode=RunMode.FULL.value,
                               checked_at=utcnow_iso(), index_status=IndexStatus.NOT_INDEXED.value)
    repos.results.insert(today_result)
    service.build_daily_snapshots(today, [today_result])

    events = service.diff_yesterday(today)
    assert len(events) == 1
    assert str(events[0].event_type) == "lost_indexed"


def test_unknown_never_produces_diff_events(tmp_db: Database) -> None:
    """unknown 不参与 diff：今日 unknown 或昨日 unknown 都不产生 new/lost 事件。"""
    service = _service(tmp_db)
    today = "2024-05-06"
    yesterday = shift_days(today, -1)
    repos = Repositories(tmp_db)
    _seed(repos, "unknown-site.com", IndexStatus.INDEXED.value, yesterday, baseline=True)
    site_ids = {site.domain: int(site.id or 0) for site in repos.sites.list_all()}
    today_result = CheckResult(site_id=site_ids["unknown-site.com"], task_id="t4",
                               run_mode=RunMode.FULL.value, checked_at=utcnow_iso(),
                               index_status=IndexStatus.UNKNOWN.value)
    repos.results.insert(today_result)
    service.build_daily_snapshots(today, [today_result])
    assert service.diff_yesterday(today) == []


def test_unknown_persistent_event(tmp_db: Database) -> None:
    """连续 ≥3 天 unknown 时生成 unknown_persistent 事件。"""
    service = _service(tmp_db)
    today = "2024-05-06"
    repos = Repositories(tmp_db)
    site_id = _seed(repos, "persist-unknown.com", IndexStatus.UNKNOWN.value, today, baseline=True)
    for offset in (1, 2, 3):
        date_key = shift_days(today, -offset)
        repos.snapshots.upsert(
            DailySnapshot(site_id=site_id, stat_date=date_key,
                          index_status=IndexStatus.UNKNOWN.value, created_at=utcnow_iso())
        )
    events = service.check_unknown_persistent(today)
    assert len(events) == 1
    assert str(events[0].event_type) == "unknown_persistent"
    assert int(events[0].detail.get("streak_days", 0)) >= 3


def test_diff_week_is_memory_only(tmp_db: Database) -> None:
    """diff_week 只产出不落库，报告中用于对比列。"""
    service = _service(tmp_db)
    today = "2024-05-06"
    week_ago = shift_days(today, -7)
    repos = Repositories(tmp_db)
    _seed(repos, "week.com", IndexStatus.NOT_INDEXED.value, week_ago, baseline=True)
    today_result = CheckResult(site_id=1, task_id="t5", run_mode=RunMode.FULL.value,
                               checked_at=utcnow_iso(), index_status=IndexStatus.INDEXED.value)
    repos.results.insert(today_result)
    service.build_daily_snapshots(today, [today_result])

    events = service.diff_week(today)
    assert len(events) == 1
    assert str(events[0].event_type) == "new_indexed"
    assert events[0].detail.get("compare") == "week"
    assert repos.events.list_by_date(today) == []


def test_health_events_generation(tmp_db: Database) -> None:
    """健康类事件：HTTP 异常 / 重定向异常 / DNS 失败。"""
    service = _service(tmp_db)
    today = "2024-05-06"
    repos = Repositories(tmp_db)
    site = Site(domain="health.com", created_at=utcnow_iso(), updated_at=utcnow_iso())
    site.id = repos.sites.insert(site)
    result = CheckResult(site_id=int(site.id), task_id="t6", run_mode=RunMode.FULL.value,
                         checked_at=utcnow_iso(), index_status=IndexStatus.INDEXED.value,
                         http_status=500, anomalies={"redirect_anomaly": True},
                         error_type="dns_failed")
    result.ssl.days_left = 3
    repos.results.insert(result)

    events = service.check_health_events(today)
    kinds = {str(item.event_type) for item in events}
    assert "http_anomaly" in kinds
    assert "ssl_expiring" not in kinds
    assert "redirect_anomaly" in kinds
    assert "dns_failed" in kinds
