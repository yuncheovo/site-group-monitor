"""代理出口探测：经代理访问 IP 查询接口，得到出口 IP、地理位置与延迟。

国内提取 IP 多为 HTTP 代理，探测时优先走 ``http://`` 入口，并使用国内可达的查询接口。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from site_group_monitor.models.entities import ProxyEndpoint
from site_group_monitor.utils.timeutil import utcnow_iso

_LOOKUP_URLS = (
    "http://whois.pconline.com.cn/ipJson.jsp?json=true",
    "http://ip.3322.net/",
    "http://ip-api.com/json/?lang=zh-CN&fields=status,message,country,regionName,city,isp,query",
)

_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _decode_body(content: bytes, declared: str = "") -> str:
    """尽量按 utf-8 / gb18030 解码响应。"""
    for enc in (declared, "utf-8", "gb18030"):
        if not enc:
            continue
        try:
            return content.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


def parse_ip_lookup(body: str) -> tuple[str, str]:
    """从 IP 查询接口响应中抽出出口 IP 与地理位置。

    Args:
        body: 响应文本。

    Returns:
        ``(ip, location)``，解析失败时为空串。
    """
    text = (body or "").strip()
    if not text:
        return "", ""

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError):
        payload = None

    if isinstance(payload, dict):
        ip = str(
            payload.get("query")
            or payload.get("ip")
            or payload.get("origin")
            or payload.get("cip")
            or ""
        ).split(",")[0].strip()
        if payload.get("status") == "fail":
            return ip, ""
        parts: list[str] = []
        for key in ("country", "country_name", "pro", "regionName", "region", "city", "addr"):
            value = payload.get(key)
            if value and str(value) not in parts:
                parts.append(str(value).strip())
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("country", "prov", "city", "isp", "owner"):
                value = data.get(key)
                if value and str(value) not in parts:
                    parts.append(str(value).strip())
            ip = ip or str(data.get("ip") or "")
        isp = payload.get("isp") or payload.get("org")
        location = " ".join(parts)
        if isp and isp not in location:
            location = f"{location} · {isp}" if location else str(isp)
        if ip or location:
            return ip, location

    match = _IP_RE.search(text)
    if match:
        return match.group(0), ""
    return "", ""


def _candidate_proxy_urls(proxy: ProxyEndpoint) -> list[str]:
    """探测时同时尝试 http / 原协议，避免界面误选 https 导致全部失败。"""
    if (proxy.protocol or "").startswith("socks"):
        return [proxy.url]
    urls: list[str] = []
    auth = ""
    if proxy.username and proxy.password_enc:
        auth = f"{proxy.username}:{proxy.password_enc}@"
    for scheme in ("http", proxy.protocol or "http"):
        url = f"{scheme}://{auth}{proxy.host}:{proxy.port}"
        if url not in urls:
            urls.append(url)
    return urls


def probe_proxy(proxy: ProxyEndpoint, timeout: float = 8.0) -> dict[str, Any]:
    """经指定代理探测出口 IP、地理位置与往返延迟。"""
    import httpx

    last_error = ""
    for proxy_url in _candidate_proxy_urls(proxy):
        for url in _LOOKUP_URLS:
            started = time.perf_counter()
            try:
                with httpx.Client(
                    proxy=proxy_url,
                    timeout=timeout,
                    trust_env=False,
                    verify=False,
                    follow_redirects=True,
                ) as client:
                    resp = client.get(url)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    if resp.status_code >= 400:
                        last_error = f"HTTP {resp.status_code}"
                        continue
                    charset = ""
                    ctype = resp.headers.get("content-type") or ""
                    if "charset=" in ctype.lower():
                        charset = ctype.split("charset=", 1)[-1].split(";")[0].strip()
                    body = _decode_body(resp.content, charset)
                    ip, location = parse_ip_lookup(body)
                    if not ip and not location:
                        last_error = "无法解析 IP 查询响应"
                        continue
                    if not ip:
                        ip = proxy.host
                    return {
                        "ok": True,
                        "proxy_id": proxy.id,
                        "url": proxy.masked_url,
                        "ip": ip,
                        "location": location,
                        "latency_ms": latency_ms,
                        "error": "",
                    }
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue
    return {
        "ok": False,
        "proxy_id": proxy.id,
        "url": proxy.masked_url,
        "ip": "",
        "location": "",
        "latency_ms": None,
        "error": last_error or "探测失败",
    }


def store_probe(repos: Any, proxy: ProxyEndpoint, result: dict[str, Any]) -> dict[str, Any]:
    """把探测结果写回 ``proxies`` 表。"""
    if proxy.id is None:
        return result
    repos.proxies.save_probe(
        proxy.id,
        egress_ip=str(result.get("ip") or ""),
        location=str(result.get("location") or ""),
        latency_ms=result.get("latency_ms"),
        error=str(result.get("error") or ""),
    )
    result["probed_at"] = utcnow_iso()
    return result


def probe_and_store(proxy: ProxyEndpoint, repos: Any, timeout: float = 8.0) -> dict[str, Any]:
    """探测并落库。"""
    result = probe_proxy(proxy, timeout=timeout)
    return store_probe(repos, proxy, result)


def probe_source_proxies(
    source_id: int,
    repos: Any,
    *,
    timeout: float = 8.0,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """对某个 API 源当前启用的代理逐条探测（默认最多 20 条）。"""
    proxies = [
        item
        for item in repos.proxies.list_by_source("api", str(source_id))
        if not item.disabled
    ]
    results: list[dict[str, Any]] = []
    for proxy in proxies[: max(0, int(limit))]:
        results.append(probe_and_store(proxy, repos, timeout=timeout))
    return results
