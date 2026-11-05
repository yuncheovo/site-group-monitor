"""TDK 抓取与解析（Title / Description / Keywords）。

策略（对应架构文档 §1.6）：
    * **默认纯 HTML 静态解析**（httpx 流式读前 512KB + parsel/lxml），不用浏览器；
    * URL 选择：``https://{domain}/`` → 失败回落 ``http://{domain}/``；
    * 编码四级兜底：HTTP CT → ``<meta charset>`` → charset-normalizer → gb18030；
    * 字段缺失一律填空串 + ``missing`` 标记，**不抛错**；
    * 只有开启 ``tdk.render_fallback.enabled`` 且域名在 ``render_whitelist`` 中，
      才用 playwright **异步旁路**（主链路绝不依赖）。

纯函数入口：:func:`parse_html`（输入 bytes/str 出 :class:`TdkInfo`，单测不联网）。
"""

from __future__ import annotations

import re
from typing import Any

from parsel import Selector

from site_group_monitor.models.entities import TdkInfo
from site_group_monitor.models.enums import ErrorType
from site_group_monitor.utils.netutil import clean_text, decode_html, root_url_of
from site_group_monitor.utils.timeutil import epoch_ms

_TITLE_LIMIT = 256
_DESC_LIMIT = 512
_KEYWORDS_LIMIT = 512
_PLACEHOLDER_TITLES = {
    "页面加载中",
    "页面加载中...",
    "页面加载中…",
    "loading",
    "just a moment...",
    "please wait",
    "请稍候",
    "请稍等",
}


def _is_placeholder_title(title: str) -> bool:
    """WAF / JS 壳页标题，不能当真实 TDK。"""
    text = (title or "").strip()
    if not text:
        return False
    if text.lower() in {item.lower() for item in _PLACEHOLDER_TITLES}:
        return True
    compact = text.replace(".", "").replace("…", "").replace(" ", "")
    if compact.lower() in ("页面加载中", "loading"):
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in ("just a moment", "please wait"))


def parse_html(
    raw: bytes | str,
    base_url: str = "",
    *,
    content_type: str = "",
    fallback_meta: bool = True,
    encoding_hint: str = "",
) -> TdkInfo:
    """纯函数：从 HTML 抽取 TDK（不做任何网络/文件 I/O）。

    Args:
        raw: HTML 字节或文本。
        base_url: 来源 URL（写入 ``source_url``）。
        content_type: 响应 Content-Type（用于编码探测）。
        fallback_meta: description 缺失时是否回落 og/twitter。
        encoding_hint: 已知编码（可为空）。

    Returns:
        :class:`TdkInfo`。
    """
    info = TdkInfo(source_url=base_url)
    if raw is None:
        return info

    if isinstance(raw, bytes):
        text, encoding = decode_html(raw, content_type or None)
    else:
        text, encoding = (raw, encoding_hint or "utf-8")
    info.encoding = encoding

    selector = Selector(text=text)

    title = clean_text(selector.xpath("//title/text()").get("") or "", _TITLE_LIMIT)
    # 空标题或 WAF 占位标题时回落 og:title / <h1>
    if not title or _is_placeholder_title(title):
        og_title = clean_text(
            selector.xpath('//meta[@property="og:title"]/@content').get("") or "", _TITLE_LIMIT
        )
        h1_title = clean_text(selector.xpath("//h1//text()").get("") or "", _TITLE_LIMIT)
        fallback = og_title or h1_title
        title = fallback if fallback and not _is_placeholder_title(fallback) else ""
    info.title = title
    info.title_missing = not bool(title)

    description = clean_text(
        selector.xpath('//meta[translate(@name,"DESCRIPTION","description")="description"]/@content').get("")
        or selector.xpath('//meta[@name="Description"]/@content').get("")
        or "",
        _DESC_LIMIT,
    )
    desc_source = "meta" if description else ""
    if not description and fallback_meta:
        description = clean_text(
            selector.xpath('//meta[@property="og:description"]/@content').get("") or "", _DESC_LIMIT
        )
        desc_source = "og" if description else ""
    if not description and fallback_meta:
        description = clean_text(
            selector.xpath('//meta[@name="twitter:description"]/@content').get("") or "", _DESC_LIMIT
        )
        desc_source = "twitter" if description else ""
    info.description = description
    info.desc_source = desc_source
    info.desc_missing = not bool(description)

    keywords = clean_text(
        selector.xpath('//meta[translate(@name,"KEYWORDS","keywords")="keywords"]/@content').get("") or "",
        _KEYWORDS_LIMIT,
    )
    info.keywords = keywords
    info.keywords_missing = not bool(keywords)

    if not title and not description and not keywords:
        info.parse_warning = "TDK 全部字段缺失"
    elif _looks_like_shell(text):
        info.parse_warning = "疑似 JS 渲染空壳页面（如需准确 TDK 请启用 render_fallback）"

    info.error_type = ErrorType.NONE.value
    info.apply_hashes()
    return info


def _looks_like_shell(text: str) -> bool:
    """判断是否像 SPA 空壳页（``<noscript>`` 提示或 body 极短）。

    Args:
        text: HTML 文本。

    Returns:
        疑似返回 True。
    """
    if "noscript" in text.lower() and len(text) < 8192:
        return True
    body = re.sub(r"<script[\s\S]*?</script>", "", text, flags=re.IGNORECASE)
    visible = clean_text(re.sub(r"<[^>]+>", " ", body))
    return len(visible) < 80


class TdkParser:
    """TDK 抓取器（网络层），内部降级捕获所有异常。"""

    def __init__(
        self,
        http: Any,
        settings: Any,
        logger: Any | None = None,
        browser: Any | None = None,
    ) -> None:
        """构造抓取器。

        Args:
            http: HTTP 客户端。
            settings: 配置对象。
            logger: 日志记录器。
            browser: 本机浏览器会话（可选）。
        """
        self.http = http
        self.settings = settings
        self.logger = logger
        self.browser = browser

    # ---------------------------------------------------------------- 配置
    def _max_bytes(self) -> int:
        """流式读取上限字节数。"""
        if self.settings is None:
            return 524288
        return int(getattr(self.settings.tdk, "max_bytes", 524288) or 524288)

    def _timeout_sec(self) -> float:
        """TDK 请求总超时。"""
        if self.settings is None:
            return 15.0
        return float(getattr(self.settings.tdk, "timeout_sec", 15) or 15)

    def _fallback_meta(self) -> bool:
        """是否启用 og/twitter 回落。"""
        if self.settings is None:
            return True
        return bool(getattr(self.settings.tdk, "fallback_meta", True))

    # ---------------------------------------------------------------- 抓取
    async def fetch_and_parse(
        self,
        domain: str,
        root_url: str = "",
        *,
        proxy: Any | None = None,
        browser: Any | None = None,
    ) -> TdkInfo:
        """抓取首页并解析 TDK。

        Args:
            domain: 归一化域名。
            root_url: 人工覆盖的首页 URL（为空则自动推导）。

        Returns:
            :class:`TdkInfo`；失败时带 ``error_type``。
        """
        info = TdkInfo(source_url=root_url or root_url_of(domain))
        if not domain:
            info.error_type = ErrorType.CONN_FAILED.value
            return info
        candidates = _homepage_candidates(domain, root_url)
        routes: list[Any] = []
        if proxy is not None:
            routes.append(proxy)
        routes.append(None)

        last_error = ErrorType.CONN_FAILED.value
        best: TdkInfo | None = None
        page = browser if browser is not None else self.browser
        if page is not None and getattr(page, "is_available", lambda: False)():
            for url in candidates[:1]:
                if not url:
                    continue
                try:
                    response = await page.fetch(url, proxy=proxy)
                except Exception as exc:  # noqa: BLE001
                    last_error = ErrorType.CONN_FAILED.value
                    if self.logger is not None:
                        self.logger.debug("tdk: 浏览器抓取 %s 异常 %s", url, exc)
                    continue
                raw = response.content or (response.text.encode("utf-8", errors="ignore") if response.text else b"")
                if not raw:
                    continue
                parsed = parse_html(
                    raw,
                    response.final_url or url,
                    content_type="text/html",
                    fallback_meta=self._fallback_meta(),
                )
                usable = bool(parsed.title) and not parsed.title_missing
                if usable:
                    parsed.error_type = ErrorType.NONE.value
                    return parsed
                if best is None:
                    best = parsed
        for try_proxy in routes:
            for url in candidates:
                if not url:
                    continue
                try:
                    response = await self.http.fetch_limited(
                        url,
                        proxy=try_proxy,
                        max_bytes=self._max_bytes(),
                        timeout_sec=self._timeout_sec(),
                    )
                except Exception as exc:  # noqa: BLE001 - 采集内核异常不得冒泡
                    last_error = ErrorType.CONN_FAILED.value
                    if self.logger is not None:
                        self.logger.debug("tdk: %s 抓取异常 %s", url, exc)
                    continue
                if not response.ok or response.status_code == 0 or not response.content:
                    last_error = _map_error(response.error_type)
                    continue
                parsed = parse_html(
                    response.content,
                    response.final_url or url,
                    content_type=response.headers.get("content-type", ""),
                    fallback_meta=self._fallback_meta(),
                )
                usable = bool(parsed.title) and not parsed.title_missing
                if response.status_code >= 400:
                    last_error = ErrorType.CONN_FAILED.value
                    if usable and best is None:
                        best = parsed
                    continue
                parsed.error_type = ErrorType.NONE.value
                if parsed.title_missing and self._render_allowed(domain, parsed):
                    rendered = await self._render(url, domain)
                    if rendered:
                        parsed = parse_html(
                            rendered, response.final_url or url, fallback_meta=self._fallback_meta()
                        )
                        usable = bool(parsed.title) and not parsed.title_missing
                if usable:
                    return parsed
                if best is None:
                    best = parsed

            if best is not None and best.title and not best.title_missing:
                return best

        if best is not None:
            return best

        info.error_type = last_error
        return info

    # ---------------------------------------------------------------- 渲染兜底
    def _render_allowed(self, domain: str, info: TdkInfo) -> bool:
        """判断是否允许使用 playwright 渲染兜底。

        Args:
            domain: 域名。
            info: 静态解析结果。

        Returns:
            允许返回 True。
        """
        if self.settings is None:
            return False
        cfg = getattr(self.settings.tdk, "render_fallback", None)
        if cfg is None or not getattr(cfg, "enabled", False):
            return False
        whitelist = list(getattr(cfg, "render_whitelist", []) or [])
        if domain not in whitelist:
            return False
        return bool(info.title_missing) or bool(info.parse_warning)

    async def _render(self, url: str, domain: str) -> str:
        """用 playwright 渲染页面（optional，失败静默返回空串）。

        Args:
            url: 页面 URL。
            domain: 域名（日志用）。

        Returns:
            渲染后的 HTML；不可用返回空串。
        """
        try:
            # 用 importlib 动态解析，避免 PyInstaller 静态分析把 playwright
            # 的 driver（含 node.exe，约 99MB）打进 exe。playwright 是可选
            # 兜底依赖，运行时才按需加载，缺失即静默跳过。
            import importlib

            module = importlib.import_module("playwright.async_api")
            async_playwright = module.async_playwright
        except Exception:  # noqa: BLE001 - 未安装即跳过
            if self.logger is not None:
                self.logger.info("tdk: %s 需要 playwright 渲染但未安装 optional 依赖", domain)
            return ""
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                html = await page.content()
                await browser.close()
                return html or ""
        except Exception as exc:  # noqa: BLE001
            if self.logger is not None:
                self.logger.warning("tdk: %s playwright 渲染失败 %s", domain, exc)
            return ""


def _homepage_candidates(domain: str, root_url: str = "") -> list[str]:
    """首页候选：https / http，以及无 www 时补 www。"""
    urls: list[str] = []
    primary = (root_url or "").strip() or root_url_of(domain, prefer_https=True)
    for item in (
        primary,
        root_url_of(domain, prefer_https=True),
        root_url_of(domain, prefer_https=False),
    ):
        if item and item not in urls:
            urls.append(item)
    if domain and not domain.lower().startswith("www."):
        for item in (f"https://www.{domain}/", f"http://www.{domain}/"):
            if item not in urls:
                urls.append(item)
    return urls


def _map_error(error_type: str) -> str:
    """把 HTTP 层错误码映射为实体 ErrorType。

    Args:
        error_type: HTTP 层错误类型字符串。

    Returns:
        :class:`ErrorType` 值字符串。
    """
    mapping = {
        "dns_failed": ErrorType.DNS_FAILED.value,
        "conn_failed": ErrorType.CONN_FAILED.value,
        "timeout": ErrorType.TIMEOUT.value,
        "proxy_failed": ErrorType.CONN_FAILED.value,
        "network_error": ErrorType.CONN_FAILED.value,
    }
    return mapping.get(error_type, ErrorType.CONN_FAILED.value)


async def measure_fetch_ms(func: Any) -> tuple[Any, int]:  # pragma: no cover - 便捷工具
    """测量某个协程的执行耗时（毫秒）。

    Args:
        func: 无参协程函数。

    Returns:
        ``(结果, 耗时毫秒)``。
    """
    start = epoch_ms()
    result = await func()
    return result, epoch_ms() - start
