"""告警判定：阈值 + 静默时段 + 去重冷却（R-014）。

三条规则：
    1. **阈值**：整体收录率跌破 ``alert.index_rate_threshold`` → ``index_rate_drop``；
       掉收录站点数 > 0 → ``lost_indexed``；SSL 临期 / HTTP 异常同样产生告警；
    2. **静默时段**：``alert.quiet_hours``（支持跨零点）内 **不push邮件**，但仍落库报告；
    3. **去重冷却**：同一 (site_id, event_type) 在 ``alert.dedup_hours`` 内不重复告警。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Sequence

from site_group_monitor.models.entities import ChangeEvent, DailyReport
from site_group_monitor.models.enums import EventType
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.utils.timeutil import in_quiet_hours, utcnow

#: 参与告警的事件类型（其余仅记录）
ALERT_EVENT_TYPES: tuple[str, ...] = (
    EventType.LOST_INDEXED.value,
    EventType.NEW_INDEXED.value,
    EventType.UNKNOWN_PERSISTENT.value,
    EventType.SSL_EXPIRING.value,
    EventType.HTTP_ANOMALY.value,
    EventType.REDIRECT_ANOMALY.value,
    EventType.DNS_FAILED.value,
    EventType.RATE_DROP.value,
)


class AlertService:
    """告警评估服务。"""

    def __init__(self, repos: Repositories, settings: Any, logger: Any | None = None) -> None:
        """构造告警服务。

        Args:
            repos: 仓储容器。
            settings: 配置对象。
            logger: 日志记录器。
        """
        self.repos = repos
        self.settings = settings
        self.logger = logger
        self._sent_cache: set[tuple[int, str, str]] = set()

    # ================================================================ 主入口
    def evaluate(self, report: DailyReport, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """基于日报评估告警清单。

        Args:
            report: 日报告。
            now: 当前时间（便于测试）。

        Returns:
            告警字典列表（含 ``event_type`` / ``domain`` / ``level`` / ``detail``）。
        """
        alerts: list[dict[str, Any]] = []

        threshold = float(getattr(self.settings.alert, "index_rate_threshold", 0.70) or 0.70)
        if report.index_rate < threshold:
            alerts.append(
                {
                    "event_type": EventType.RATE_DROP.value,
                    "site_id": 0,
                    "domain": "(全站)",
                    "level": "critical",
                    "detail": {
                        "index_rate": report.index_rate,
                        "threshold": threshold,
                        "checked": report.checkable_count(),
                        "unknown": report.unknown_count,
                    },
                }
            )

        for item in report.lost_indexed:
            alerts.append(
                {
                    "event_type": EventType.LOST_INDEXED.value,
                    "site_id": int(item.get("site_id") or 0),
                    "domain": str(item.get("domain") or ""),
                    "level": "critical",
                    "detail": {"old_value": item.get("old_value"), "new_value": item.get("new_value")},
                }
            )
        for item in report.anomalies:
            kind = str(item.get("kind") or item.get("event_type") or "")
            if kind not in ALERT_EVENT_TYPES:
                continue
            alerts.append(
                {
                    "event_type": kind,
                    "site_id": int(item.get("site_id") or 0),
                    "domain": str(item.get("domain") or ""),
                    "level": "warning",
                    "detail": dict(item.get("detail") or {}),
                }
            )
        # 去重冷却
        filtered = [alert for alert in alerts if not self.dedup(alert, report.stat_date)]
        if self.logger is not None:
            self.logger.info("alert: 评估出 %d 条告警（去重前 %d 条）", len(filtered), len(alerts))
        if self.in_quiet_hours(now) and self.logger is not None:
            self.logger.info("alert: 处于静默时段，告警仅落库不推送")
        return filtered

    def evaluate_events(self, events: Sequence[ChangeEvent], stat_date: str) -> list[dict[str, Any]]:
        """直接将已落库的事件列表转为告警清单（供手动触发场景）。

        Args:
            events: 事件列表。
            stat_date: 日期键。

        Returns:
            告警字典列表。
        """
        alerts: list[dict[str, Any]] = []
        for event in events:
            if str(event.event_type) not in ALERT_EVENT_TYPES:
                continue
            alert = {
                "event_type": str(event.event_type),
                "site_id": int(event.site_id or 0),
                "domain": str(getattr(event, "domain", "") or ""),
                "level": "critical" if str(event.event_type) == EventType.LOST_INDEXED.value else "warning",
                "detail": dict(event.detail or {}),
            }
            if not self.dedup(alert, stat_date):
                alerts.append(alert)
        return alerts

    # ================================================================ 规则
    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        """是否处于静默时段。

        Args:
            now: 当前时间；缺省取当前 UTC。

        Returns:
            静默返回 True。
        """
        if not getattr(self.settings.alert, "enabled", True):
            return False
        quiet = list(getattr(self.settings.alert, "quiet_hours", []) or [])
        return in_quiet_hours(now or utcnow(), quiet, self.settings.timezone_name())

    def dedup(self, alert: dict[str, Any], stat_date: str) -> bool:
        """判断该告警是否处于去重冷却窗口内（True = 应抑制）。

        Args:
            alert: 告警字典。
            stat_date: 日期键。

        Returns:
            需要抑制返回 True。
        """
        key = (int(alert.get("site_id") or 0), str(alert.get("event_type")), str(stat_date))
        if key in self._sent_cache:
            return True
        window_days = 1
        dedup_hours = int(getattr(self.settings.alert, "dedup_hours", 12) or 12)
        if dedup_hours >= 24:
            window_days = max(1, dedup_hours // 24)
        site_id = int(alert.get("site_id") or 0)
        if site_id and alert.get("event_type") != EventType.RATE_DROP.value:
            if self.repos.events.exists_recent(
                site_id, str(alert.get("event_type")), stat_date, window_days=window_days
            ):
                return True
        self._sent_cache.add(key)
        return False

    def mark_notified(self, alerts: Iterable[dict[str, Any]], stat_date: str) -> int:
        """把已通知的事件在库中标记为 ``notified=1``。

        Args:
            alerts: 告警列表。
            stat_date: 日期键。

        Returns:
            标记条数。
        """
        events = self.repos.events.list_by_date(stat_date)
        targets = [
            event.id
            for event in events
            if any(
                int(alert.get("site_id") or 0) == int(event.site_id)
                and str(alert.get("event_type")) == str(event.event_type)
                for alert in alerts
            )
            and event.id is not None
        ]
        return self.repos.events.mark_notified([int(item) for item in targets])
