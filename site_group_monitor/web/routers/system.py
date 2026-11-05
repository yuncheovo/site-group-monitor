"""系统路由：设置读写、代理池管理、调度状态、手动触发、审计查询。"""

from __future__ import annotations

import threading
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from site_group_monitor.models.entities import ProxyApiSource, ProxyEndpoint
from site_group_monitor.core.proxy_api_fetcher import parse_proxy_line
from site_group_monitor.utils.errors import ConfigError
from site_group_monitor.utils.netutil import mask_secret
from site_group_monitor.utils.timeutil import utcnow_iso
from site_group_monitor.web.deps import Container, get_container
from site_group_monitor.web.schemas import (
    ProxyApiSourceRequest,
    ProxyApiSourcePolicyRequest,
    ProxyBatchRequest,
    ProxyCreateRequest,
    ProxyToggleRequest,
    RunRequest,
    SettingsPatchRequest,
    fail,
    ok,
    page_of,
)

router = APIRouter(prefix="/system", tags=["system"])

_SENSITIVE_KEYS = {"password", "password_enc"}


# ------------------------------------------------------------------ 调度 / 状态
@router.get("/status")
async def system_status(container: Annotated[Container, Depends(get_container)]) -> dict[str, Any]:
    """系统状态：调度器 + 检测进度 + 库信息 + 邮件可用性。

    Args:
        container: 服务容器。

    Returns:
        状态字典。
    """
    from site_group_monitor.notify.mailer import MailNotifier

    mailer = MailNotifier(container.settings, container.logger, container.audit)
    return ok(
        {
            "scheduler": container.scheduler.status(),
            "progress": container.check.progress_snapshot(),
            "db": container.db.stats(),
            "mail": {"enabled": mailer.is_enabled(), "reason": mailer.reason_disabled()},
            "version": _version(),
        }
    )


@router.post("/run")
async def trigger_run(
    payload: RunRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """手动触发一轮检测（后台线程执行，接口立即返回）。

    Args:
        payload: 触发参数。
        container: 服务容器。

    Returns:
        启动确认字典（实际进度通过 ``GET /api/system/status`` 轮询）。
    """
    if container.check.is_running:
        return fail("已有检测任务在运行，请等待完成后再触发", code=409)
    domains = [item.strip().lower() for item in (payload.domains or []) if str(item).strip()]
    mode = "manual_single" if domains else payload.mode
    selected = bool(domains)
    container.audit.log("web", "check.trigger", mode,
                        {"mode": mode, "notify": payload.notify, "export": payload.export,
                         "domains": domains[:20], "count": len(domains)})
    # 在后台线程启动前就建立共享取消令牌，用户立即点击取消也不会丢信号。
    container.check.prepare_run()
    threading.Thread(
        target=container.scheduler.run_pipeline,
        kwargs={
            "mode": mode,
            "do_notify": bool(payload.notify) and not selected,
            "do_export": bool(payload.export) and not selected,
            "do_cleanup": False,
            "domains": domains or None,
        },
        daemon=True,
    ).start()
    return ok({"status": "started", "mode": mode, "count": len(domains) or "all"})


@router.post("/cancel")
async def cancel_run(container: Annotated[Container, Depends(get_container)]) -> dict[str, Any]:
    """请求取消正在运行的检测批次。

    Args:
        container: 服务容器。

    Returns:
        取消结果。
    """
    token = container.check.cancel()
    container.audit.log("web", "check.cancel", "", {"reason": token.reason})
    return ok({"cancelled": True, "reason": token.reason})


# ------------------------------------------------------------------ 配置
@router.get("/settings")
async def read_settings(container: Annotated[Container, Depends(get_container)]) -> dict[str, Any]:
    """读取当前生效配置（敏感字段脱敏）。

    Args:
        container: 服务容器。

    Returns:
        脱敏后的配置字典。
    """
    data = container.settings.to_yaml_dict()
    _mask_tree(data)
    return ok(data)


@router.post("/settings")
async def write_settings(
    payload: SettingsPatchRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """局部更新配置并热加载。

    Args:
        payload: 配置补丁。
        container: 服务容器。

    Returns:
        更新后的配置（脱敏）。
    """
    patch = payload.patch or {}
    try:
        _validate_web_host(patch)
    except ConfigError as exc:
        return fail(exc.message, code=400)
    try:
        settings = container.reload(patch)
    except Exception as exc:  # noqa: BLE001
        return fail(f"配置更新失败: {exc}", code=500)
    container.audit.log("web", "config.update", "", {"keys": sorted(patch.keys())})
    data = settings.to_yaml_dict()
    _mask_tree(data)
    return ok(data)


# ------------------------------------------------------------------ 代理池
@router.get("/proxies")
async def list_proxies(container: Annotated[Container, Depends(get_container)]) -> dict[str, Any]:
    """代理池列表与统计（凭据脱敏）。

    Args:
        container: 服务容器。

    Returns:
        代理列表与统计。
    """
    from site_group_monitor.core.proxy_pool import ProxyPool

    pool = ProxyPool(container.repos.proxies, container.settings, None, container.logger)
    stats = pool.stats()
    return ok(stats)


@router.post("/proxies")
async def add_proxy(
    payload: ProxyCreateRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """新增/更新一条代理。

    Args:
        payload: 代理参数。
        container: 服务容器。

    Returns:
        代理 id。
    """
    proxy = ProxyEndpoint(
        name=payload.name,
        protocol=payload.protocol,
        host=payload.host,
        port=int(payload.port),
        username=payload.username,
        password_enc=payload.password,
        weight=float(payload.weight or 1.0),
        created_at=utcnow_iso(),
        source=payload.source or "manual",
        source_ref=payload.source_ref,
    )
    proxy_id = container.repos.proxies.save(proxy)
    container.audit.log("web", "proxy.add", f"{payload.host}:{payload.port}",
                        {"protocol": payload.protocol, "user_masked": mask_secret(payload.username)})
    return ok({"id": proxy_id})


@router.delete("/proxies/{identifier}")
async def remove_proxy(
    identifier: str,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """删除代理。

    Args:
        identifier: 代理 id 或 host。
        container: 服务容器。

    Returns:
        删除结果。
    """
    removed = container.repos.proxies.remove(identifier)
    if not removed:
        return fail("代理不存在", code=404)
    container.audit.log("web", "proxy.remove", identifier, {})
    return ok({"removed": True})


@router.patch("/proxies/{proxy_id}/toggle")
async def toggle_proxy(
    proxy_id: int,
    payload: ProxyToggleRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """启停代理。

    Args:
        proxy_id: 代理 id。
        payload: 是否禁用。
        container: 服务容器。

    Returns:
        操作结果。
    """
    updated = container.repos.proxies.set_disabled(proxy_id, payload.disabled)
    if not updated:
        return fail("代理不存在", code=404)
    container.audit.log("web", "proxy.toggle", str(proxy_id), {"disabled": bool(payload.disabled)})
    return ok({"disabled": bool(payload.disabled)})


# ------------------------------------------------------------------ 批量代理
@router.post("/proxies/batch")
async def add_proxies_batch(
    payload: ProxyBatchRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """批量添加固定 IP 代理（每行一条 ``host:port`` 或 ``host:port:user:pass``）。

    Args:
        payload: 批量请求体（文本 + 默认协议）。
        container: 服务容器。

    Returns:
        添加结果（added / skipped / invalid）。
    """
    added = 0
    skipped = 0
    invalid: list[str] = []
    seen_pairs: set[tuple[str, int]] = set()
    for raw_line in (payload.raw or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parsed = parse_proxy_line(line)
        if parsed is None:
            invalid.append(line)
            continue
        key = (parsed["host"], parsed["port"])
        if key in seen_pairs:
            skipped += 1
            continue
        seen_pairs.add(key)
        protocol = payload.protocol or parsed["protocol"] or "http"
        proxy = ProxyEndpoint(
            name=f"{parsed['host']}:{parsed['port']}",
            protocol=protocol,
            host=parsed["host"],
            port=parsed["port"],
            username=payload.username or parsed["username"],
            password_enc=payload.password or parsed["password"],
            weight=float(payload.default_weight or 1.0),
            created_at=utcnow_iso(),
            source="batch",
        )
        existing = container.repos.proxies.find(proxy.protocol, proxy.host, proxy.port)
        container.repos.proxies.save(proxy)
        if existing is None:
            added += 1
        else:
            skipped += 1
    container.audit.log("web", "proxy.batch", "", {"added": added, "skipped": skipped, "invalid": len(invalid)})
    return ok({"added": added, "skipped": skipped, "invalid": invalid})


# ------------------------------------------------------------------ 代理 API 源
@router.get("/proxies/api-sources")
async def list_api_sources(container: Annotated[Container, Depends(get_container)]) -> dict[str, Any]:
    """列出全部代理 API 源（密码脱敏，附带各源代理数）。

    Args:
        container: 服务容器。

    Returns:
        API 源列表。
    """
    sources = container.repos.proxies_api.list_all(include_disabled=True)
    items = []
    for source in sources:
        item = source.to_row()
        item.pop("password_enc", None)
        item["password"] = "***" if source.password_enc else ""
        item["id"] = source.id
        item["proxy_count"] = len(container.repos.proxies.list_by_source("api", str(source.id)))
        items.append(item)
    return ok(items)


@router.post("/proxies/api-sources")
async def create_api_source(
    payload: ProxyApiSourceRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """新增/更新一个代理 API 源（按 url 幂等）。

    Args:
        payload: API 源配置。
        container: 服务容器。

    Returns:
        源 id。
    """
    if not payload.url:
        return fail("API 源 URL 不能为空", code=400)
    source = ProxyApiSource(
        name=payload.name or payload.url,
        url=payload.url,
        protocol=payload.protocol or "http",
        username=payload.username,
        password_enc=payload.password,
        refresh_interval_min=max(1, int(payload.refresh_interval_min or 30)),
        rotate_per_check=bool(payload.rotate_per_check),
        enabled=bool(payload.enabled),
        created_at=utcnow_iso(),
    )
    source_id = container.repos.proxies_api.save(source)
    try:
        container.scheduler.register_proxy_api_sources(container.repos)
    except Exception as exc:  # noqa: BLE001 - 注册失败不影响保存
        if container.logger is not None:
            container.logger.warning("proxy_api: 注册定时刷新失败 %s", exc)
    container.audit.log("web", "proxy_api.create", payload.url,
                        {"name": source.name, "interval": source.refresh_interval_min,
                         "user_masked": mask_secret(payload.username)})
    return ok({"id": source_id})


@router.patch("/proxies/api-sources/{source_id}/policy")
async def update_api_source_policy(
    source_id: int,
    payload: ProxyApiSourcePolicyRequest,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """修改 API 源的 IP 切换策略，其他配置保持不变。"""
    updated = container.repos.proxies_api.update_policy(
        source_id, payload.rotate_per_check
    )
    if not updated:
        return fail("API 源不存在", code=404)
    strategy = "每查询一个站切换 IP" if payload.rotate_per_check else "遇到验证码/风控时切换 IP"
    container.audit.log(
        "web", "proxy_api.policy", str(source_id),
        {"rotate_per_check": bool(payload.rotate_per_check)},
    )
    return ok({"updated": True, "strategy": strategy})


@router.delete("/proxies/api-sources/{source_id}")
async def delete_api_source(
    source_id: int,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """删除代理 API 源（不自动删除其已拉取的代理）。

    Args:
        source_id: 源 id。
        container: 服务容器。

    Returns:
        删除结果。
    """
    removed = container.repos.proxies_api.remove(source_id)
    if not removed:
        return fail("API 源不存在", code=404)
    container.audit.log("web", "proxy_api.delete", str(source_id), {})
    return ok({"removed": True})


@router.post("/proxies/{proxy_id}/probe")
def probe_proxy(
    proxy_id: int,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """探测单条代理的出口 IP、地理位置与延迟。

    Args:
        proxy_id: 代理 id。
        container: 服务容器。

    Returns:
        探测结果。
    """
    proxy = container.repos.proxies.get_by_id(proxy_id)
    if proxy is None:
        return fail("代理不存在", code=404)
    from site_group_monitor.core.proxy_probe import probe_and_store

    result = probe_and_store(proxy, container.repos)
    container.audit.log("web", "proxy.probe", str(proxy_id),
                        {"ok": result.get("ok"), "ip": result.get("ip"),
                         "latency_ms": result.get("latency_ms")})
    return ok(result)


@router.post("/proxies/api-sources/{source_id}/refresh")
def refresh_api_source(
    source_id: int,
    container: Annotated[Container, Depends(get_container)],
) -> dict[str, Any]:
    """立即拉取一个 API 源的新 IP，并对启用中的代理测速（出口 IP / 位置 / 延迟）。

    Args:
        source_id: 源 id。
        container: 服务容器。

    Returns:
        拉取与探测结果。
    """
    source = container.repos.proxies_api.get_by_id(source_id)
    if source is None:
        return fail("API 源不存在", code=404)
    container.audit.log("web", "proxy_api.refresh", str(source_id), {"url": source.url})
    from site_group_monitor.core.proxy_api_fetcher import fetch_and_store
    from site_group_monitor.core.proxy_probe import probe_source_proxies

    prune = "delete" if getattr(source, "rotate_per_check", True) else "disable"
    try:
        result = fetch_and_store(source, container.repos, container.settings, prune=prune)
    except Exception as exc:  # noqa: BLE001
        return fail(f"刷新失败: {exc}", code=500)
    probes: list[dict[str, Any]] = []
    if result.get("ok"):
        probes = probe_source_proxies(source_id, container.repos)
    result["probes"] = probes
    result["id"] = source_id
    return ok(result)


# ------------------------------------------------------------------ 审计
@router.get("/audit")
async def query_audit(
    container: Annotated[Container, Depends(get_container)],
    actor: Annotated[str | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    keyword: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=500)] = 50,
) -> dict[str, Any]:
    """查询审计日志。

    Args:
        container: 服务容器。
        actor: 来源过滤。
        action: 动作名模糊匹配。
        keyword: 关键字搜索。
        page: 页码。
        size: 每页条数。

    Returns:
        分页信封。
    """
    records, total = container.audit.query(
        actor=actor, action=action, keyword=keyword, page=page, size=size
    )
    return ok(page_of(records, total, page, size))


# ------------------------------------------------------------------ 工具
def _mask_tree(node: Any) -> None:
    """递归脱敏敏感字段（就地修改）。

    Args:
        node: 配置字典或列表。
    """
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if str(key).lower() in _SENSITIVE_KEYS and value:
                node[key] = "***"
            else:
                _mask_tree(value)
    elif isinstance(node, list):
        for item in node:
            _mask_tree(item)


def _validate_web_host(patch: dict[str, Any]) -> None:
    """校验补丁中的 web.host 必须为本地地址。

    Args:
        patch: 配置补丁。

    Raises:
        ConfigError: 绑定非本地地址时抛出。
    """
    web_section = patch.get("web") or {}
    host = web_section.get("host") if isinstance(web_section, dict) else None
    if host and str(host) not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("出于安全考虑，Web 仪表盘只能绑定 127.0.0.1")


def _version() -> str:
    """返回包版本号。"""
    from site_group_monitor import __version__

    return __version__
