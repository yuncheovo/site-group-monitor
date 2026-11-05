"""每日报告聚合、导出与数据清理（R-006 / R-012 / R-015 / R-021）。

产出 :class:`DailyReport`：
    * 收录率（分子分母 **排除 unknown**）与较昨日变化；
    * 新增收录 / 掉收录 / 查询失败 / 异常四类清单；
    * 30 天趋势与 diff 柱状数据；
    * 全站明细行（供 Excel「明细」sheet 与 HTML 表格使用）。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Iterable, Sequence

from site_group_monitor.models.entities import ChangeEvent, DailyReport, OverviewCard
from site_group_monitor.models.enums import ErrorType, EventType, IndexStatus, RunStatus
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.utils.timeutil import fmt_local, shift_days, today_str, utcnow_iso


class ReportService:
    """报告聚合与导出服务。"""

    def __init__(
        self,
        repos: Repositories,
        settings: Any,
        logger: Any | None = None,
        html_writer: Any | None = None,
        excel_writer: Any | None = None,
    ) -> None:
        """构造报告服务。

        Args:
            repos: 仓储容器。
            settings: 配置对象。
            logger: 日志记录器。
            html_writer: HTML 报告写出器（懒加载）。
            excel_writer: Excel 导出器（懒加载）。
        """
        self.repos = repos
        self.settings = settings
        self.logger = logger
        # 注意：缓存变量不能与下面的 _get_*_writer 方法同名，否则实例属性会遮蔽方法
        # （曾导致 "'NoneType' object is not callable"）。
        self._html_writer_cache = html_writer
        self._excel_writer_cache = excel_writer

    # ================================================================ 聚合
    def build_daily(self, stat_date: str | None = None) -> DailyReport:
        """构建某天的完整报告聚合体。

        Args:
            stat_date: 日期键，缺省今日。

        Returns:
            :class:`DailyReport`。
        """
        stat_date = stat_date or today_str(self.settings.timezone_name())
        report = DailyReport(stat_date=stat_date, generated_at=utcnow_iso())
        # 代理池统计需在报告备注前拿到（下方「代理池为空」提示依赖它）
        proxy_stats = self.repos.proxies.stats()

        snapshots = self.repos.snapshots.by_date(stat_date)
        report.total_sites = len(snapshots)
        for snapshot in snapshots:
            status = str(snapshot.index_status)
            if status == IndexStatus.INDEXED.value:
                report.indexed_count += 1
            elif status == IndexStatus.NOT_INDEXED.value:
                report.not_indexed_count += 1
            else:
                report.unknown_count += 1
        checkable = report.checkable_count()
        report.index_rate = round(report.indexed_count / checkable, 4) if checkable else 0.0

        # 较昨日
        trend = self.repos.snapshots.trend(days=31, end_date=stat_date)
        report.trend = trend
        for point in trend:
            if str(point.get("stat_date")) == shift_days(stat_date, -1):
                report.prev_rate = float(point.get("rate", 0.0) or 0.0)
                break

        # 站点映射（域名 / 分组）
        sites = {int(site.id or 0): site for site in self._all_sites()}
        latest_map = self.repos.results.latest_map([int(site_id) for site_id in sites])

        # 事件分桶
        events = self.repos.events.list_by_date(stat_date)
        for event in events:
            payload = self._event_payload(event, sites)
            if event.event_type == EventType.NEW_INDEXED.value:
                report.new_indexed.append(payload)
            elif event.event_type == EventType.LOST_INDEXED.value:
                report.lost_indexed.append(payload)
            elif event.event_type == EventType.UNKNOWN_PERSISTENT.value:
                report.need_manual.append(payload)
            else:
                payload["kind"] = str(event.event_type)
                report.anomalies.append(payload)
        report.ssl_expiring_count = sum(
            1 for item in report.anomalies if item.get("kind") == EventType.SSL_EXPIRING.value
        )
        report.anomalous_count = len(
            {int(item.get("site_id") or 0) for item in report.anomalies}
        )

        # 待人工确认清单（今日 unknown 快照）
        unknown_ids = [
            int(snapshot.site_id) for snapshot in snapshots
            if str(snapshot.index_status) == IndexStatus.UNKNOWN.value
        ]
        for site_id in unknown_ids:
            if any(int(item.get("site_id") or 0) == site_id for item in report.need_manual):
                continue
            latest = latest_map.get(site_id)
            site = sites.get(site_id)
            report.need_manual.append(
                {
                    "site_id": site_id,
                    "domain": site.domain if site else "",
                    "group_name": site.group_name if site else "",
                    "reason": latest.index_reason if latest else "",
                    "evidence_path": latest.evidence_path if latest else "",
                    "detail": {"need_manual": True},
                }
            )

        # 基线建立清单
        report.baseline_created = [
            {
                "site_id": int(snapshot.site_id),
                "domain": sites.get(int(snapshot.site_id)).domain
                if sites.get(int(snapshot.site_id)) else "",
                "stat_date": snapshot.stat_date,
            }
            for snapshot in snapshots
            if snapshot.is_baseline
        ]

        # 明细行（供导出）
        report.rows = self._build_rows(snapshots, sites, latest_map)

        # 备注（如代理池为空提示）
        report.notes = []
        if proxy_stats.get("empty"):
            report.notes.append("代理池为空：当前采用直连模式，检测速率受限且更易触发验证码。")
        if report.baseline_created:
            report.notes.append(
                f"已为 {len(report.baseline_created)} 个站点建立基线，首次运行不产生任何 diff 事件。"
            )
        if report.unknown_count:
            report.notes.append(
                f"今日 {report.unknown_count} 个站点为「查询失败」(验证码/网络/解析失败)，"
                "不计入收录率分子分母，也不参与收录变化统计。"
            )
        return report

    def _event_payload(self, event: ChangeEvent, sites: dict[int, Any]) -> dict[str, Any]:
        """把事件转换为报告展示用的字典。

        Args:
            event: 事件实体。
            sites: 站点映射。

        Returns:
            展示字典。
        """
        site = sites.get(int(event.site_id or 0))
        return {
            "site_id": int(event.site_id or 0),
            "domain": site.domain if site else "",
            "group_name": site.group_name if site else "",
            "event_type": str(event.event_type),
            "old_value": str(event.old_value),
            "new_value": str(event.new_value),
            "detail": dict(event.detail or {}),
        }

    def _build_rows(
        self,
        snapshots: Sequence[Any],
        sites: dict[int, Any],
        latest_map: dict[int, Any],
    ) -> list[dict[str, Any]]:
        """构造明细行列表（导出用）。

        Args:
            snapshots: 当日快照。
            sites: 站点映射。
            latest_map: 最新结果映射。

        Returns:
            明细字典列表。
        """
        rows: list[dict[str, Any]] = []
        for snapshot in snapshots:
            site_id = int(snapshot.site_id)
            site = sites.get(site_id)
            latest = latest_map.get(site_id)
            rows.append(
                {
                    "site_id": site_id,
                    "domain": site.domain if site else "",
                    "group_name": site.group_name if site else "",
                    "tags": site.tags if site else [],
                    "index_status": str(snapshot.index_status),
                    "index_reason": latest.index_reason if latest else "",
                    "index_result_count": latest.index_result_count if latest else 0,
                    "index_top_url": latest.index_top_url if latest else "",
                    "http_status": snapshot.http_status,
                    "response_ms": snapshot.response_ms,
                    "ssl_days_left": snapshot.ssl_days_left,
                    "title": snapshot.title,
                    "description": latest.tdk.description if latest and latest.tdk else "",
                    "keywords": latest.tdk.keywords if latest and latest.tdk else "",
                    "final_url": latest.final_url if latest else "",
                    "evidence_path": latest.evidence_path if latest else "",
                    "error_type": latest.error_type if latest else ErrorType.NONE.value,
                    "error_msg": latest.error_msg if latest else "",
                    "checked_at": fmt_local(
                        latest.checked_at if latest else snapshot.created_at,
                        self.settings.timezone_name(),
                    ),
                }
            )
        return sorted(rows, key=lambda item: str(item.get("domain") or ""))

    def _all_sites(self) -> list[Any]:
        """读取全部站点（带缓存友好的单次查询）。"""
        return self.repos.sites.list_all()

    # ================================================================ 导出
    def save(self, stat_date: str | None = None, fmt: str = "all") -> dict[str, str]:
        """导出报告文件到 ``data/reports``。

        Args:
            stat_date: 日期键。
            fmt: ``html`` / ``xlsx`` / ``all``。

        Returns:
            ``{"html": path, "xlsx": path}``（按 fmt 决定内容）。
        """
        stat_date = stat_date or today_str(self.settings.timezone_name())
        report = self.build_daily(stat_date)
        out_dir = Path(self.settings.reports_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths: dict[str, str] = {}
        if fmt in ("html", "all"):
            html_path = out_dir / f"report-{stat_date}.html"
            paths["html"] = self._get_html_writer().write(report, html_path)
        if fmt in ("xlsx", "all"):
            xlsx_path = out_dir / f"report-{stat_date}.xlsx"
            paths["xlsx"] = self._get_excel_writer().write(report, xlsx_path)
        if self.logger is not None:
            self.logger.info("report: %s 报告已导出 %s", stat_date, paths)
        return paths

    def _get_html_writer(self) -> Any:
        """惰性构造 HTML 写出器。"""
        if self._html_writer_cache is None:
            from site_group_monitor.report.html_report import HtmlReportWriter

            self._html_writer_cache = HtmlReportWriter(self.settings, self.logger)
        return self._html_writer_cache

    def _get_excel_writer(self) -> Any:
        """惰性构造 Excel 写出器。"""
        if self._excel_writer_cache is None:
            from site_group_monitor.report.excel_report import ExcelReportWriter

            self._excel_writer_cache = ExcelReportWriter(self.settings, self.logger)
        return self._excel_writer_cache

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        """列出已生成的报告文件。

        Args:
            limit: 条数上限。

        Returns:
            报告文件元信息列表（按日期倒序）。
        """
        base = Path(self.settings.reports_dir)
        if not base.is_dir():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(base.glob("report-*.*"), reverse=True)[: max(1, limit)]:
            items.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "fmt": "xlsx" if path.suffix == ".xlsx" else "html",
                    "size_bytes": path.stat().st_size,
                    "date": path.stem.replace("report-", ""),
                }
            )
        return items

    # ================================================================ 总览卡片
    def overview(self) -> OverviewCard:
        """构建仪表盘总览卡片数据（对应 PRD §5 五张卡）。

        Returns:
            :class:`OverviewCard`。
        """
        card = OverviewCard(generated_at=utcnow_iso())
        card.total_sites = self.repos.sites.count_all()
        card.enabled_sites = self.repos.sites.count_enabled()

        latest_date = self.repos.snapshots.latest_date()
        if latest_date is None:
            # 无快照时用最新检测结果兜底
            results = self.repos.results.latest_all()
            card.indexed_count = sum(1 for item in results if str(item.index_status) == IndexStatus.INDEXED.value)
            card.not_indexed_count = sum(
                1 for item in results if str(item.index_status) == IndexStatus.NOT_INDEXED.value
            )
            card.unknown_count = sum(1 for item in results if str(item.index_status) == IndexStatus.UNKNOWN.value)
        else:
            snapshots = self.repos.snapshots.by_date(latest_date)
            card.indexed_count = sum(
                1 for item in snapshots if str(item.index_status) == IndexStatus.INDEXED.value
            )
            card.not_indexed_count = sum(
                1 for item in snapshots if str(item.index_status) == IndexStatus.NOT_INDEXED.value
            )
            card.unknown_count = sum(
                1 for item in snapshots if str(item.index_status) == IndexStatus.UNKNOWN.value
            )
        checkable = card.indexed_count + card.not_indexed_count
        card.index_rate = round(card.indexed_count / checkable, 4) if checkable else 0.0
        counted = card.indexed_count + card.not_indexed_count + card.unknown_count
        card.unchecked_count = max(0, int(card.total_sites) - counted)

        # 较昨日
        trend = self.repos.snapshots.trend(days=2)
        if len(trend) >= 2:
            today_rate = float(trend[-1].get("rate", 0.0))
            prev_rate = float(trend[-2].get("rate", 0.0))
            card.delta_vs_yesterday = today_rate - prev_rate
        if latest_date:
            card.anomalous_count = sum(
                count for event_type, count in self.repos.events.counts_by_type(latest_date).items()
                if event_type in (
                    EventType.HTTP_ANOMALY.value,
                    EventType.REDIRECT_ANOMALY.value,
                    EventType.DNS_FAILED.value,
                )
            )
            card.ssl_expiring_count = self.repos.events.counts_by_type(latest_date).get(
                EventType.SSL_EXPIRING.value, 0
            )

        proxy_stats = self.repos.proxies.stats()
        card.proxy_total = int(proxy_stats.get("total", 0))
        card.proxy_enabled = int(proxy_stats.get("enabled", 0))
        card.proxy_health_rate = float(proxy_stats.get("health_rate", 0.0))
        card.proxy_empty = bool(proxy_stats.get("empty", True))

        last_run = self.repos.runs.latest()
        card.last_run = last_run or {}
        if card.proxy_empty:
            card.last_run = {**(card.last_run or {}), "proxy_warning": "代理池为空，已降级直连"}
        return card

    # ================================================================ 清理与备份
    def cleanup(self, days: int | None = None) -> dict[str, int]:
        """按保留期清理过期数据 + 证据文件 + 备份轮换。

        Args:
            days: 覆盖保留天数。

        Returns:
            各类清理条数统计。
        """
        retention_days = int(days or getattr(self.settings.retention, "days", 180) or 180)
        evidence_days = int(getattr(self.settings.retention, "evidence_days", 30) or 30)
        audit_days = int(getattr(self.settings.retention, "audit_days", 365) or 365)
        today = today_str(self.settings.timezone_name())
        cutoff_date = shift_days(today, -retention_days)
        stats = {
            "check_results": self.repos.results.delete_before(f"{cutoff_date}T00:00:00Z"),
            "daily_snapshots": self.repos.snapshots.delete_before(cutoff_date),
            "change_events": self.repos.events.delete_before(cutoff_date),
            "audit_logs": self.repos.audit.delete_before(
                shift_days(today, -audit_days) + "T00:00:00Z"
            ),
            "evidence": self._clean_evidence(evidence_days),
        }
        if self.logger is not None:
            self.logger.info("report: 保留期清理完成 %s", stats)
        return stats

    def _clean_evidence(self, keep_days: int) -> int:
        """清理过期证据目录（按域名子目录的 mtime 判断）。

        Args:
            keep_days: 保留天数。

        Returns:
            删除的文件数。
        """
        base = Path(self.settings.evidence_dir)
        if not base.is_dir():
            return 0
        import time

        threshold = time.time() - keep_days * 86400
        removed = 0
        for path in base.rglob("*.gz"):
            try:
                if path.stat().st_mtime < threshold:
                    path.unlink()
                    removed += 1
            except OSError:  # pragma: no cover
                continue
        for path in base.rglob("*.meta.json"):
            try:
                if path.stat().st_mtime < threshold:
                    path.unlink()
            except OSError:  # pragma: no cover
                continue
        return removed

    def backup(self) -> str:
        """执行一次数据库备份（``VACUUM INTO``），并按 ``keep`` 轮换。

        Returns:
            备份文件路径；未启用备份返回空串。
        """
        if not getattr(self.settings.storage.backup, "enabled", True):
            return ""
        backups_dir = Path(self.settings.backups_dir)
        backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = today_str(self.settings.timezone_name()).replace("-", "")
        target = backups_dir / f"sgm-{stamp}.db"
        path = self.repos.db.backup_to(target)
        keep = max(1, int(getattr(self.settings.storage.backup, "keep", 7) or 7))
        existing = sorted(backups_dir.glob("sgm-*.db"), reverse=True)
        for old in existing[keep:]:
            try:
                old.unlink()
            except OSError:  # pragma: no cover
                continue
        return str(path)

    def purge_reports(self, keep_days: int | None = None) -> int:
        """清理过期报告文件。

        Args:
            keep_days: 保留天数，缺省取配置 ``report.keep_days``。

        Returns:
            删除文件数。
        """
        limit_days = int(keep_days or getattr(self.settings.report, "keep_days", 365) or 365)
        base = Path(self.settings.reports_dir)
        if not base.is_dir():
            return 0
        import time

        threshold = time.time() - limit_days * 86400
        removed = 0
        for path in base.glob("report-*"):
            try:
                if path.stat().st_mtime < threshold:
                    if path.is_file():
                        path.unlink()
                    else:
                        shutil.rmtree(path, ignore_errors=True)
                    removed += 1
            except OSError:  # pragma: no cover
                continue
        return removed

    def last_run_status(self) -> str:
        """返回最近一轮运行状态字符串。

        Returns:
            状态字符串；无记录返回 ``idle``。
        """
        run = self.repos.runs.latest()
        if not run:
            return "idle"
        return str(run.get("status", RunStatus.SUCCESS.value))


def report_to_context(report: DailyReport, settings: Any) -> dict[str, Any]:
    """把 :class:`DailyReport` 转成模板友好字典。

    Args:
        report: 报告聚合体。
        settings: 配置对象。

    Returns:
        模板上下文字典。
    """
    return {
        "report": report,
        "index_rate_percent": round(report.index_rate * 100, 2),
        "prev_rate_percent": round((report.prev_rate or 0) * 100, 2),
        "trend": report.trend,
        "timezone": settings.timezone_name(),
    }


def iter_events(events: Iterable[ChangeEvent]) -> list[dict[str, Any]]:
    """把事件列表转为字典列表（供 JSON 序列化）。

    Args:
        events: 事件迭代器。

    Returns:
        字典列表。
    """
    return [item.model_dump() for item in events]
