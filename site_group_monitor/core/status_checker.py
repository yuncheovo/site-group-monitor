"""站点可访问性检测：DNS / HTTP 状态 / 重定向链 / 响应时间 / SSL / 混合内容。

要点（对应架构文档 §1.7）：
    * ``follow_redirects=False`` 后**手动逐跳**（<= ``http.max_redirects``），记录完整重定向链与环路；
    * SSL 握手发到线程池（``run_in_executor``），**绝不阻塞事件循环**；
    * DNS 预检失败立即短路，省去后续 HTTP / SSL 开销；
    * https 页面 HTML 中出现 ``http://`` 资源 → ``anomalies.mixed_content``；
    * 重定向异常四类判定：跳数 > 3 / 跨域 / https→http 降级 / 中间出现 4xx·5xx。
"""

from __future__ import annotations

import asyncio
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from site_group_monitor.models.entities import SiteStatusInfo, SslInfo
from site_group_monitor.models.enums import ErrorType
from site_group_monitor.utils.netutil import assure_absolute, root_url_of, strip_scheme_and_path

_MIXED_CONTENT_RE = re.compile(r"""(?:src|href)\s*=\s*["']http://""", re.IGNORECASE)
_MAX_REDIRECT_HOPS_ALERT = 3


@dataclass
class _Hop:
    """一次跳转记录。"""

    url: str
    status: int = 0
    location: str = ""
    elapsed_ms: int = 0


class StatusChecker:
    """站点健康检测。"""

    def __init__(self, http: Any, settings: Any, logger: Any | None = None) -> None:
        """构造检测器。

        Args:
            http: HTTP 客户端。
            settings: 配置对象。
            logger: 日志记录器。
        """
        self.http = http
        self.settings = settings
        self.logger = logger

    # ---------------------------------------------------------------- 配置
    def _max_redirects(self) -> int:
        """最大跟跳数。"""
        if self.settings is None:
            return 5
        return max(1, int(getattr(self.settings.http, "max_redirects", 5)))

    def _timeout_sec(self) -> float:
        """单次 HTTP 总超时。"""
        if self.settings is None:
            return 20.0
        return float(getattr(self.settings.http, "timeout_total", 20) or 20)

    # ---------------------------------------------------------------- 主流程
    async def probe(
        self,
        domain: str,
        root_url: str = "",
        *,
        proxy: Any | None = None,
    ) -> SiteStatusInfo:
        """对一个站点做完整可访问性探测。

        Args:
            domain: 归一化域名。
            root_url: 覆盖的首页 URL。

        Returns:
            :class:`SiteStatusInfo`。
        """
        info = SiteStatusInfo()
        if not domain:
            info.error_type = ErrorType.DNS_FAILED.value
            info.error_msg = "域名为空"
            return info

        dns_ok = await self.probe_dns(domain)
        info.dns_ok = dns_ok
        if not dns_ok:
            info.error_type = ErrorType.DNS_FAILED.value
            info.error_msg = "DNS 解析失败"
            if self.logger is not None:
                self.logger.debug("status: %s DNS 解析失败，跳过 HTTP/SSL", domain)
            return info

        start_url = root_url or root_url_of(domain, prefer_https=True)
        try:
            chain, final_status, final_url, total_ms, html_bytes, error = await self.probe_http(
                start_url, proxy=proxy
            )
        except Exception as exc:  # noqa: BLE001 - 采集内核异常一律降级
            info.error_type = ErrorType.CONN_FAILED.value
            info.error_msg = str(exc)
            return info

        info.redirect_chain = [hop.__dict__ for hop in chain]
        info.http_status = final_status
        info.final_url = final_url
        info.response_ms = total_ms
        info.anomalies = self.analyze_chain(domain, chain, final_status)
        if error:
            info.error_type = error
            if final_status and final_status >= 500:
                info.error_type = ErrorType.HTTP_5XX.value

        # 混合内容（仅 https 最终页 HTML 中检测）
        if html_bytes and str(final_url).lower().startswith("https://"):
            try:
                preview = html_bytes[:262144].decode("utf-8", errors="ignore")
                info.mixed_content = bool(_MIXED_CONTENT_RE.search(preview))
            except Exception:  # noqa: BLE001
                info.mixed_content = False
        if info.mixed_content:
            info.anomalies["mixed_content"] = True
        return info

    # ---------------------------------------------------------------- DNS
    async def probe_dns(self, host: str) -> bool:
        """DNS 解析预检（executor 中执行，避免阻塞事件循环）。

        Args:
            host: 主机名。

        Returns:
            解析成功返回 True。
        """
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, socket.getaddrinfo, host, None)
            return True
        except Exception:  # noqa: BLE001 - DNS 任何异常都视为解析失败
            return False

    # ---------------------------------------------------------------- HTTP
    async def probe_http(
        self,
        url: str,
        *,
        max_hops: int | None = None,
        proxy: Any | None = None,
    ) -> tuple[list[_Hop], int | None, str, int, bytes, str]:
        """手动逐跳跟随重定向并采集状态码 / 耗时 / HTML 预览。

        Args:
            url: 起始 URL。
            max_hops: 最大跳数（默认读取配置）。

        Returns:
            ``(chain, final_status, final_url, total_ms, html_bytes, error_type)``。
        """
        limit = max_hops or self._max_redirects()
        chain: list[_Hop] = []
        current = url
        seen: set[str] = set()
        total_ms = 0
        error_type = ""
        final_status: int | None = None
        final_url = url
        html_bytes = b""
        visited_host = ""

        for hop_index in range(limit):
            if current in seen:
                chain.append(_Hop(url=current, status=0, location="", elapsed_ms=0))
                return chain, 0, current, total_ms, b"", ErrorType.CONN_FAILED.value
            seen.add(current)
            response = await self.http.fetch_limited(
                current,
                proxy=proxy,
                max_bytes=262144,
                timeout_sec=self._timeout_sec(),
                follow_redirects=False,
            )
            total_ms += response.elapsed_ms
            location = str(response.headers.get("location", "") or "")
            hop = _Hop(
                url=current,
                status=response.status_code,
                location=location,
                elapsed_ms=response.elapsed_ms,
            )
            chain.append(hop)
            if not response.ok or response.status_code == 0:
                return chain, response.status_code or None, current, total_ms, b"", response.error_type
            visited_host = strip_scheme_and_path(current)
            if response.status_code in (301, 302, 303, 307, 308) and location:
                next_url = assure_absolute(location, current)
                final_url = next_url
                current = next_url
                continue
            final_status = response.status_code
            final_url = response.final_url or current
            html_bytes = response.content
            if response.status_code >= 500:
                error_type = ErrorType.HTTP_5XX.value
            elif response.status_code >= 400:
                error_type = ErrorType.CONN_FAILED.value
            _ = visited_host
            _ = hop_index
            return chain, final_status, final_url, total_ms, html_bytes, error_type

        # 超过最大跳数
        return chain, final_status or 0, final_url, total_ms, html_bytes, ErrorType.CONN_FAILED.value

    # ---------------------------------------------------------------- SSL
    async def probe_ssl(self, host: str, port: int = 443) -> SslInfo:
        """SSL 证书探测（executor 中执行）。

        Args:
            host: 主机名。
            port: 端口。

        Returns:
            :class:`SslInfo`。
        """
        loop = asyncio.get_running_loop()
        timeout_sec = 8.0
        if self.settings is not None:
            timeout_sec = float(getattr(self.settings.http, "timeout_connect", 5) or 5) + 3.0
        try:
            return await loop.run_in_executor(None, _sync_probe_ssl, host, port, timeout_sec)
        except Exception as exc:  # noqa: BLE001 - 任何异常降级为未取到证书
            return SslInfo(ok=False, fail_reason=str(exc), error_type=ErrorType.TLS_FAILED.value)

    # ---------------------------------------------------------------- 判定
    def analyze_chain(self, domain: str, chain: list[_Hop], final_status: int | None) -> dict[str, Any]:
        """分析重定向链是否异常。

        Args:
            domain: 目标注册域（归一化）。
            chain: 跳转记录。
            final_status: 最终状态码。

        Returns:
            异常字典，如 ``{"redirect_anomaly": True, "redirect_loop": False, ...}``。
        """
        anomalies: dict[str, Any] = {
            "redirect_anomaly": False,
            "redirect_loop": False,
            "redirect_hops": max(0, len(chain) - 1),
            "cross_domain": False,
            "https_downgrade": False,
            "intermediate_error": False,
        }
        if len(chain) <= 1:
            return anomalies

        hops = len(chain) - 1
        anomalies["redirect_hops"] = hops
        anomalies["redirect_loop"] = len({hop.url for hop in chain}) != len(chain)

        target_root = domain
        last_url = chain[-1].url
        final_host = strip_scheme_and_path(last_url or (chain[-1].location or ""))
        anomalies["cross_domain"] = bool(final_host) and final_host != target_root and (
            target_root not in final_host
        )
        anomalies["https_downgrade"] = any(
            hop.url.lower().startswith("https://") and hop.location.lower().startswith("http://")
            for hop in chain
        )
        anomalies["intermediate_error"] = any(
            400 <= hop.status < 600 for hop in chain[:-1]
        )
        anomalies["redirect_anomaly"] = bool(
            hops > _MAX_REDIRECT_HOPS_ALERT
            or anomalies["redirect_loop"]
            or anomalies["cross_domain"]
            or anomalies["https_downgrade"]
            or anomalies["intermediate_error"]
        )
        _ = final_status
        return anomalies


def _build_ssl_context() -> ssl.SSLContext:
    """构造 SSL 校验上下文：系统 CA 与 certifi CA 取**并集**。

    Windows 的系统根证书存储本机实测仅 64 条，缺 Cloudflare / SSL.com 等
    issuer 链，单独使用会把大量正常 HTTPS 站误判成 ``tls_failed``；
    certifi 提供 99 条权威 CA。``load_verify_locations`` 是**追加**语义，
    两者并集最稳（实测 64 → 165 条，TLSv1.3 握手成功）。

    Returns:
        :class:`ssl.SSLContext`。
    """
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - certifi 缺失时退回系统 CA
        pass
    return ctx


def _sync_probe_ssl(host: str, port: int, timeout_sec: float) -> SslInfo:
    """同步 SSL 握手 + 证书解析（在线程池中执行）。

    Args:
        host: 主机名。
        port: 端口。
        timeout_sec: 超时秒数。

    Returns:
        :class:`SslInfo`。
    """
    info = SslInfo()
    older_context = _build_ssl_context()
    try:
        with socket.create_connection((host, port), timeout=timeout_sec) as sock:
            with older_context.wrap_socket(sock, server_hostname=host) as secure:
                der = secure.getpeercert(binary_form=True)
        if not der:
            info.ok = False
            info.fail_reason = "未取到对端证书"
            info.error_type = ErrorType.TLS_FAILED.value
            return info
        _fill_from_der(info, der)
        info.ok = True
        return info
    except ssl.SSLCertVerificationError as exc:
        # 证书过期 / 域名不匹配：仍然尝试读出证书信息以便计算剩余天数
        unverified = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE
        try:
            with socket.create_connection((host, port), timeout=timeout_sec) as sock:
                with unverified.wrap_socket(sock, server_hostname=host) as secure:
                    der = secure.getpeercert(binary_form=True)
            if der:
                _fill_from_der(info, der)
        except Exception as inner:  # noqa: BLE001
            info.days_left = None
            info.fail_reason = f"{exc}"
            info.ok = False
            info.error_type = ErrorType.TLS_FAILED.value
            _ = inner
            return info
        info.ok = False
        info.error_type = ErrorType.TLS_FAILED.value
        info.fail_reason = f"证书校验失败: {exc}"
        return info
    except ssl.SSLError as exc:
        info.ok = False
        info.error_type = ErrorType.TLS_FAILED.value
        info.fail_reason = f"TLS 握手失败: {exc}"
        return info
    except (socket.timeout, TimeoutError) as exc:
        info.ok = False
        info.error_type = ErrorType.TIMEOUT.value
        info.fail_reason = f"SSL 连接超时: {exc}"
        return info
    except OSError as exc:
        info.ok = False
        info.error_type = ErrorType.CONN_FAILED.value
        info.fail_reason = f"SSL 连接失败: {exc}"
        return info


def _fill_from_der(info: SslInfo, der: bytes) -> None:
    """用 cryptography 解析 DER 证书并填充 SslInfo。

    Args:
        info: 待填充的证书信息实体。
        der: DER 编码的证书字节。
    """
    cert = x509.load_der_x509_certificate(der)
    try:
        info.subject = cert.subject.rfc4514_string()
    except Exception:  # noqa: BLE001
        info.subject = ""
    try:
        info.issuer = cert.issuer.rfc4514_string()
    except Exception:  # noqa: BLE001
        info.issuer = ""
    info.serial = format(cert.serial_number, "x")[:32]
    not_before = cert.not_valid_before_utc.replace(tzinfo=timezone.utc)
    not_after = cert.not_valid_after_utc.replace(tzinfo=timezone.utc)
    info.not_before = not_before.strftime("%Y-%m-%d")
    info.not_after = not_after.strftime("%Y-%m-%d")
    now = datetime.now(tz=timezone.utc)
    info.days_left = (not_after - now).days
    try:
        info.error_type = ErrorType.NONE.value
        _ = cert.signature_hash_algorithm.__class__ is hashes.SHA256
    except Exception:  # noqa: BLE001
        pass
