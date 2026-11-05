"""每日快照构建与 diff 事件生成（R-006 / R-012 / R-017）。

核心规则（对应架构文档 §4.1 与安全红线 7）：
    * 每站每日写一条 ``daily_snapshots``（``UNIQUE(site_id, stat_date)``，重复跑幂等 upsert）；
    * **首站无历史 → ``is_baseline=1``，且不产生任何 diff 事件**（首次零误报）；
    * 之后与「前一条 **非基线** 快照」比对，状态变化才生成 ``new_indexed`` / ``lost_indexed``；
    * **``unknown`` 不参与 diff**：今日 unknown 不生成 new/lost；
    * 连续 ≥ ``alert.unknown_persistent_days`` 天 unknown → ``unknown_persistent`` 事件；
    * 另产出 SSL 到期、HTTP 异常、重定向异常、DNS 失败、TDK 变更等健康类事件。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from site_group_monitor.models.entities import ChangeEvent, CheckResult, DailySnapshot
from site_group_monitor.models.enums import ErrorType, EventType, IndexStatus
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.utils.timeutil import date_range, shift_days, today_str, utcnow_iso


class DiffService:
    """快照与 diff 服务。"""

    def __init__(self, repos: Repositories, settings: Any, logger: Any | None = None) -> None:
        """构造 diff 服务。

        Args:
            repos: 仓储容器。
            settings: 配置对象。
            logger: 日志记录器。
        """
        self.repos = repos
        self.settings = settings
        self.logger = logger

    # ================================================================ 快照
    def build_daily_snapshots(
        self,
        stat_date: str | None = None,
        results: Sequence[CheckResult] | None = None,
    ) -> list[DailySnapshot]:
        """为一天的检测结果生成/更新每日快照（幂等）。

        Args:
            stat_date: 日期键，缺省今日。
            results: 本轮检测结果；缺省取「每个站点最近一条结果」。

        Returns:
            写入的快照列表。
        """
        stat_date = stat_date or today_str(self.settings.timezone_name())
        items = list(results) if results is not None else self.repos.results.latest_all()
        snapshots: list[DailySnapshot] = []
        for result in items:
            site_id = int(result.site_id or 0)
            if not site_id:
                continue
            is_baseline = not self.repos.snapshots.has_history_before(site_id, stat_date)
            snapshot = DailySnapshot(
                site_id=site_id,
                stat_date=stat_date,
                index_status=str(result.index_status),
                http_status=result.http_status,
                response_ms=result.response_ms,
                ssl_days_left=result.ssl.days_left if result.ssl else None,
                title=result.tdk.title if result.tdk else "",
                tdk_title_hash=result.tdk.title_hash if result.tdk else "",
                is_baseline=int(is_baseline),
                created_at=utcnow_iso(),
            )
            self.repos.snapshots.upsert(snapshot)
            snapshots.append(snapshot)
            try:
                self.repos.sites.mark_baseline(site_id, True)
            except Exception:  # noqa: BLE001 - 基线标记失败不影响快照
                pass
        if self.logger is not None:
            self.logger.info("diff: %s 建立/更新 %d 条快照（其中基线 %d 条）", stat_date,
                             len(snapshots), sum(1 for item in snapshots if item.is_baseline))
        return snapshots

    def baseline_count(self, stat_date: str) -> int:
        """统计某天新增的基线快照数量（报告「建立基线 N 个」）。

        Args:
            stat_date: 日期键。

        Returns:
            基线数量。
        """
        return sum(1 for item in self.repos.snapshots.by_date(stat_date) if item.is_baseline)

    # ================================================================ diff
    def diff_yesterday(self, stat_date: str | None = None) -> list[ChangeEvent]:
        """对当日快照与前一条非基线快照做 diff，产出事件并落库。

        Args:
            stat_date: 日期键，缺省今日。

        Returns:
            生成的事件列表。
        """
        stat_date = stat_date or today_str(self.settings.timezone_name())
        events: list[ChangeEvent] = []
        for snapshot in self.repos.snapshots.by_date(stat_date):
            events.extend(self._diff_one(snapshot, stat_date))
        return self._persist(events)

    def _persist(self, events: list[ChangeEvent]) -> list[ChangeEvent]:
        """把事件落库，并返回**本次真正新增**的那部分。

        落库依赖 ``ux_ce_site_type_date`` 唯一索引 + ``ON CONFLICT DO NOTHING``，
        重复项会被静默跳过。但调用方（调度器 / 邮件通知）拿到的是本函数返回值，
        所以必须把「已存在」的条目剔掉，否则同一天重复跑就会重复推送。

        Args:
            events: 本次内存生成的事件列表。

        Returns:
            实际新入库的事件列表（顺序与入参一致）。
        """
        if not events:
            return []
        # 理论上同一批次内 stat_date 一致；按出现的日期逐一取已存在键，兼容混日期。
        existing: set[tuple[int, str, str]] = set()
        for date_key in {str(event.stat_date) for event in events}:
            for item in self.repos.events.list_by_date(date_key):
                existing.add((int(item.site_id), str(item.event_type), str(item.stat_date)))
        fresh = [
            event
            for event in events
            if (int(event.site_id), str(event.event_type), str(event.stat_date)) not in existing
        ]
        if fresh:
            self.repos.events.insert_many(fresh)
        return fresh

    def diff_week(self, stat_date: str | None = None) -> list[ChangeEvent]:
        """与上周同日快照做 diff（**只产出不落库**，用于报告对比列）。

        Args:
            stat_date: 日期键。

        Returns:
            事件列表（内存态，不落库）。
        """
        stat_date = stat_date or today_str(self.settings.timezone_name())
        week_ago = shift_days(stat_date, -7)
        events: list[ChangeEvent] = []
        for snapshot in self.repos.snapshots.by_date(stat_date):
            previous = self.repos.snapshots.get(int(snapshot.site_id), week_ago)
            if previous is None:
                continue
            if self._status_changed(previous.index_status, snapshot.index_status):
                events.append(
                    self._event(
                        int(snapshot.site_id),
                        EventType.NEW_INDEXED.value
                        if str(snapshot.index_status) == IndexStatus.INDEXED.value
                        else EventType.LOST_INDEXED.value,
                        str(previous.index_status),
                        str(snapshot.index_status),
                        stat_date,
                        {"compare": "week", "previous_date": week_ago},
                    )
                )
        return events

    def check_unknown_persistent(self, stat_date: str | None = None) -> list[ChangeEvent]:
        """检测连续多天 unknown 的站点，生成 ``unknown_persistent`` 事件并落库。

        Args:
            stat_date: 日期键。

        Returns:
            生成的事件列表。
        """
        stat_date = stat_date or today_str(self.settings.timezone_name())
        threshold = int(getattr(self.settings.alert, "unknown_persistent_days", 3) or 3)
        events: list[ChangeEvent] = []
        for snapshot in self.repos.snapshots.by_date(stat_date):
            if str(snapshot.index_status) != IndexStatus.UNKNOWN.value:
                continue
            streak = self.repos.snapshots.count_unknown_streak(
                int(snapshot.site_id), stat_date, max_days=threshold + 2
            ) + 1  # 含今日
            if streak >= threshold:
                existing = self.repos.snapshots.previous(int(snapshot.site_id), stat_date)
                _ = existing
                events.append(
                    self._event(
                        int(snapshot.site_id),
                        EventType.UNKNOWN_PERSISTENT.value,
                        "unknown_need_manual_check",
                        "unknown_need_manual_check",
                        stat_date,
                        {"streak_days": streak, "threshold": threshold},
                    )
                )
        return self._persist(events)

    def check_health_events(self, stat_date: str | None = None) -> list[ChangeEvent]:
        """基于最新检测结果生成健康类事件（SSL / HTTP / 重定向 / DNS / TDK）。

        Args:
            stat_date: 日期键。

        Returns:
            生成的事件列表（已落库）。
        """
        stat_date = stat_date or today_str(self.settings.timezone_name())
        events: list[ChangeEvent] = []
        results = self.repos.results.latest_all()
        previous_rows: dict[int, CheckResult] = {}

        for result in results:
            site_id = int(result.site_id or 0)
            history = previous_rows.get(site_id)
            if history is None:
                history = self._second_last(site_id)
                previous_rows[site_id] = history
            if not site_id:
                continue
            # DNS / HTTP 异常
            if str(result.error_type) == ErrorType.DNS_FAILED.value:
                events.append(
                    self._event(site_id, EventType.DNS_FAILED.value, "", "", stat_date,
                                {"domain_error": "DNS 解析失败"})
                )
            http_status = result.http_status
            if isinstance(http_status, int) and http_status >= 400:
                events.append(
                    self._event(site_id, EventType.HTTP_ANOMALY.value, "", str(http_status), stat_date,
                                {"http_status": http_status})
                )
            if result.anomalies and result.anomalies.get("redirect_anomaly"):
                events.append(
                    self._event(site_id, EventType.REDIRECT_ANOMALY.value, "", result.final_url, stat_date,
                                {"hops": result.anomalies.get("redirect_hops"),
                                 "cross_domain": bool(result.anomalies.get("cross_domain")),
                                 "https_downgrade": bool(result.anomalies.get("https_downgrade"))})
                )
            # TDK 变更（与前一次对比 title_hash）
            if history is not None and result.tdk.title_hash and history.tdk.title_hash:
                if result.tdk.title_hash != history.tdk.title_hash:
                    events.append(
                        self._event(site_id, EventType.TDK_CHANGED.value,
                                    history.tdk.title or "",
                                    result.tdk.title or "", stat_date,
                                    {"previous_title": history.tdk.title,
                                     "current_title": result.tdk.title})
                    )
        return self._persist(events)

    def _second_last(self, site_id: int) -> CheckResult | None:
        """取上一次（倒数第二条）检测结果，用于 TDK 变更比对。

        Args:
            site_id: 站点 id。

        Returns:
            检测结果或 None。
        """
        items = self.repos.results.history(site_id, limit=2)
        return items[1] if len(items) > 1 else None

    def _diff_one(self, snapshot: DailySnapshot, stat_date: str) -> list[ChangeEvent]:
        """对单个站点做昨日 diff（内部方法）。

        Args:
            snapshot: 今日快照。
            stat_date: 日期键。

        Returns:
            事件列表（可能为空）。
        """
        site_id = int(snapshot.site_id)
        # 基线快照本身不产生事件（首次零误报），但**可以作为后续比对的参照物**：
        # 否则站点首日建基线后，第 2 天永远 diff 不出变化，要等到第 3 天才生效。
        # 因此这里 skip_baseline=False；若今日快照本身是基线，说明之前没有任何
        # 历史快照，previous() 自然返回 None，仍然零误报。
        previous = self.repos.snapshots.previous(site_id, stat_date, skip_baseline=False)
        if previous is None:
            # 无历史（含仅有基线）→ 不产生任何 diff 事件（首次零误报）
            return []
        old_status = str(previous.index_status)
        new_status = str(snapshot.index_status)
        # unknown 不参与 diff
        if IndexStatus.UNKNOWN.value in (old_status, new_status):
            return []
        if not self._status_changed(old_status, new_status):
            return []
        event_type = (
            EventType.NEW_INDEXED.value
            if new_status == IndexStatus.INDEXED.value
            else EventType.LOST_INDEXED.value
        )
        return [
            self._event(
                site_id,
                event_type,
                old_status,
                new_status,
                stat_date,
                {
                    "previous_date": str(previous.stat_date),
                    "http_status": snapshot.http_status,
                    "evidence_note": "由每日快照 diff 产生",
                },
            )
        ]

    @staticmethod
    def _status_changed(old_status: str, new_status: str) -> bool:
        """判断两个收录状态之间是否构成「新增/丢失」。

        Args:
            old_status: 旧状态。
            new_status: 新状态。

        Returns:
            构成变化返回 True。
        """
        return str(old_status) != str(new_status)

    def _event(
        self,
        site_id: int,
        event_type: str,
        old_value: str,
        new_value: str,
        stat_date: str,
        detail: dict[str, Any] | None = None,
    ) -> ChangeEvent:
        """构造事件实体（统一 ``created_at`` 与时区无关的日期键）。

        Args:
            site_id: 站点 id。
            event_type: 事件类型。
            old_value: 旧值。
            new_value: 新值。
            stat_date: 日期键。
            detail: 附加详情。

        Returns:
            :class:`ChangeEvent`。
        """
        return ChangeEvent(
            site_id=site_id,
            event_type=event_type,
            old_value=old_value,
            new_value=new_value,
            stat_date=stat_date,
            detail=detail or {},
            created_at=utcnow_iso(),
        )

    def summary_counts(self, stat_date: str) -> dict[str, int]:
        """返回某天各事件类型的数量。

        Args:
            stat_date: 日期键。

        Returns:
            类型到数量的字典。
        """
        return self.repos.events.counts_by_type(stat_date)

    def ensure_dates(self, days: int, end_date: str | None = None) -> list[str]:
        """返回需要检查的日期键列表（升序），便于补跑历史。

        Args:
            days: 天数。
            end_date: 结束日期。

        Returns:
            日期键列表。
        """
        end = end_date or today_str(self.settings.timezone_name())
        return date_range(end, days)

    def snapshot_of(self, site_id: int, dates: Iterable[str]) -> dict[str, DailySnapshot]:
        """批量读取某个站点在多个日期的快照。

        Args:
            site_id: 站点 id。
            dates: 日期键列表。

        Returns:
            ``{stat_date: DailySnapshot}``。
        """
        result: dict[str, DailySnapshot] = {}
        for date_key in dates:
            snapshot = self.repos.snapshots.get(site_id, date_key)
            if snapshot is not None:
                result[date_key] = snapshot
        return result
