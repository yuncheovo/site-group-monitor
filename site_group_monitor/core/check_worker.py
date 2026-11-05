"""单站检测编排 Worker：收录 + TDK + 状态三路并发 → 一条 :class:`CheckResult`。

设计约束（对应架构文档 §1.7 与 T03 验收 ⑧）：
    * 三路子任务并发执行（I/O 密集，互不阻塞）；
    * **任一子项异常都内部降级**，绝不抛出打断整批；
    * 单站软超时（默认 25s）由本层 ``asyncio.wait_for`` 兜底；
    * 收录判定来自 ``IndexResult`` 三态；TDK / 状态失败只影响对应字段，不改三态。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from site_group_monitor.core.checker_base import IndexCheckContext, SearchEngineChecker
from site_group_monitor.models.entities import CheckResult, Site, SslInfo, TdkInfo
from site_group_monitor.models.enums import ErrorType, IndexReasonCode, IndexStatus, RunMode
from site_group_monitor.utils.timeutil import epoch_ms, utcnow_iso


class SiteCheckWorker:
    """单站检测编排器（不含批次调度，调度在 ``CheckService``）。"""

    def __init__(
        self,
        index_checker: SearchEngineChecker,
        tdk_parser: Any,
        status_checker: Any,
        settings: Any,
        logger: Any | None = None,
    ) -> None:
        """构造 Worker。

        Args:
            index_checker: 搜索引擎收录检测器（当前为百度）。
            tdk_parser: TDK 抓取器。
            status_checker: 站点状态检测器。
            settings: 配置对象。
            logger: 日志记录器。
        """
        self.index_checker = index_checker
        self.tdk_parser = tdk_parser
        self.status_checker = status_checker
        self.settings = settings
        self.logger = logger
        self.last_network = ""

    def _soft_timeout(self) -> float:
        """单站软超时秒数。"""
        if self.settings is None:
            return 25.0
        base = float(getattr(self.settings.engine.baidu, "timeout_sec", 25) or 25) + 5.0
        browser = getattr(getattr(self.settings.engine.baidu, "browser", None), "enabled", False)
        return base + 40.0 if browser else base

    async def run(
        self,
        site: Site,
        task_id: str,
        run_mode: RunMode | str = RunMode.FULL,
    ) -> CheckResult:
        """检测一个站点，输出完整 CheckResult。

        Args:
            site: 站点实体。
            task_id: 本轮任务 UUID。
            run_mode: 运行模式。

        Returns:
            :class:`CheckResult`（保证不为 None）。
        """
        started = epoch_ms()
        result = CheckResult(
            site_id=int(site.id or 0),
            task_id=task_id,
            run_mode=str(run_mode),
            checked_at=utcnow_iso(),
            index_status=IndexStatus.UNKNOWN.value,
            index_reason=IndexReasonCode.PARSE_FAILED.value,
        )
        domain = site.domain
        root_url = site.effective_root_url()
        self.last_network = ""
        pool = getattr(self.index_checker, "proxies", None)
        proxy = None
        acquired = False
        if pool is not None:
            try:
                proxy_wait = float(
                    getattr(getattr(self.settings, "proxy", None), "acquire_timeout_sec", 5.0) or 5.0
                )
                proxy = await pool.acquire(timeout_sec=max(0.5, proxy_wait))
                acquired = proxy is not None
            except Exception:  # noqa: BLE001 - 代理拿不到时直连降级
                proxy = None
        self.last_network = describe_network_path(self.settings, proxy)
        context = IndexCheckContext(
            task_id=task_id, attempt=1, proxy=proxy,
            engine=getattr(self.index_checker, "engine", "baidu"), run_mode=str(run_mode),
        )

        timeout = self._soft_timeout()
        browser_pool = getattr(self, "browser", None)
        page = None
        if browser_pool is not None and getattr(browser_pool, "acquire", None) is not None:
            try:
                if getattr(browser_pool, "is_available", lambda: False)():
                    page = await browser_pool.acquire()
            except Exception:  # noqa: BLE001
                page = None
        status_task = asyncio.create_task(
            self.status_checker.probe(domain, root_url, proxy=proxy)
        )
        try:
            index_coro = self.index_checker.check(domain, context, browser=page)
        except TypeError:
            index_coro = self.index_checker.check(domain, context)
        index_task = asyncio.create_task(index_coro)
        index_result_raw: Any = None
        tdk_result_raw: Any = None
        status_result_raw: Any = None
        tdk_task: Any = None
        try:
            # 无浏览器页时 TDK 与收录可完全并行；共享浏览器页时仍顺序导航，避免互相覆盖。
            if page is None:
                try:
                    tdk_coro = self.tdk_parser.fetch_and_parse(
                        domain, root_url, proxy=proxy, browser=None
                    )
                except TypeError:
                    tdk_coro = self.tdk_parser.fetch_and_parse(domain, root_url, proxy=proxy)
                tdk_task = asyncio.create_task(tdk_coro)
            # 同一窗口先查收录再跳首页查 TDK，不另开浏览器、站点之间也不关进程
            index_result_raw = await _guarded(index_task, timeout, IndexStatus.UNKNOWN, timeout / 2)
            if tdk_task is None:
                try:
                    tdk_coro = self.tdk_parser.fetch_and_parse(
                        domain, root_url, proxy=proxy, browser=page
                    )
                except TypeError:
                    tdk_coro = self.tdk_parser.fetch_and_parse(domain, root_url, proxy=proxy)
                tdk_task = asyncio.create_task(tdk_coro)
            tdk_result_raw, status_result_raw = await asyncio.gather(
                _guarded(tdk_task, timeout, None, timeout),
                _guarded(status_task, timeout, None, timeout),
                return_exceptions=False,
            )
        finally:
            for task in (index_task, tdk_task, status_task):
                if task is not None and not task.done():
                    task.cancel()
            if page is not None and browser_pool is not None:
                with contextlib.suppress(Exception):
                    await browser_pool.release(page)
            if acquired and pool is not None and proxy is not None:
                try:
                    pool.release(proxy)
                except Exception:  # noqa: BLE001
                    pass

        # ---------------- 收录 ----------------
        from site_group_monitor.models.entities import IndexResult

        index_result = index_result_raw if isinstance(index_result_raw, IndexResult) else None
        if index_result is not None:
            result.index_status = str(index_result.status)
            result.index_reason = str(index_result.reason)
            result.index_confidence = float(index_result.confidence or 0.0)
            result.index_result_count = int(index_result.result_count or 0)
            result.index_top_url = index_result.top_url or ""
            result.evidence_path = index_result.evidence_path or ""
            result.proxy_id = index_result.proxy_id
            # 百度检索页自身 HTTP 状态码（仅做旁路记录，绝不覆盖站点健康字段）
            result.index_http_status = index_result.http_status
            if index_result.error_type and index_result.error_type != ErrorType.NONE.value:
                result.error_type = index_result.error_type
                result.error_msg = index_result.error_msg
        else:
            result.index_status = IndexStatus.UNKNOWN.value
            result.index_reason = IndexReasonCode.TIMEOUT.value
            if result.error_type in (None, "", ErrorType.NONE.value):
                result.error_type = ErrorType.TIMEOUT.value
            result.error_msg = result.error_msg or "收录检测超时/失败"

        # ---------------- TDK ----------------
        result.tdk = _coerce_tdk(tdk_result_raw, root_url)

        # ---------------- 状态 / SSL ----------------
        status = _coerce_status(status_result_raw)
        result.http_status = status.http_status if status.http_status is not None else result.http_status
        result.response_ms = status.response_ms
        result.final_url = status.final_url
        result.redirect_chain = list(status.redirect_chain)
        result.anomalies = dict(status.anomalies)
        if self.last_network:
            result.anomalies["network"] = self.last_network
        result.ssl = status.ssl if isinstance(status.ssl, SslInfo) else SslInfo()
        if not status.dns_ok:
            result.error_type = result.error_type if result.error_type != ErrorType.NONE.value \
                else ErrorType.DNS_FAILED.value
            result.error_msg = result.error_msg or "DNS 解析失败"
        elif status.error_type and status.error_type != ErrorType.NONE.value \
                and result.error_type == ErrorType.NONE.value:
            result.error_type = status.error_type
            result.error_msg = status.error_msg

        if result.http_status is not None and result.http_status >= 400 and status.dns_ok:
            result.anomalies.setdefault("http_error", True)

        result.duration_ms = max(0, epoch_ms() - started)
        if self.logger is not None:
            self.logger.debug(
                "worker: %s -> %s/%s %sms", domain, result.index_status, result.index_reason,
                result.duration_ms,
            )
        return result


async def _guarded(task: Any, timeout: float, default: Any, wait_for: float | None = None) -> Any:
    """等待单个任务完成，异常/超时返回默认值。

    Args:
        task: asyncio Task。
        timeout: 本步骤软超时。
        default: 失败时的返回值。
        wait_for: ``asyncio.wait_for`` 的超时（可与 timeout 不同）。

    Returns:
        任务结果或默认值。
    """
    seconds = wait_for if wait_for is not None else timeout
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.1, seconds))
    except asyncio.TimeoutError:
        if not task.done():
            task.cancel()
        return default
    except asyncio.CancelledError:
        # 外层批次取消必须继续向上传播；吞掉该异常会让 Worker 转而执行后续
        # TDK/状态步骤，表现为点击“取消检测”后仍持续几十秒。
        if not task.done():
            task.cancel()
        raise
    except Exception:  # noqa: BLE001 - 采集内核异常一律降级
        return default


def _coerce_tdk(value: Any, source_url: str) -> TdkInfo:
    """把阶段产出安全地转为 TdkInfo。

    Args:
        value: 阶段结果。
        source_url: 兜底的来源 URL。

    Returns:
        :class:`TdkInfo`。
    """
    if isinstance(value, TdkInfo):
        return value
    info = TdkInfo(source_url=source_url)
    if value is None:
        info.error_type = ErrorType.TIMEOUT.value
        info.parse_warning = "TDK 抓取超时"
        return info
    if isinstance(value, dict):  # pragma: no cover - 兼容外部 mock
        try:
            return TdkInfo(**value)
        except Exception:  # noqa: BLE001
            return info
    info.error_type = ErrorType.CONN_FAILED.value
    info.parse_warning = "TDK 抓取失败"
    return info


def _coerce_status(value: Any) -> Any:
    """把阶段产出安全地转为 SiteStatusInfo。

    Args:
        value: 阶段结果。

    Returns:
        :class:`SiteStatusInfo`。
    """
    from site_group_monitor.models.entities import SiteStatusInfo

    if isinstance(value, SiteStatusInfo):
        return value
    info = SiteStatusInfo()
    if value is None:
        info.error_type = ErrorType.TIMEOUT.value
        info.error_msg = "状态检测超时"
        return info
    if isinstance(value, dict):  # pragma: no cover - 兼容外部 mock
        try:
            return SiteStatusInfo(**value)
        except Exception:  # noqa: BLE001
            return info
    info.error_type = ErrorType.CONN_FAILED.value
    info.error_msg = "状态检测失败"
    return info


def describe_network_path(settings: Any, proxy: Any) -> str:
    """说明本站查询实际走哪条出口，给进度条和详情抽屉看。"""
    cfg = getattr(getattr(getattr(settings, "engine", None), "baidu", None), "browser", None)
    browser_on = bool(cfg is not None and getattr(cfg, "enabled", False))
    via_proxy = bool(cfg is not None and getattr(cfg, "via_proxy", True))
    brief = _proxy_brief(proxy)
    if browser_on and not via_proxy:
        if brief:
            return f"收录/TDK：本机直连（未走代理池）；打开网站：代理 {brief}"
        return "收录/TDK：本机直连（未走代理池）；打开网站：直连"
    if brief:
        return f"收录/TDK/网站状态：代理 {brief}"
    return "代理池暂无可用 IP，本次直连降级"


def _proxy_brief(proxy: Any) -> str:
    if proxy is None:
        return ""
    ip = str(getattr(proxy, "last_egress_ip", "") or getattr(proxy, "host", "") or "").strip()
    loc = str(getattr(proxy, "last_location", "") or "").strip()
    latency = getattr(proxy, "last_latency_ms", None)
    parts = [item for item in (ip, loc) if item]
    if latency is not None and str(latency) != "":
        parts.append(f"{latency}ms")
    return " ".join(parts)
