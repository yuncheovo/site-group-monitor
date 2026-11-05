"""离线 HTML 报告导出（单文件、可直接双击打开）。

特点：
    * Jinja2 模板渲染，模板在 ``report/templates/report.html.j2``；
    * **趋势曲线用内联 SVG**（不依赖外部 CDN / echarts），保证离线可用；
    * 报告内含：总览卡片、趋势、新增/掉收录/查询失败/异常清单、全站明细表格。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from site_group_monitor.models.entities import DailyReport

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


class HtmlReportWriter:
    """HTML 报告写出器。"""

    def __init__(self, settings: Any, logger: Any | None = None) -> None:
        """构造写出器。

        Args:
            settings: 配置对象。
            logger: 日志记录器。
        """
        self.settings = settings
        self.logger = logger
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "j2"]),
            undefined=StrictUndefined,
        )
        self.env.filters["pct"] = _fmt_pct
        self.env.filters["num"] = _fmt_num

    def render(self, report: DailyReport) -> str:
        """渲染报告 HTML 字符串。

        Args:
            report: 日报聚合体。

        Returns:
            HTML 文本。
        """
        template = self.env.get_template("report.html.j2")
        context = {
            "report": report,
            "trend_svg": trend_svg(report.trend),
            "index_rate_pct": round(report.index_rate * 100, 2),
            "prev_rate_pct": round((report.prev_rate or 0) * 100, 2),
            "timezone": self.settings.timezone_name(),
            "generated_at": report.generated_at,
        }
        return template.render(**context)

    def write(self, report: DailyReport, path: str | Path) -> str:
        """把报告写入文件。

        Args:
            report: 日报聚合体。
            path: 目标路径。

        Returns:
            实际写入的绝对路径字符串。
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        html = self.render(report)
        target.write_text(html, encoding="utf-8")
        if self.logger is not None:
            self.logger.info("html_report: 已生成 %s (%d 字节)", target, len(html))
        return str(target)


# ================================================================ 辅助
def _fmt_pct(value: Any) -> str:
    """把 0-1 的比例格式化为百分比字符串。

    Args:
        value: 比例。

    Returns:
        形如 ``87.30%`` 的字符串。
    """
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_num(value: Any) -> str:
    """数字千分位格式化。

    Args:
        value: 数值。

    Returns:
        格式化字符串。
    """
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def trend_svg(trend: Sequence[dict[str, Any]], width: int = 900, height: int = 260) -> str:
    """生成趋势折线的内联 SVG（不依赖外部库）。

    Args:
        trend: ``[{"stat_date","rate",...}]``。
        width: SVG 宽度。
        height: SVG 高度。

    Returns:
        SVG 字符串；数据不足返回空串。
    """
    points = [item for item in (trend or []) if item.get("rate") is not None]
    if len(points) < 2:
        return ""
    padding = 40
    inner_w = width - padding * 2
    inner_h = height - padding * 2
    max_rate = max(float(item.get("rate") or 0) for item in points)
    min_rate = min(float(item.get("rate") or 0) for item in points)
    span = max(0.05, max_rate - min_rate)
    step = inner_w / max(1, len(points) - 1)

    def x_pos(index: int) -> float:
        return padding + index * step

    def y_pos(rate: float) -> float:
        ratio = (rate - min_rate) / span
        return padding + inner_h - ratio * inner_h

    polyline = " ".join(
        f"{x_pos(index):.1f},{y_pos(float(item.get('rate') or 0)):.1f}"
        for index, item in enumerate(points)
    )
    area = f"{padding},{height - padding} {polyline} {padding + inner_w:.1f},{height - padding}"
    max_label = f"{max_rate * 100:.1f}%"
    min_label = f"{min_rate * 100:.1f}%"
    ticks = ""
    label_step = max(1, len(points) // 6)
    for index, item in enumerate(points):
        if index % label_step and index != len(points) - 1:
            continue
        ticks += (
            f'<text x="{x_pos(index):.1f}" y="{height - 12}" font-size="10" fill="#888">'
            f"{str(item.get('stat_date'))[5:]}</text>"
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="auto" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{width}" height="{height}" fill="#fff"/>'
        f'<line x1="{padding}" y1="{padding}" x2="{padding}" y2="{height - padding}" stroke="#ddd"/>'
        f'<line x1="{padding}" y1="{height - padding}" x2="{width - padding}" '
        f'y2="{height - padding}" stroke="#ddd"/>'
        f'<polygon points="{area}" fill="rgba(54,162,235,0.15)"/>'
        f'<polyline points="{polyline}" fill="none" stroke="#36a2eb" stroke-width="2"/>'
        f'<text x="6" y="{padding + 4}" font-size="10" fill="#888">{max_label}</text>'
        f'<text x="6" y="{height - padding}" font-size="10" fill="#888">{min_label}</text>'
        f"{ticks}</svg>"
    )


def _nice_ceil(value: float) -> int:  # pragma: no cover - 保留给未来坐标轴刻度使用
    """向上取整到「好看」的刻度值。

    Args:
        value: 数值。

    Returns:
        取整后的整数。
    """
    return int(math.ceil(value * 10) / 10)
