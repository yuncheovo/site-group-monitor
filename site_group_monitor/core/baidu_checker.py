"""百度收录检测器。

通过 ``site:域名`` 查询百度，复用 :mod:`baidu_parser` 的保守三态判定规则。
任何网络、浏览器或解析异常都在模块边界降级为 ``unknown``，不会中断批次。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

from site_group_monitor.core.baidu_parser import parse_and_decide
from site_group_monitor.core.checker_base import IndexCheckContext, SearchEngineChecker
from site_group_monitor.models.entities import IndexResult
from site_group_monitor.models.enums import ErrorType, IndexReasonCode, IndexStatus


class BaiduIndexChecker(SearchEngineChecker):
    """百度 ``site:`` 收录检测实现。"""

    engine = "baidu"

    async def check(
        self,
        domain: str,
        ctx: IndexCheckContext,
        browser: Any | None = None,
    ) -> IndexResult:
        """查询一个域名并返回保守的三态判定。"""
        started = time.perf_counter()
        config = getattr(getattr(self.settings, "engine", None), "baidu", None)
        search_url = str(getattr(config, "search_url", "https://www.baidu.com/s"))
        timeout = float(getattr(config, "timeout_sec", 25) or 25)
        rn = max(1, int(getattr(config, "rn", 10) or 10))
        params = {"wd": f"site:{domain}", "rn": rn, "ie": "utf-8"}
        proxy = ctx.proxy
        response = None

        try:
            limiter_key = proxy.key if proxy is not None else "direct"
            if self.limiter is not None:
                await self.limiter.acquire(limiter_key)

            if browser is not None and getattr(browser, "fetch", None) is not None:
                url = f"{search_url}?{urlencode(params)}"
                response = await browser.fetch(url, proxy=proxy, referer="https://www.baidu.com/")
            else:
                attempts = min(2, max(1, int(getattr(config, "max_retry", 2) or 2)))
                backoff = tuple(getattr(config, "retry_backoff_sec", [1.0]) or [1.0])
                response = await self.http.get_with_retry(
                    search_url,
                    params=params,
                    proxy=proxy,
                    timeout_sec=timeout,
                    attempts=attempts,
                    backoff_sec=backoff,
                    headers={
                        "Accept-Language": "zh-CN,zh;q=0.9",
                        "Referer": "https://www.baidu.com/",
                    },
                )

            elapsed = int((time.perf_counter() - started) * 1000)
            if response is None or not response.ok or not response.text:
                return self._network_failure(response, proxy, elapsed)

            result = parse_and_decide(response.text, domain)
            result.engine = self.engine
            result.http_status = response.status_code or None
            result.elapsed_ms = response.elapsed_ms or elapsed
            result.proxy_id = proxy.id if proxy is not None else None

            meta = {
                "engine": self.engine,
                "query": params["wd"],
                "http_status": result.http_status,
                "final_url": response.final_url,
                "proxy_id": result.proxy_id,
                "reason": str(result.reason),
            }
            # 明确结果只保留压缩 HTML；异常结果同时保留可用截图，便于排障。
            result.evidence_path = self.evidence.save(domain, response.text, meta)
            if result.status == IndexStatus.UNKNOWN and response.screenshot:
                screenshot = self.evidence.save_png(domain, response.screenshot, meta)
                if screenshot:
                    result.evidence_path = screenshot

            if result.reason == IndexReasonCode.CAPTCHA:
                result.error_type = ErrorType.CAPTCHA.value
                result.error_msg = "百度触发验证码/风控"
                if self.proxies is not None:
                    self.proxies.report_captcha(proxy)
                    max_rotate = max(
                        0,
                        int(getattr(getattr(config, "captcha", None), "max_rotate", 3) or 0),
                    )
                    if ctx.attempt <= max_rotate and hasattr(self.proxies, "rotate_after_captcha"):
                        replacement = await self.proxies.rotate_after_captcha(proxy)
                        if replacement is not None:
                            retry_ctx = IndexCheckContext(
                                task_id=ctx.task_id,
                                attempt=ctx.attempt + 1,
                                proxy=replacement,
                                engine=ctx.engine,
                                run_mode=ctx.run_mode,
                            )
                            try:
                                return await self.check(domain, retry_ctx, browser=browser)
                            finally:
                                self.proxies.release(replacement)
            elif result.status == IndexStatus.UNKNOWN:
                result.error_type = ErrorType.PARSE_FAILED.value
                result.error_msg = "百度结果页结构异常，需人工确认"
                if self.proxies is not None:
                    self.proxies.report_fail(proxy, kind="fail")
            else:
                result.error_type = ErrorType.NONE.value
                if self.proxies is not None:
                    self.proxies.report_ok(proxy)
            return result
        except Exception as exc:  # noqa: BLE001 - 采集边界必须安全降级
            elapsed = int((time.perf_counter() - started) * 1000)
            if self.logger is not None:
                self.logger.warning("baidu_checker: %s 查询失败: %s", domain, exc)
            if self.proxies is not None:
                self.proxies.report_fail(proxy, kind="fail")
            result = self.unknown(
                IndexReasonCode.NETWORK_ERROR,
                message=str(exc),
                elapsed_ms=elapsed,
            )
            result.error_type = ErrorType.CONN_FAILED.value
            result.proxy_id = proxy.id if proxy is not None else None
            return result

    def _network_failure(self, response: Any, proxy: Any, elapsed: int) -> IndexResult:
        """把 HTTP/浏览器失败映射为稳定的原因码与错误类型。"""
        raw_type = str(getattr(response, "error_type", "") or "network_error")
        message = str(getattr(response, "error_msg", "") or "百度查询没有返回有效内容")
        status = int(getattr(response, "status_code", 0) or 0) or None
        if raw_type == ErrorType.TIMEOUT.value:
            reason = IndexReasonCode.TIMEOUT
            error_type = ErrorType.TIMEOUT.value
        elif raw_type == "proxy_failed":
            reason = IndexReasonCode.PROXY_FAILED
            error_type = ErrorType.CONN_FAILED.value
        else:
            reason = IndexReasonCode.NETWORK_ERROR
            error_type = ErrorType.CONN_FAILED.value
        if self.proxies is not None:
            self.proxies.report_fail(proxy, kind="proxy" if raw_type == "proxy_failed" else "fail")
        result = self.unknown(
            reason,
            message=message,
            http_status=status,
            elapsed_ms=elapsed,
        )
        result.error_type = error_type
        result.proxy_id = proxy.id if proxy is not None else None
        return result
