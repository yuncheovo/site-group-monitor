"""代理 API 源拉取器：从用户配置的接口拉取 IP 列表并填充代理池。

要点：
    * 容错解析：JSON（list / dict 包裹 list）/ 纯文本（换行 + 逗号分隔）都支持；
    * 单行代理解析接受 ``host:port`` / ``host:port:user:pass`` / ``scheme://host:port`` /
      ``scheme://user:pass@host:port`` / ``user:pass@host:port``；
    * 拉取结果先 upsert 到 ``proxies``（source="api", source_ref=源 id），再对账：
      本轮没拉到的旧 IP 软 retire（disabled=1），避免残留失效代理；
    * 任何单行 / 单源异常都不抛出，返回结构化结果，由调度层/前端决定展示。
"""

from __future__ import annotations

import json
import re
from typing import Any

from site_group_monitor.models.entities import ProxyApiSource, ProxyEndpoint
from site_group_monitor.utils.timeutil import utcnow_iso


# host:port 基础校验（host 非空、port 为 1-65535 整数）
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-_%]+$")


def _valid_host_port(host: str, port: Any) -> bool:
    """校验 host / port 是否可用于代理。

    Args:
        host: 主机名或 IP。
        port: 端口（可传入字符串或整数）。

    Returns:
        合法返回 True。
    """
    if not host or not _HOST_RE.match(host):
        return False
    try:
        port_i = int(port)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return 1 <= port_i <= 65535


def parse_proxy_line(line: str) -> dict[str, Any] | None:
    """解析单行代理文本为字段字典。

    支持格式：
        * ``host:port``
        * ``host:port:user:pass``
        * ``protocol://host:port``
        * ``protocol://user:pass@host:port``
        * ``user:pass@host:port``

    Args:
        line: 单行文本。

    Returns:
        字段字典（含 host/port/protocol/username/password），无法解析返回 None。
    """
    raw = (line or "").strip()
    if not raw:
        return None

    protocol = ""
    username = ""
    password = ""
    rest = raw

    # 协议前缀：scheme://...
    if "://" in raw:
        scheme, _, after = raw.partition("://")
        scheme = scheme.strip().lower()
        if scheme in ("http", "https", "socks5", "socks4"):
            protocol = "socks5" if scheme == "socks4" else scheme
            rest = after
        else:
            # 不认识的 scheme（如 ftp），忽略协议，继续按裸串解析
            rest = raw

    # 用户密码：user:pass@...
    if "@" in rest:
        creds, _, hostpart = rest.partition("@")
        if ":" in creds:
            username, _, password = creds.partition(":")
        else:
            username = creds
        rest = hostpart

    # 剩余部分按 host:port[:user:pass] 解析
    parts = rest.split(":")
    if len(parts) < 2:
        return None
    host = parts[0].strip()
    port = parts[1].strip()
    # host:port:user:pass 形式（无协议前缀、无 @）
    if not username and len(parts) >= 4:
        username = parts[2].strip()
        password = parts[3].strip()

    if not _valid_host_port(host, port):
        return None
    return {
        "host": host,
        "port": int(port),
        "protocol": protocol,
        "username": username or "",
        "password": password or "",
    }


def _extract_from_dict(item: dict[str, Any]) -> dict[str, Any] | None:
    """从 JSON dict 中抽取代理字段。

    Args:
        item: 一个代理对象字典。

    Returns:
        字段字典或 None。
    """
    if not isinstance(item, dict):
        return None
    # 单键包含 proxy/url 字符串时，按 URL 解析
    for key in ("proxy", "url"):
        if key in item and isinstance(item[key], str):
            parsed = parse_proxy_line(item[key])
            if parsed:
                # 允许 dict 额外覆盖协议/账号
                protocol = (
                    _pick(item, ("protocol", "type", "scheme"))
                    or parsed["protocol"]
                )
                if protocol in ("http", "https", "socks5"):
                    parsed["protocol"] = protocol
                username = _pick(item, ("username", "user", "pwd", "password")) or parsed["username"]
                if username:
                    parsed["username"] = username
                return parsed
            return None

    host = _pick(item, ("host", "ip", "server", "address"))
    if not host:
        return None
    port = _pick(item, ("port", "port_http", "port_https"))
    if port is None:
        return None
    protocol = _pick(item, ("protocol", "type", "scheme")) or ""
    if protocol:
        protocol = str(protocol).lower()
        if protocol.startswith("socks"):
            protocol = "socks5"
        if protocol not in ("http", "https", "socks5"):
            protocol = ""
    username = _pick(item, ("username", "user", "pwd", "password")) or ""
    password = _pick(item, ("password", "pass", "pwd"), allow_none=True)
    if password is None:
        password = ""
    if not _valid_host_port(host, port):
        return None
    return {
        "host": str(host),
        "port": int(port),
        "protocol": protocol,
        "username": username or "",
        "password": password or "",
    }


def _pick(item: dict[str, Any], keys: tuple[str, ...], allow_none: bool = False) -> Any:
    """按优先级从字典取值。

    Args:
        item: 源字典。
        keys: 候选键（依次尝试）。
        allow_none: 是否允许返回 None（否则空串也不取）。

    Returns:
        找到的值或 "" / None。
    """
    for key in keys:
        if key in item and item[key] not in (None, ""):
            return item[key]
    return None if allow_none else ""


def _iter_list_payload(payload: Any) -> list[Any]:
    """从 JSON 负载中找出代表代理列表的对象。

    Args:
        payload: json.loads 的结果。

    Returns:
        代理条目列表（可能是 str 或 dict）。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "list", "proxies", "result", "nodes", "rows", "items"):
            if key in payload and isinstance(payload[key], list):
                return payload[key]
        # 没有已知包裹键：若 values 里第一个是 list 则用它
        for value in payload.values():
            if isinstance(value, list) and value:
                return value
    return []


def parse_response_body(body: str, default_protocol: str = "http") -> list[dict[str, Any]]:
    """解析 API 响应体为代理字段列表。

    Args:
        body: 响应文本。
        default_protocol: 缺省协议。

    Returns:
        代理字段字典列表（已过滤无法解析的条目）。
    """
    body = (body or "").strip()
    if not body:
        return []

    parsed_items: list[dict[str, Any]] = []
    # 先尝试 JSON
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        payload = None

    if payload is not None:
        for item in _iter_list_payload(payload):
            if isinstance(item, str):
                parsed = parse_proxy_line(item)
                if parsed:
                    parsed_items.append(parsed)
            elif isinstance(item, dict):
                parsed = _extract_from_dict(item)
                if parsed:
                    parsed_items.append(parsed)
        return _apply_default_protocol(parsed_items, default_protocol)

    # 纯文本：按换行 + 逗号切分
    tokens: list[str] = []
    for line in body.splitlines():
        for tok in re.split(r"[,\s]+", line):
            tok = tok.strip()
            if tok:
                tokens.append(tok)
    for tok in tokens:
        parsed = parse_proxy_line(tok)
        if parsed:
            parsed_items.append(parsed)
    return _apply_default_protocol(parsed_items, default_protocol)


def _apply_default_protocol(items: list[dict[str, Any]], default_protocol: str) -> list[dict[str, Any]]:
    """为缺省协议的条目补上默认协议。"""
    for item in items:
        if not item.get("protocol"):
            item["protocol"] = default_protocol or "http"
    return items


def _effective_protocol(item: dict[str, Any], source: ProxyApiSource) -> str:
    """提取 API 返回的 IP 默认按 HTTP 代理使用。

    界面若选了 https，httpx 会对代理本身做 TLS，国内 ``getips`` 类提取 IP 几乎都不支持。
    """
    protocol = (item.get("protocol") or source.protocol or "http").lower()
    if protocol == "https":
        return "http"
    if protocol not in ("http", "socks5"):
        return "http"
    return protocol


def fetch_and_store(
    source: ProxyApiSource,
    repos: Any,
    settings: Any,
    *,
    prune: str = "disable",
) -> dict[str, Any]:
    """拉取一个 API 源并写入代理池（含对账）。

    Args:
        source: API 源实体。
        repos: 仓储容器（需含 ``proxies`` 与 ``proxies_api``）。
        settings: 配置对象（提供 ``http.timeout_total``）。
        prune: 本轮未出现的旧 IP：``disable`` 软下线 / ``delete`` 删除 / ``keep`` 保留。

    Returns:
        结构化结果字典。
    """
    import httpx

    timeout = 20
    if settings is not None:
        timeout = int(getattr(getattr(settings, "http", None), "timeout_total", 20) or 20)

    auth = None
    if source.username:
        auth = (source.username, source.password_enc)

    try:
        with httpx.Client(timeout=timeout, auth=auth, verify=False, follow_redirects=True) as client:
            resp = client.get(source.url)
            body = resp.text
    except Exception as exc:  # noqa: BLE001 - 网络/解析异常统一返回失败
        repos.proxies_api.update_fetch_result(source.id, 0, str(exc))
        return {"ok": False, "error": str(exc), "added": 0, "updated": 0, "disabled": 0}

    default_protocol = "http" if (source.protocol or "http") == "https" else (source.protocol or "http")
    items = parse_response_body(body, default_protocol=default_protocol)

    added = 0
    updated = 0
    seen: set[tuple[str, int]] = set()
    fresh_ids: list[int] = []
    for item in items:
        key = (item["host"], item["port"])
        seen.add(key)
        protocol = _effective_protocol(item, source)
        existing = repos.proxies.find_host_port(item["host"], item["port"])
        if existing is not None and existing.protocol == "https" and protocol == "http":
            repos.proxies.remove(existing.id)
            existing = None
        elif existing is not None:
            protocol = existing.protocol
        proxy = ProxyEndpoint(
            name=f"api#{source.id}:{item['host']}:{item['port']}",
            protocol=protocol,
            host=item["host"],
            port=item["port"],
            username=item.get("username") or "",
            password_enc=item.get("password") or "",
            source="api",
            source_ref=str(source.id),
            disabled=False,
            created_at=utcnow_iso(),
        )
        if existing is not None and not proxy.username:
            proxy.username = existing.username
            proxy.password_enc = existing.password_enc
        proxy_id = repos.proxies.save(proxy)
        repos.proxies.set_disabled(proxy_id, False)
        fresh_ids.append(proxy_id)
        if existing is None:
            added += 1
        else:
            updated += 1

    disabled = 0
    existing_proxies = repos.proxies.list_by_source("api", str(source.id))
    for proxy in existing_proxies:
        if (proxy.host, proxy.port) in seen:
            continue
        if prune == "keep":
            continue
        if prune == "delete" and proxy.id is not None:
            repos.proxies.remove(proxy.id)
            disabled += 1
            continue
        if not proxy.disabled:
            repos.proxies.set_disabled(proxy.id, True)
        disabled += 1

    existing_proxies = repos.proxies.list_by_source("api", str(source.id))
    repos.proxies_api.update_fetch_result(source.id, len(items), "")
    return {
        "ok": True,
        "added": added,
        "updated": updated,
        "disabled": disabled,
        "total": len(existing_proxies),
        "count": len(items),
        "fresh_ids": fresh_ids,
    }
