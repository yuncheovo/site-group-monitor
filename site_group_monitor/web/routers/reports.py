"""报告列表 / 详情 / 生成 / 下载路由。"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from site_group_monitor.web.deps import Container, get_container
from site_group_monitor.web.schemas import fail, ok

router = APIRouter(prefix="/reports", tags=["reports"])

_FMT_CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "db": "application/x-sqlite3",
}


@router.get("")
async def list_reports(
    container: Annotated[Container, Depends(get_container)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """列出已生成的报告文件。

    Args:
        container: 服务容器。
        limit: 条数。

    Returns:
        报告文件列表。
    """
    return ok(container.report.list_reports(limit=limit))


@router.get("/{stat_date}")
async def report_detail(
    stat_date: str,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """获取某天的报告聚合数据（不落文件）。

    Args:
        stat_date: 日期键（``YYYY-MM-DD``），支持 ``today`` / ``yesterday``。
        container: 服务容器。

    Returns:
        报告字典。
    """
    key = _resolve_date(stat_date, container)
    try:
        report = container.report.build_daily(key)
    except Exception as exc:  # noqa: BLE001 - 查询失败统一走信封
        return fail(f"报告生成失败: {exc}", code=500)
    return ok(report.to_dict())


@router.post("/{stat_date}/generate")
async def generate_report(
    stat_date: str,
    container: Annotated[Container, Depends(get_container)],
    fmt: Annotated[str, Query(pattern="^(html|xlsx|all)$")] = "all",
) -> dict[str, Any]:
    """生成（导出）某天报告文件。

    Args:
        stat_date: 日期键。
        container: 服务容器。
        fmt: 导出格式。

    Returns:
        文件路径字典。
    """
    key = _resolve_date(stat_date, container)
    try:
        paths = container.report.save(key, fmt=fmt)
    except Exception as exc:  # noqa: BLE001
        return fail(f"导出失败: {exc}", code=500)
    container.audit.log("web", "report.export", key, {"fmt": fmt, "paths": paths})
    return ok(paths)


@router.get("/{stat_date}/download")
async def download_report(
    stat_date: str,
    container: Annotated[Container, Depends(get_container)],
    fmt: Annotated[str, Query(pattern="^(html|xlsx)$")] = "html",
) -> Any:
    """下载报告文件（不存在时即时生成）。

    Args:
        stat_date: 日期键。
        container: 服务容器。
        fmt: ``html`` / ``xlsx``。

    Returns:
        ``FileResponse`` 或失败信封。
    """
    key = _resolve_date(stat_date, container)
    candidates = [
        Path(container.settings.reports_dir) / f"report-{key}.{fmt}",
    ]
    existing = next((path for path in candidates if path.is_file()), None)
    if existing is None:
        try:
            paths = container.report.save(key, fmt=fmt)
        except Exception as exc:  # noqa: BLE001
            return fail(f"导出失败: {exc}", code=500)
        existing = Path(paths.get(fmt, ""))
    if not existing.is_file():
        return fail("报告文件不存在", code=404)
    content_type = _FMT_CONTENT_TYPES.get(
        fmt, mimetypes.guess_type(str(existing))[0] or "application/octet-stream"
    )
    return FileResponse(str(existing), media_type=content_type, filename=existing.name)


def _resolve_date(stat_date: str, container: Container) -> str:
    """解析日期键（支持 today / yesterday 别名）。

    Args:
        stat_date: 原始输入。
        container: 服务容器（用于时区）。

    Returns:
        ``YYYY-MM-DD`` 日期键。
    """
    from site_group_monitor.utils.timeutil import shift_days, today_str

    text = str(stat_date or "").strip().lower()
    today = today_str(container.settings.timezone_name())
    if text in ("today", "今天"):
        return today
    if text in ("yesterday", "昨天"):
        return shift_days(today, -1)
    return str(stat_date)
