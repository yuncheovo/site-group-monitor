"""总览 / 趋势 / 统计路由。"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from site_group_monitor.web.deps import Container, get_container
from site_group_monitor.web.schemas import ok

router = APIRouter(prefix="/overview", tags=["overview"])


@router.get("")
async def get_overview(container: Annotated[Container, Depends(get_container)]) -> dict[str, Any]:
    """总览卡片数据（站点总数 / 收录率 / 较昨日 / 异常 / 代理可用率）。

    Args:
        container: 服务容器。

    Returns:
        统一信封，``data`` 为总览卡片字典。
    """
    card = container.report.overview()
    return ok(card.to_dict())


@router.get("/trend")
async def get_trend(
    container: Annotated[Container, Depends(get_container)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """收录率趋势与 diff 柱状数据。

    Args:
        container: 服务容器。
        days: 统计天数。

    Returns:
        ``{"series": [...], "diffs": [...]}``。
    """
    series = container.repos.snapshots.trend(days=days)
    diffs = container.repos.events.diff_trend(days=days)
    return ok({"series": series, "diffs": diffs, "days": days})


@router.get("/stats")
async def get_stats(container: Annotated[Container, Depends(get_container)]) -> dict[str, Any]:
    """健康度分布与存储统计（可观测性）。

    Args:
        container: 服务容器。

    Returns:
        统计字典。
    """
    ssl_warn_days = int(container.settings.alert.ssl_warn_days)
    health_counts = {"healthy": 0, "warn": 0, "abnormal": 0, "unknown": 0}
    status_counts = {"indexed": 0, "not_indexed": 0, "unknown_need_manual_check": 0}
    results = container.repos.results.latest_all()
    for result in results:
        key = str(result.index_status)
        status_counts[key] = status_counts.get(key, 0) + 1
        bucket = result.health(ssl_warn_days=ssl_warn_days)
        health_counts[bucket] = health_counts.get(bucket, 0) + 1
    db_stats = container.db.stats()
    db_stats.pop("tables", None)
    return ok(
        {
            "health": health_counts,
            "index_status": status_counts,
            "sites": {
                "total": container.repos.sites.count_all(),
                "enabled": container.repos.sites.count_enabled(),
            },
            "db": db_stats,
            "timezone": container.settings.timezone_name(),
        }
    )
