"""站点 CRUD 与详情路由。"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from site_group_monitor.services.site_service import health_of
from site_group_monitor.storage.repository import SiteRepository
from site_group_monitor.utils.netutil import normalize_domain
from site_group_monitor.utils.timeutil import fmt_local
from site_group_monitor.web.deps import Container, get_container
from site_group_monitor.web.schemas import (
    EnabledUpdateRequest,
    SiteCreateRequest,
    TagsUpdateRequest,
    fail,
    ok,
    page_of,
)

router = APIRouter(prefix="/sites", tags=["sites"])


@router.get("")
async def list_sites(
    container: Annotated[Container, Depends(get_container)],
    group: Annotated[str | None, Query()] = None,
    tag: Annotated[str | None, Query()] = None,
    keyword: Annotated[str | None, Query()] = None,
    enabled: Annotated[bool | None, Query()] = None,
    index_status: Annotated[str | None, Query()] = None,
    health: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=500)] = 50,
    sort: Annotated[str, Query()] = "domain",
    desc: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """分页查询站点列表（支持筛选/搜索/排序）。

    Args:
        container: 服务容器。
        group: 分组过滤。
        tag: 标签过滤。
        keyword: 域名/备注模糊搜索。
        enabled: 启停过滤。
        index_status: 收录三态过滤。
        health: 健康度过滤。
        page: 页码。
        size: 每页条数。
        sort: 排序字段。
        desc: 是否倒序。

    Returns:
        分页信封。
    """
    items, total = container.sites.list(
        group=group,
        tag=tag,
        keyword=keyword,
        enabled=enabled,
        index_status=index_status,
        health=health,
        page=page,
        size=size,
        sort=sort,
        desc=desc,
    )
    ssl_warn_days = int(container.settings.alert.ssl_warn_days)
    payload = []
    for site in items:
        row = site.model_dump()
        row["health"] = health_of(site, ssl_warn_days=ssl_warn_days)
        row["checked_at"] = fmt_local(site.checked_at, container.settings.timezone_name()) \
            if site.checked_at else ""
        row["last_checked_at"] = fmt_local(site.last_checked_at, container.settings.timezone_name()) \
            if site.last_checked_at else ""
        payload.append(row)
    return ok(page_of(payload, total, page, size))


@router.get("/groups")
async def list_groups(container: Annotated[Container, Depends(get_container)]) -> dict[str, Any]:
    """分组统计（用于筛选下拉）。"""
    return ok(container.sites.groups())


@router.post("")
async def create_site(
    payload: SiteCreateRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """新增站点（幂等）。

    Args:
        payload: 请求体。
        container: 服务容器。

    Returns:
        站点数据。
    """
    domain = normalize_domain(payload.domain)
    if not domain:
        return fail("非法域名", code=400)
    site = container.sites.add(
        domain,
        group=payload.group,
        tags=payload.tags,
        root_url=payload.root_url,
        name=payload.name,
        notes=payload.notes,
        actor="web",
    )
    return ok(site.model_dump())


@router.delete("/{identifier}")
async def delete_site(
    identifier: str,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """删除站点（级联清理明细/快照/事件）。

    Args:
        identifier: 域名或 id。
        container: 服务容器。

    Returns:
        删除结果。
    """
    removed = container.sites.remove(identifier, actor="web")
    if not removed:
        return fail("站点不存在或删除失败", code=404)
    return ok({"removed": True, "identifier": identifier})


@router.patch("/{identifier}/tags")
async def update_tags(
    identifier: str,
    payload: TagsUpdateRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """更新标签。

    Args:
        identifier: 域名或 id。
        payload: 标签列表。
        container: 服务容器。

    Returns:
        更新后的标签。
    """
    updated = container.sites.update_tags(identifier, payload.tags, actor="web")
    if not updated:
        return fail("站点不存在", code=404)
    site = container.sites.get(identifier)
    return ok({"tags": site.tags if site else []})


@router.patch("/{identifier}/enabled")
async def update_enabled(
    identifier: str,
    payload: EnabledUpdateRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """启停站点。

    Args:
        identifier: 域名或 id。
        payload: 启停布尔值。
        container: 服务容器。

    Returns:
        更新结果。
    """
    updated = container.sites.set_enabled(identifier, payload.enabled, actor="web")
    if not updated:
        return fail("站点不存在", code=404)
    return ok({"enabled": bool(payload.enabled)})


@router.get("/{identifier}")
async def site_detail(
    identifier: str,
    container: Annotated[Container, Depends(get_container)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """站点详情（历史曲线 / TDK 历史 / 重定向链 / SSL / 事件）。

    Args:
        identifier: 域名或 id。
        container: 服务容器。
        days: 历史天数。

    Returns:
        详情字典。
    """
    site = container.sites.get(identifier)
    if site is None or site.id is None:
        return fail("站点不存在", code=404)
    site_id = int(site.id)
    history = container.repos.results.history(site_id, limit=days)
    events = container.repos.events.list_recent(site_id, limit=20)
    tz_name = container.settings.timezone_name()
    return ok(
        {
            "site": site.model_dump(),
            "history": [
                {
                    "checked_at": fmt_local(item.checked_at, tz_name),
                    "index_status": item.index_status,
                    "index_reason": item.index_reason,
                    "http_status": item.http_status,
                    "response_ms": item.response_ms,
                    "ssl_days_left": item.ssl.days_left if item.ssl else None,
                    "title": item.tdk.title if item.tdk else "",
                    "description": item.tdk.description if item.tdk else "",
                    "keywords": item.tdk.keywords if item.tdk else "",
                    "evidence_path": item.evidence_path,
                    "error_type": item.error_type,
                }
                for item in history
            ],
            "latest": _latest_payload(history[0] if history else None, tz_name),
            "events": [
                {**item.model_dump(exclude={"detail"}), **item.detail}
                for item in events
            ],
        }
    )


@router.get("/{identifier}/evidence")
async def site_evidence(
    identifier: str,
    container: Annotated[Container, Depends(get_container)],
) -> Any:
    """返回该站最近一次收录证据文件（截图或 HTML 快照）。"""
    site = container.sites.get(identifier)
    if site is None or site.id is None:
        return fail("站点不存在", code=404)
    history = container.repos.results.history(int(site.id), limit=1)
    if not history:
        return fail("还没有收录证据", code=404)
    path = _safe_evidence_file(str(history[0].evidence_path or ""), container.settings.evidence_dir)
    if path is None:
        return fail("证据文件不存在", code=404)
    media = "image/png" if path.suffix.lower() == ".png" else "application/octet-stream"
    return FileResponse(path, media_type=media, filename=path.name)


def _latest_payload(result: Any, tz_name: str) -> dict[str, Any]:
    """把最新一条结果转成详情抽屉需要的字段。

    Args:
        result: 检测结果实体。
        tz_name: 时区名。

    Returns:
        字段字典。
    """
    if result is None:
        return {}
    return {
        "checked_at": fmt_local(result.checked_at, tz_name),
        "index_status": result.index_status,
        "index_reason": result.index_reason,
        "index_confidence": result.index_confidence,
        "index_top_url": result.index_top_url,
        "evidence_path": result.evidence_path,
        "evidence_kind": _evidence_kind(result.evidence_path),
        "http_status": result.http_status,
        "response_ms": result.response_ms,
        "final_url": result.final_url,
        "redirect_chain": result.redirect_chain,
        "anomalies": result.anomalies,
        "network": (result.anomalies or {}).get("network", "") if isinstance(result.anomalies, dict) else "",
        "ssl": result.ssl.model_dump() if result.ssl else {},
        "tdk": result.tdk.model_dump() if result.tdk else {},
        "error_type": result.error_type,
        "error_msg": result.error_msg,
    }


def _evidence_kind(path: str) -> str:
    """根据证据文件后缀判断类型。"""
    lower = str(path or "").lower()
    if lower.endswith(".png"):
        return "screenshot"
    if ".html" in lower:
        return "html"
    return ""


def _safe_evidence_file(raw: str, evidence_dir: Path) -> Path | None:
    """只允许读取证据目录内的文件。"""
    if not raw:
        return None
    candidate = Path(raw.split("|")[-1].strip())
    try:
        resolved = candidate.resolve()
        root = Path(evidence_dir).resolve()
    except OSError:
        return None
    if root not in resolved.parents and resolved != root:
        return None
    return resolved if resolved.is_file() else None


__all__ = ["router", "SiteRepository"]
