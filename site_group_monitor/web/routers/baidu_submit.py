"""百度搜索资源平台浏览器提交接口。"""

from __future__ import annotations

import asyncio
import threading
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from site_group_monitor.models.enums import IndexStatus
from site_group_monitor.web.deps import Container, get_container
from site_group_monitor.web.schemas import BaiduSubmitRequest, fail, ok

router = APIRouter(prefix="/baidu-submit", tags=["baidu-submit"])


@router.get("/status")
async def submit_status(
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """返回浏览器提交任务进度。"""
    return ok(container.baidu_submit.snapshot())


@router.post("/run")
async def start_submit(
    payload: BaiduSubmitRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """把当前未收录站点首页提交到百度资源平台。"""
    service = container.baidu_submit
    if service.is_running:
        return fail("已有百度提交任务正在运行", code=409)

    sites = []
    requested = {str(item).strip().lower() for item in payload.domains if str(item).strip()}
    if requested:
        for domain in sorted(requested):
            site = container.sites.get(domain)
            if site is None or site.id is None or not site.enabled:
                continue
            latest = container.repos.results.latest(int(site.id))
            if latest is not None and latest.index_status == IndexStatus.NOT_INDEXED.value:
                sites.append(site)
            if len(sites) >= payload.limit:
                break
    else:
        sites, _ = container.repos.sites.list(
            enabled=True,
            index_status=IndexStatus.NOT_INDEXED.value,
            page=1,
            size=payload.limit,
            sort="domain",
        )
    if not sites:
        return fail("当前没有可提交的未收录站点", code=400)

    threading.Thread(
        target=lambda: asyncio.run(service.run(sites)),
        name="baidu-browser-submit",
        daemon=True,
    ).start()
    container.audit.log(
        "web", "baidu.submit.browser", "", {"count": len(sites), "domains": [s.domain for s in sites]}
    )
    return ok({"started": True, "count": len(sites), "mode": "browser"})


@router.post("/cancel")
async def cancel_submit(
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """停止当前百度浏览器提交任务。"""
    container.baidu_submit.cancel()
    return ok({"cancelled": True})
