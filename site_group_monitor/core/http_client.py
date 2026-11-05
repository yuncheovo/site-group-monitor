"""异步 HTTP 客户端封装。

要点：
    * 每个代理独立 ``httpx.AsyncClient``（**CookieJar 隔离**，便于百度 BAIDUID 预热）；
    * 统一的 timeout（connect / read / write / pool）与精细错误降级；
    * 支持「受限流式读取」给 TDK 使用（只读前 N 字节，避免大落地页拖慢批次）；
    * 所有异常在边界转换为 :class:`RawResponse`（``ok=False``），**不向上抛**；
    * tenacity 做一层轻量退避重试（架构约定：整体重试层数 <= 2）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from site_group_monitor.core.user_agents import UserAgentPool
from site_group_monitor.models.entities import ProxyEndpoint


@dataclass
class RawResponse:
    """统一的 HTTP 响应结果（含失败降级信息）。"""

    ok: bool = False
    status_code: int = 0
    url: str = ""
    final_url: str = ""
    text: str = ""
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    error_type: str = ""
    error_msg: str = ""
    truncated: bool = False
    screenshot: bytes = b""

    @property
    def is_redirect(self) -> bool:
        """是否为 3xx 跳转响应。"""
        return 300 <= self.status_code < 400


class HttpClient:
    """带代理隔离与指纹轮换的 HTTP 客户端。

    Args:
        settings: 全局配置对象。
        logger: 日志记录器。
        http2: 是否启用 HTTP/2（默认关闭，降低指纹识别）。
    """

    def __init__(self, settings: Any | None = None, logger: Any | None = None,
                 http2: bool | None = None) -> None:
        self.settings = settings
        self.logger = logger
        self._http2 = bool(http2) if http2 is not None else self._configured_http2()
        self._clients: dict[str, httpx.AsyncClient] = {}
        pool_size = 20
        rotate = True
        if settings is not None:
            pool_size = int(getattr(settings.http, "headers_pool_size", 20) or 20)
            rotate = bool(getattr(settings.http, "user_agent_rotation", True))
        self.ua_pool = UserAgentPool(pool_size=pool_size, rotate=rotate)
        self._timeout = self._build_timeout()

    # ---------------------------------------------------------------- 配置
    def _configured_http2(self) -> bool:
        """读取配置中的 http2 开关（默认 False）。"""
        if self.settings is None:
            return False
        return bool(getattr(self.settings.engine.baidu, "http2", False))

    def _build_timeout(self) -> httpx.Timeout:
        """按配置构造 timeout 对象。"""
        http_cfg = getattr(self.settings, "http", None) if self.settings else None
        connect = float(getattr(http_cfg, "timeout_connect", 5) or 5)
        read = float(getattr(http_cfg, "timeout_read", 10) or 10)
        total = float(getattr(http_cfg, "timeout_total", 20) or 20)
        pool = float(getattr(http_cfg, "timeout_pool", 3) or 3)
        return httpx.Timeout(total, connect=connect, read=read, write=read, pool=pool)

    def timeout_for(self, total_sec: float | None = None) -> httpx.Timeout:
        """返回定制总超时的 timeout。

        Args:
            total_sec: 总超时秒数，None 使用默认。

        Returns:
            ``httpx.Timeout`` 实例。
        """
        if not total_sec:
            return self._timeout
        return httpx.Timeout(
            total_sec,
            connect=self._timeout.connect,
            read=self._timeout.read,
            write=self._timeout.write,
            pool=self._timeout.pool,
        )

    # ---------------------------------------------------------------- 连接池
    @staticmethod
    def proxy_key(proxy: ProxyEndpoint | str | None) -> str:
        """计算代理 key（``direct`` 表示直连）。

        Args:
            proxy: 代理实体、URL 或 None。

        Returns:
            key 字符串。
        """
        if proxy is None:
            return "direct"
        if isinstance(proxy, str):
            return proxy
        return proxy.key

    def _client(self, proxy: ProxyEndpoint | str | None) -> httpx.AsyncClient:
        """取得（或创建）该代理对应的 AsyncClient —— CookieJar 按代理隔离。

        Args:
            proxy: 代理实体 / URL / None。

        Returns:
            ``httpx.AsyncClient`` 实例。
        """
        key = self.proxy_key(proxy)
        cached = self._clients.get(key)
        if cached is not None and not cached.is_closed:
            return cached
        proxy_url: str | None = None
        if isinstance(proxy, ProxyEndpoint):
            proxy_url = proxy.url
        elif isinstance(proxy, str):
            proxy_url = proxy
        verify_ssl = True
        if self.settings is not None:
            verify_ssl = bool(getattr(self.settings.http, "verify_ssl", True))
        client = httpx.AsyncClient(
            proxy=proxy_url,
            timeout=self._timeout,
            http2=self._http2,
            verify=verify_ssl,
            follow_redirects=False,
            # 不继承系统环境变量里的 http_proxy/https_proxy：
            # 否则「无代理降级直连」会被环境代理劫持，代理池配置形同虚设，
            # 且环境代理不通时表现为全站 timeout（排查极难）。
            trust_env=False,
        )
        self._clients[key] = client
        return client

    async def close(self, key: str | None = None) -> None:
        """关闭并清理 HTTP 客户端。

        Args:
            key: 指定代理 key；None 表示全部关闭。
        """
        targets = [key] if key else list(self._clients.keys())
        for target in targets:
            client = self._clients.pop(target, None)
            if client is None:
                continue
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - 关闭异常无需冒泡
                pass

    def cookies_snapshot(self, proxy: ProxyEndpoint | str | None = None) -> dict[str, str]:
        """读取某个 client 的 Cookie（用于判断是否已预热 BAIDUID）。

        Args:
            proxy: 代理实体 / URL / None。

        Returns:
            ``{name: value}`` 字典。
        """
        try:
            client = self._clients.get(self.proxy_key(proxy))
            if client is None:
                return {}
            return {str(cookie.name): str(cookie.value) for cookie in client.cookies.jar}
        except Exception:  # noqa: BLE001
            return {}

    # ---------------------------------------------------------------- 请求
    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        proxy: ProxyEndpoint | None = None,
        timeout_sec: float | None = None,
        follow_redirects: bool = True,
        use_ua: bool = True,
    ) -> RawResponse:
        """执行一次 GET（失败不抛异常）。

        Args:
            url: 目标 URL。
            params: query 参数。
            headers: 附加请求头。
            proxy: 使用的代理（None 为直连）。
            timeout_sec: 覆盖总超时。
            follow_redirects: 是否自动跟跳。
            use_ua: 是否注入随机 UA。

        Returns:
            :class:`RawResponse`。
        """
        request_headers = dict(headers or {})
        if use_ua and not any(key.lower() == "user-agent" for key in request_headers):
            request_headers.update(self.ua_pool.next_headers())
        client = self._client(proxy)
        try:
            response = await client.get(
                url,
                params=params,
                headers=request_headers,
                timeout=self.timeout_for(timeout_sec),
                follow_redirects=follow_redirects,
            )
            return self._to_raw(response, url)
        except httpx.TimeoutException as exc:
            return RawResponse(ok=False, url=url, error_type="timeout", error_msg=str(exc))
        except httpx.ProxyError as exc:
            return RawResponse(ok=False, url=url, error_type="proxy_failed", error_msg=str(exc))
        except httpx.ConnectError as exc:
            return RawResponse(ok=False, url=url, error_type="conn_failed", error_msg=str(exc))
        except httpx.HTTPError as exc:
            return RawResponse(ok=False, url=url, error_type="network_error", error_msg=str(exc))
        except Exception as exc:  # noqa: BLE001 - 兜底，绝不冒泡
            return RawResponse(ok=False, url=url, error_type="network_error", error_msg=str(exc))

    async def get_with_retry(
        self,
        url: str,
        *,
        attempts: int = 2,
        backoff_sec: Sequence[float] = (0.5, 2.0),
        **kwargs: Any,
    ) -> RawResponse:
        """带退避重试的 GET（网络层重试，最多 1 层）。

        Args:
            url: 目标 URL。
            attempts: 总尝试次数。
            backoff_sec: 退避区间。
            **kwargs: 透传给 :meth:`get`。

        Returns:
            最后一次的 :class:`RawResponse`。
        """
        last = RawResponse(url=url, error_type="network_error", error_msg="未发起请求")
        for index in range(max(1, int(attempts))):
            last = await self.get(url, **kwargs)
            if last.ok or last.status_code:
                return last
            if index < attempts - 1:
                await self._sleep_for_retry(backoff_sec, index)
        return last

    async def fetch_limited(
        self,
        url: str,
        *,
        proxy: ProxyEndpoint | None = None,
        max_bytes: int = 524288,
        timeout_sec: float | None = None,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> RawResponse:
        """流式读取前 N 字节（TDK 抓取用，防止超大页面拖慢批次）。

        Args:
            url: 目标 URL。
            proxy: 使用的代理。
            max_bytes: 读取上限字节数。
            timeout_sec: 覆盖总超时。
            headers: 附加请求头。
            follow_redirects: 是否自动跟跳（状态检测需手动逐跳时传 False）。

        Returns:
            :class:`RawResponse`，``truncated`` 标记是否被截断。
        """
        request_headers = dict(headers or {})
        if not any(key.lower() == "user-agent" for key in request_headers):
            request_headers.update(self.ua_pool.next_headers())
        client = self._client(proxy)
        buffer = bytearray()
        # httpx 的 response.elapsed 在 stream() 模式下恒为 0（它只在
        # 非流式响应完成后由 httpx 自己回填）。之前直接取 elapsed 导致
        # 所有站点 response_ms 都记成 0，响应慢的站完全看不出问题。
        # 这里自己打点：从发起请求到读完 body。
        started = time.perf_counter()
        try:
            async with client.stream(
                "GET",
                url,
                headers=request_headers,
                timeout=self.timeout_for(timeout_sec),
                follow_redirects=follow_redirects,
            ) as response:
                async for chunk in response.aiter_bytes(8192):
                    buffer.extend(chunk)
                    if len(buffer) >= max_bytes:
                        break
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                raw = RawResponse(
                    ok=True,
                    status_code=response.status_code,
                    url=url,
                    final_url=str(response.url),
                    content=bytes(buffer[:max_bytes]),
                    headers={key: value for key, value in response.headers.items()},
                    elapsed_ms=elapsed_ms,
                    truncated=len(buffer) >= max_bytes,
                )
                raw.history = [
                    {"url": str(item.url), "status": item.status_code}
                    for item in getattr(response, "history", [])
                ]
                return raw
        except httpx.TimeoutException as exc:
            return RawResponse(ok=False, url=url, error_type="timeout", error_msg=str(exc))
        except httpx.ProxyError as exc:
            return RawResponse(ok=False, url=url, error_type="proxy_failed", error_msg=str(exc))
        except httpx.ConnectError as exc:
            return RawResponse(ok=False, url=url, error_type="conn_failed", error_msg=str(exc))
        except httpx.HTTPError as exc:
            return RawResponse(ok=False, url=url, error_type="network_error", error_msg=str(exc))
        except Exception as exc:  # noqa: BLE001
            return RawResponse(ok=False, url=url, error_type="network_error", error_msg=str(exc))

    # ---------------------------------------------------------------- 工具
    async def _sleep_for_retry(self, backoff: Sequence[float], index: int) -> None:
        """重试前的退避等待。

        注意：本项目约定「限速/等待」只出现在 rate_limiter 与 proxy_pool；
        这里是 **网络层重试退避**（架构文档 §7.10 允许的 HttpClient 层重试），保持最小且可控。

        Args:
            backoff: 退避秒数序列。
            index: 当前重试序号。
        """
        seconds = float(backoff[min(index, len(backoff) - 1)]) if backoff else 0.5
        await asyncio.sleep(max(0.0, seconds))

    @staticmethod
    def _to_raw(response: httpx.Response, requested_url: str) -> RawResponse:
        """把 httpx.Response 转为 RawResponse（自动解码文本）。

        Args:
            response: httpx 响应。
            requested_url: 原始请求 URL。

        Returns:
            :class:`RawResponse`。
        """
        raw = RawResponse(
            ok=response.status_code > 0,
            status_code=response.status_code,
            url=requested_url,
            final_url=str(response.url),
            content=response.content,
            text=response.text,
            headers={key: value for key, value in response.headers.items()},
            elapsed_ms=int((response.elapsed.total_seconds() if response.elapsed else 0) * 1000),
            history=[
                {"url": str(item.url), "status": item.status_code}
                for item in getattr(response, "history", [])
            ],
        )
        return raw


# tenacity 保留给未来「策略化重试」使用（当前 API 已内建退避，避免嵌套超过 2 层）
_RETRY_POLICY = AsyncRetrying(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.ConnectError)),
    reraise=False,
)
