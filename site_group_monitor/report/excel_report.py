"""Excel 报告导出（openpyxl，多 Sheet）。

Sheet 结构：
    1. 总览：关键指标与计数；
    2. 明细：全站当日数据（域名/分组/收录/HTTP/响应/TDK/证据）；
    3. 变化：新增收录 + 掉收录 + 查询失败；
    4. 查询失败：验证码/网络/解析失败站点清单。

另在有趋势数据时追加「趋势」sheet（近 30 天收录率）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from site_group_monitor.models.entities import DailyReport

_HEADER_FILL = PatternFill("solid", fgColor="F2F2F2")
_HEADER_FONT = Font(bold=True, color="333333")
_STATUS_FILLS = {
    "indexed": PatternFill("solid", fgColor="E3F2E1"),
    "not_indexed": PatternFill("solid", fgColor="FBE4E4"),
    "unknown_need_manual_check": PatternFill("solid", fgColor="FFF6DA"),
}


class ExcelReportWriter:
    """Excel 导出器。"""

    def __init__(self, settings: Any, logger: Any | None = None) -> None:
        """构造导出器。

        Args:
            settings: 配置对象。
            logger: 日志记录器。
        """
        self.settings = settings
        self.logger = logger

    def write(self, report: DailyReport, path: str | Path) -> str:
        """导出 Excel 报告。

        Args:
            report: 日报聚合体。
            path: 目标路径。

        Returns:
            实际写入的绝对路径字符串。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        workbook = Workbook()

        overview_sheet = workbook.active
        overview_sheet.title = "总览"
        self._write_overview(overview_sheet, report)

        detail_sheet = workbook.create_sheet("明细")
        self._write_detail(detail_sheet, report)

        change_sheet = workbook.create_sheet("变化")
        self._write_changes(change_sheet, report)

        manual_sheet = workbook.create_sheet("查询失败")
        self._write_manual(manual_sheet, report)

        if report.trend:
            trend_sheet = workbook.create_sheet("趋势")
            self._write_trend(trend_sheet, report.trend)

        workbook.save(str(target))
        if self.logger is not None:
            self.logger.info("excel_report: 已生成 %s（%d 个 sheet）", target, len(workbook.sheetnames))
        return str(target)

    # ---------------------------------------------------------------- 各 Sheet
    def _write_overview(self, sheet: Any, report: DailyReport) -> None:
        """写入总览 sheet。

        Args:
            sheet: 工作表。
            report: 日报。
        """
        rows: list[tuple[str, Any]] = [
            ("统计日期", report.stat_date),
            ("生成时间(UTC)", report.generated_at),
            ("监控站点总数", report.total_sites),
            ("已收录", report.indexed_count),
            ("未收录", report.not_indexed_count),
            ("查询失败", report.unknown_count),
            ("收录率(排除unknown)", f"{report.index_rate * 100:.2f}%"),
            ("昨日收录率", "-" if report.prev_rate is None else f"{report.prev_rate * 100:.2f}%"),
            ("新增收录", len(report.new_indexed)),
            ("掉收录", len(report.lost_indexed)),
            ("查询失败清单数", len(report.need_manual)),
            ("异常事件数", len(report.anomalies)),
            ("建立基线数", len(report.baseline_created)),
        ]
        self._write_header(sheet, ["指标", "值"])
        for index, (key, value) in enumerate(rows, start=2):
            sheet.cell(row=index, column=1, value=key)
            sheet.cell(row=index, column=2, value=value)
        sheet.column_dimensions["A"].width = 26
        sheet.column_dimensions["B"].width = 40
        note_row = len(rows) + 3
        for offset, note in enumerate(report.notes):
            sheet.cell(row=note_row + offset, column=1, value="说明")
            sheet.cell(row=note_row + offset, column=2, value=note)

    def _write_detail(self, sheet: Any, report: DailyReport) -> None:
        """写入明细 sheet。

        Args:
            sheet: 工作表。
            report: 日报。
        """
        headers = [
            "域名", "分组", "标签", "收录状态", "原因码", "结果数", "结果示例",
            "HTTP状态", "响应(ms)", "Title", "Description", "Keywords",
            "最终URL", "证据路径", "检测时间",
        ]
        self._write_header(sheet, headers)
        for index, row in enumerate(report.rows, start=2):
            values = [
                row.get("domain", ""),
                row.get("group_name", ""),
                "|".join(row.get("tags") or []),
                row.get("index_status", ""),
                row.get("index_reason", ""),
                row.get("index_result_count", 0),
                row.get("index_top_url", ""),
                row.get("http_status", ""),
                row.get("response_ms", ""),
                row.get("title", ""),
                row.get("description", ""),
                row.get("keywords", ""),
                row.get("final_url", ""),
                row.get("evidence_path", ""),
                row.get("checked_at", ""),
            ]
            for column, value in enumerate(values, start=1):
                sheet.cell(row=index, column=column, value=value)
            status = str(row.get("index_status") or "")
            fill = _STATUS_FILLS.get(status)
            if fill is not None:
                sheet.cell(row=index, column=4).fill = fill
        self._autosize(sheet, headers, minimum=12, maximum=48)

    def _write_changes(self, sheet: Any, report: DailyReport) -> None:
        """写入变化 sheet。

        Args:
            sheet: 工作表。
            report: 日报。
        """
        self._write_header(sheet, ["类型", "域名", "分组", "旧值", "新值", "详情"])
        row_index = 2
        for kind, items in (
            ("新增收录", report.new_indexed),
            ("掉收录", report.lost_indexed),
            ("异常/健康", report.anomalies),
        ):
            for item in items:
                values = [
                    kind,
                    item.get("domain", ""),
                    item.get("group_name", ""),
                    item.get("old_value", ""),
                    item.get("new_value", ""),
                    str(item.get("detail") or {}),
                ]
                for column, value in enumerate(values, start=1):
                    sheet.cell(row=row_index, column=column, value=value)
                row_index += 1
        self._autosize(sheet, ["类型", "域名", "分组", "旧值", "新值", "详情"])

    def _write_manual(self, sheet: Any, report: DailyReport) -> None:
        """写入查询失败 sheet。

        Args:
            sheet: 工作表。
            report: 日报。
        """
        self._write_header(sheet, ["域名", "分组", "原因码", "证据路径", "备注"])
        for index, item in enumerate(report.need_manual, start=2):
            values = [
                item.get("domain", ""),
                item.get("group_name", ""),
                item.get("reason", "") or item.get("event_type", ""),
                item.get("evidence_path", "") or "-",
                "验证码/网络/解析失败，未能得出收录结论",
            ]
            for column, value in enumerate(values, start=1):
                sheet.cell(row=index, column=column, value=value)
        self._autosize(sheet, ["域名", "分组", "原因码", "证据路径", "备注"])

    def _write_trend(self, sheet: Any, trend: Sequence[dict[str, Any]]) -> None:
        """写入趋势 sheet。

        Args:
            sheet: 工作表。
            trend: 趋势数据。
        """
        self._write_header(sheet, ["日期", "总快照", "已收录", "未收录", "查询失败", "收录率"])
        for index, point in enumerate(trend, start=2):
            values = [
                point.get("stat_date", ""),
                point.get("total", 0),
                point.get("indexed", 0),
                point.get("not_indexed", 0),
                point.get("unknown", 0),
                f"{float(point.get('rate') or 0) * 100:.2f}%",
            ]
            for column, value in enumerate(values, start=1):
                sheet.cell(row=index, column=column, value=value)
        self._autosize(sheet, ["日期", "总快照", "已收录", "未收录", "查询失败", "收录率"])

    # ---------------------------------------------------------------- 辅助
    @staticmethod
    def _write_header(sheet: Any, headers: Sequence[str]) -> None:
        """写入表头（带样式）。

        Args:
            sheet: 工作表。
            headers: 表头列表。
        """
        for column, name in enumerate(headers, start=1):
            cell = sheet.cell(row=1, column=column, value=name)
            cell.font = _HEADER_FONT
            cell.fill = _HEADER_FILL
            cell.alignment = Alignment(horizontal="left", vertical="center")
        sheet.freeze_panes = "A2"

    @staticmethod
    def _autosize(sheet: Any, headers: Sequence[str], minimum: int = 10, maximum: int = 50) -> None:
        """根据表头与内容自动调整列宽。

        Args:
            sheet: 工作表。
            headers: 表头列表。
            minimum: 最小宽度。
            maximum: 最大宽度。
        """
        for column_index, _ in enumerate(headers, start=1):
            letter = get_column_letter(column_index)
            longest = 0
            for cell in sheet[letter]:
                text = "" if cell.value is None else str(cell.value)
                longest = max(longest, len(text))
            width = max(minimum, min(maximum, longest + 4))
            sheet.column_dimensions[letter].width = width
