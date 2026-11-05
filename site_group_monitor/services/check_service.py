"""全量/增量检测编排服务（CheckService）。

职责：
    * 按 ``concurrency.global_workers`` 信号量控制并发；
    * 按 ``batch_size`` 分批次，批次之间由 ``RateLimiter.batch_pause()`` 错峰（**自身不 sleep**）；
    * 无可用代理时自动降级（并发降至 ``no_proxy_workers``，间隔放大 ``no_proxy_interval_factor``）；
    * 结果即时落库（``CheckResultRepository``）+ 站点 ``last_checked_at``；
    * 提供进度快照、取消令牌（Ctrl+C → ``runs.status=cancelled``）与 Run 汇总。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from site_group_monitor.core import baidu_checker as baidu_module
from site_group_monitor.core import check_worker as worker_module
from site_group_monitor.core import http_client as http_module
from site_group_monitor.core import local_browser as browser_module
from site_group_monitor.core import proxy_pool as proxy_module
from site_group_monitor.core import rate_limiter as limiter_module
from site_group_monitor.core import status_checker as status_module
from site_group_monitor.core import tdk_parser as tdk_module
from site_group_monitor.models.entities import RunSummary, Site
from site_group_monitor.models.enums import IndexStatus, RunMode, RunStatus
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.utils.timeutil import epoch_ms, utcnow_iso


@dataclass
class CancelToken:
    """协作式取消令牌（CLI Ctrl+C 与 Web 停止按钮共用）。"""

    cancelled: bool = False
    reason: str = ""

    def cancel(self, reason: str = "用户中断") -> None:
        """请求取消。

        Args:
            reason: 取消原因。
        """
        self.cancelled = True
        self.reason = reason


@dataclass
class _ProgressState:
    """内部进度计数器。"""

    total: int = 0
    done: int = 0
    indexed: int = 0
    not_indexed: int = 0
    unknown: int = 0
    failed: int = 0
    running: int = 0
    concurrency: int = 0
    started_ms: int = field(default_factory=epoch_ms)
    message: str = ""
    current: list[str] = field(default_factory=list)


class CheckService:
    """批次编排服务。"""

    def __init__(
        self,
        repos: Repositories,
        settings: Any,
        logger: Any | None = None,
        audit: Any | None = None,
    ) -> None:
        """构造编排服务。

        Args:
            repos: 仓储容器。
            settings: 配置对象。
            logger: 日志记录器。
            audit: 审计服务（用于写 run 相关留痕）。
        """
        self.repos = repos
        self.settings = settings
        self.logger = logger
        self.audit = audit
        self._state = _ProgressState()
        self._task_id = ""
        self._run_id: int | None = None
        self._running: bool = False
        self._current_tasks: set[asyncio.Task] = set()
        self._external_cancel: CancelToken | None = None
        self._active_worker: Any = None

    # ================================================================ 内核装配
    def build_kernel(self, *, max_browser_slots: int | None = None) -> tuple[Any, Any, Any, Any, Any]:
        """按需装配采集内核（HTTP / UA / 限速 / 代理池 / Worker）。

        Returns:
            ``(http, proxy_pool, limiter, worker, concurrency)``。
        """
        http = http_module.HttpClient(self.settings, self.logger)
        limiter = limiter_module.RateLimiter.from_settings(self.settings, self.logger)
        proxies = proxy_module.ProxyPool(
            self.repos.proxies, self.settings, limiter, self.logger, repos=self.repos
        )
        browser = browser_module.BrowserPool(
            self.settings, self.logger, max_slots=max_browser_slots
        )
        index_checker = baidu_module.BaiduIndexChecker(http, proxies, limiter, self.settings, self.logger)
        index_checker.browser = browser
        tdk_parser = tdk_module.TdkParser(http, self.settings, self.logger, browser=browser)
        status_checker = status_module.StatusChecker(http, self.settings, self.logger)
        worker = worker_module.SiteCheckWorker(
            index_checker, tdk_parser, status_checker, self.settings, self.logger
        )
        worker.browser = browser
        return (worker, http, proxies, limiter, self._target_concurrency(proxies))

    def _target_concurrency(self, proxies: Any) -> int:
        """按代理池情况决定并发：空池自动降级。

        Args:
            proxies: 代理池。

        Returns:
            并发协程数。
        """
        base = int(self.settings.concurrency.global_workers or 6)
        no_proxy = int(self.settings.concurrency.no_proxy_workers or 3)
        browser_on = bool(getattr(getattr(self.settings.engine.baidu, "browser", None), "enabled", False))
        if browser_on:
            windows = browser_module.browser_window_count(self.settings)
            base = min(base, windows)
            no_proxy = min(no_proxy, windows)
        if proxies is None or proxies.is_empty():
            if self.logger is not None:
                self.logger.warning("代理池为空，降级为直连模式：并发 %d（限速间隔放大）", no_proxy)
            return no_proxy
        return base

    # ================================================================ 主入口
    async def run_all(
        self,
        mode: RunMode | str = RunMode.FULL,
        *,
        domains: Sequence[str] | Iterable[str] | None = None,
        concurrency: int | None = None,
        resume: bool = False,
        progress_cb: Callable[[dict[str, Any]], None] | None = None,
        cancel_token: CancelToken | None = None,
    ) -> RunSummary:
        """跑一轮检测。

        Args:
            mode: ``full`` / ``incremental`` / ``manual_single``。
            domains: 指定域名列表（``manual_single`` 场景）。
            concurrency: 覆盖全局并发。
            resume: 跳过当日已有快照的站点。
            progress_cb: 进度回调（每个站点完成回调一次）。
            cancel_token: 取消令牌。

        Returns:
            :class:`RunSummary`。
        """
        if self._running:
            return RunSummary(status=RunStatus.FAILED.value, message="已有任务在运行（互斥）")

        mode = RunMode(str(mode))
        self._task_id = uuid.uuid4().hex
        started_iso = utcnow_iso()
        sites = self._pick_sites(mode, domains)
        if resume:
            sites = self._skip_checked_today(sites)
        single_target = mode == RunMode.MANUAL_SINGLE and len(sites) == 1
        worker, http, proxies, limiter, default_concurrency = self.build_kernel(
            max_browser_slots=1 if single_target else None
        )
        self._active_worker = worker
        workers = 1 if single_target else max(1, int(concurrency or default_concurrency))
        cancel_token = cancel_token or self._external_cancel

        self._state = _ProgressState(total=len(sites), concurrency=workers)
        self._running = True

        self._run_id = self.repos.runs.start(
            self._task_id,
            str(mode),
            {
                "concurrency": workers,
                "proxies": proxies.size() if proxies else 0,
                "total": len(sites),
                "resume": bool(resume),
            },
        )
        try:
            browser = getattr(worker, "browser", None)
            if browser is not None and getattr(browser, "warm", None) is not None:
                with contextlib.suppress(Exception):
                    await browser.warm()
            batches = self._plan_batches(sites)
            semaphore = asyncio.Semaphore(workers)
            for batch_index, batch in enumerate(batches, start=1):
                if cancel_token is not None and cancel_token.cancelled:
                    return self._finish(mode, started_iso, RunStatus.CANCELLED.value,
                                        cancel_token.reason or "用户中断", http)
                self._state.message = f"第 {batch_index}/{len(batches)} 批 ({len(batch)} 站)"
                await self._run_batch(
                    batch, worker, semaphore, limiter, proxies, mode, progress_cb, cancel_token
                )
                if batch_index < len(batches):
                    factor = 1.0 if (proxies and not proxies.is_empty()) else float(
                        getattr(self.settings.concurrency, "no_proxy_interval_factor", 1.8)
                    )
                    await limiter.batch_pause(factor=factor)
            status, message = self._result_status()
            return self._finish(mode, started_iso, status, message, http)
        except asyncio.CancelledError:
            return self._finish(mode, started_iso, RunStatus.CANCELLED.value, "任务被取消", http)
        except Exception as exc:  # noqa: BLE001 - 编排层不因内部异常崩溃
            if self.logger is not None:
                self.logger.exception("check_service: 编排异常 %s", exc)
            return self._finish(mode, started_iso, RunStatus.FAILED.value, str(exc), http)
        finally:
            self._running = False
            if self.audit is not None:
                self.audit.log("system", "check.run", self._task_id,
                               {"mode": str(mode), "total": self._state.total,
                                "indexed": self._state.indexed, "unknown": self._state.unknown})
            with contextlib.suppress(Exception):
                await http.close()
            browser = getattr(worker, "browser", None)
            if browser is not None:
                with contextlib.suppress(Exception):
                    await browser.close()
            self._active_worker = None

    # ================================================================ 批次执行
    async def _run_batch(
        self,
        batch: Sequence[Site],
        worker: Any,
        semaphore: asyncio.Semaphore,
        limiter: Any,
        proxies: Any,
        mode: RunMode,
        progress_cb: Callable[[dict[str, Any]], None] | None,
        cancel_token: CancelToken | None,
    ) -> None:
        """并发跑一批站点。

        Args:
            batch: 站点列表。
            worker: Worker 实例。
            semaphore: 全局并发信号量。
            limiter: 限速器。
            proxies: 代理池。
            mode: 运行模式。
            progress_cb: 进度回调。
            cancel_token: 取消令牌。
        """
        tasks = [
            asyncio.create_task(
                self._run_one(site, worker, semaphore, mode, progress_cb),
                name=f"check-{site.domain}",
            )
            for site in batch
        ]
        self._current_tasks.update(tasks)
        try:
            for task in asyncio.as_completed(tasks):
                if cancel_token is not None and cancel_token.cancelled:
                    for pending in tasks:
                        if not pending.done():
                            pending.cancel()
                    break
                try:
                    await task
                except asyncio.CancelledError:
                    break
                except Exception as exc:  # noqa: BLE001 - 单站异常不影响批次
                    if self.logger is not None:
                        self.logger.warning("check_service: 单站任务异常 %s", exc)
                    self._state.failed += 1
                    self._state.done += 1
        finally:
            self._current_tasks.difference_update(tasks)

    async def _run_one(
        self,
        site: Site,
        worker: Any,
        semaphore: asyncio.Semaphore,
        mode: RunMode,
        progress_cb: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        """检测单个站点并落库。

        Args:
            site: 站点实体。
            worker: Worker。
            semaphore: 并发信号量。
            mode: 运行模式。
            progress_cb: 进度回调。
        """
        async with semaphore:
            self._state.running += 1
            if site.domain not in self._state.current:
                self._state.current.append(site.domain)
            self._state.message = f"正在查收录 / TDK / 能否打开：{site.domain}"
            try:
                result = await worker.run(site, self._task_id, mode)
            except Exception as exc:  # noqa: BLE001 - Worker 异常兜底
                from site_group_monitor.models.entities import CheckResult
                from site_group_monitor.models.enums import ErrorType, IndexReasonCode

                result = CheckResult(
                    site_id=int(site.id or 0),
                    task_id=self._task_id,
                    run_mode=str(mode),
                    checked_at=utcnow_iso(),
                    index_status=IndexStatus.UNKNOWN.value,
                    index_reason=IndexReasonCode.NETWORK_ERROR.value,
                    error_type=ErrorType.CONN_FAILED.value,
                    error_msg=f"Worker 异常: {exc}",
                )
            finally:
                self._state.running = max(0, self._state.running - 1)
                if site.domain in self._state.current:
                    self._state.current.remove(site.domain)

            self._persist(site, result)
            self._account(result)
            if progress_cb is not None:
                with contextlib.suppress(Exception):
                    progress_cb(
                        {
                            "domain": site.domain,
                            "index_status": result.index_status,
                            "success": result.error_type in ("none", ""),
                            "running_delta": 0,
                            "concurrency": self._state.concurrency,
                        }
                    )

    def _persist(self, site: Site, result: Any) -> None:
        """写入明细 + 更新站点最近检测时间（异常静默）。

        Args:
            site: 站点实体。
            result: 检测结果。
        """
        try:
            self.repos.results.insert(result)
            if site.id:
                self.repos.sites.touch_checked(int(site.id), result.checked_at)
        except Exception as exc:  # noqa: BLE001 - 写库失败不打断整批
            if self.logger is not None:
                self.logger.error("check_service: 写入检测结果失败 %s (%s)", site.domain, exc)

    def _account(self, result: Any) -> None:
        """累加统计。

        Args:
            result: 检测结果。
        """
        self._state.done += 1
        status = str(result.index_status)
        if status == IndexStatus.INDEXED.value:
            self._state.indexed += 1
        elif status == IndexStatus.NOT_INDEXED.value:
            self._state.not_indexed += 1
        else:
            self._state.unknown += 1
        if str(result.error_type) not in ("none", ""):
            self._state.failed += 1

    # ================================================================ 辅助
    def _pick_sites(self, mode: RunMode, domains: Sequence[str] | Iterable[str] | None) -> list[Site]:
        """按模式挑选待检测站点。

        Args:
            mode: 运行模式。
            domains: 指定域名。

        Returns:
            站点列表。
        """
        if domains:
            wanted = {str(item).strip().lower() for item in domains if str(item).strip()}
            all_sites = self.repos.sites.list_all()
            matched = [site for site in all_sites if site.domain in wanted]
            if mode == RunMode.MANUAL_SINGLE and len(matched) == 1:
                return matched
            if matched:
                return matched
        if mode == RunMode.INCREMENTAL:
            ratio = float(getattr(self.settings.scheduler, "incremental_sample_ratio", 0.0) or 0.0)
            return self.repos.sites.sample_incremental(ratio)
        return self.repos.sites.list_enabled()

    def _skip_checked_today(self, sites: Sequence[Site]) -> list[Site]:
        """``--resume``：跳过当天已有快照的站点。

        Args:
            sites: 候选站点。

        Returns:
            过滤后的站点列表。
        """
        from site_group_monitor.utils.timeutil import today_str

        stat_date = today_str(self.settings.timezone_name())
        done = {int(item.site_id) for item in self.repos.snapshots.by_date(stat_date)}
        remaining = [site for site in sites if int(site.id or 0) not in done]
        skipped = len(sites) - len(remaining)
        if skipped and self.logger is not None:
            self.logger.info("check_service: --resume 跳过 %d 个今日已完成站点", skipped)
        return remaining

    def _plan_batches(self, sites: Sequence[Site]) -> list[list[Site]]:
        """按 ``batch_size`` 切分批。

        Args:
            sites: 站点列表。

        Returns:
            批次列表。
        """
        size = max(1, int(self.settings.concurrency.batch_size or 50))
        return [list(sites[index:index + size]) for index in range(0, len(sites), size)]

    def _result_status(self) -> tuple[str, str]:
        """根据实际结果给批次定级，避免大量查询失败仍显示成功。"""
        total = max(0, self._state.total)
        known = self._state.indexed + self._state.not_indexed
        unknown = self._state.unknown
        failed = self._state.failed
        if total == 0:
            return RunStatus.SUCCESS.value, "没有需要检测的站点"
        if self._state.done == 0 or known == 0 or unknown / total >= 0.8:
            return (
                RunStatus.FAILED.value,
                f"查询失败：明确结果 {known}/{total}，未知 {unknown}，异常 {failed}",
            )
        if unknown or failed:
            return (
                RunStatus.PARTIAL.value,
                f"部分完成：明确结果 {known}/{total}，未知 {unknown}，异常 {failed}",
            )
        return RunStatus.SUCCESS.value, f"检测完成：{known}/{total} 个站点获得明确结果"

    def _finish(self, mode: RunMode, started_iso: str, status: str, message: str, http: Any) -> RunSummary:
        """收尾：写 Run 统计并返回汇总。

        Args:
            mode: 运行模式。
            started_iso: 开始时间。
            status: 结束状态。
            message: 说明。
            http: HTTP 客户端（用于关闭，交由调用方 finally 处理）。

        Returns:
            :class:`RunSummary`。
        """
        duration = max(0, epoch_ms() - self._state.started_ms)
        finished_iso = utcnow_iso()
        if self._run_id is not None:
            with contextlib.suppress(Exception):
                self.repos.runs.finish(
                    self._run_id,
                    {
                        "status": status,
                        "finished_at": finished_iso,
                        "total": self._state.total,
                        "indexed": self._state.indexed,
                        "not_indexed": self._state.not_indexed,
                        "unknown": self._state.unknown,
                        "failed": self._state.failed,
                        "concurrency": self._state.concurrency,
                        "duration_ms": duration,
                    },
                )
        self._running = False
        self._state.message = message
        return RunSummary(
            task_id=self._task_id,
            run_id=self._run_id,
            mode=str(mode),
            status=status,
            total=self._state.total,
            indexed=self._state.indexed,
            not_indexed=self._state.not_indexed,
            unknown=self._state.unknown,
            failed=self._state.failed,
            concurrency=self._state.concurrency,
            duration_ms=duration,
            started_at=started_iso,
            finished_at=finished_iso,
            message=message,
        )

    # ================================================================ 可观测
    def progress_snapshot(self) -> dict[str, Any]:
        """返回当前进度快照（CLI rich 与 ``/api/system/status`` 共用）。

        Returns:
            进度字典。
        """
        elapsed = max(1, epoch_ms() - self._state.started_ms)
        done = self._state.done
        remaining = max(0, self._state.total - done)
        eta_ms = int(elapsed * remaining / done) if done else 0
        return {
            "phase": "running" if self._running else "idle",
            "task_id": self._task_id,
            "total": self._state.total,
            "done": done,
            "ok": self._state.indexed + self._state.not_indexed,
            "indexed": self._state.indexed,
            "not_indexed": self._state.not_indexed,
            "unknown": self._state.unknown,
            "failed": self._state.failed,
            "running": self._state.running,
            "concurrency": self._state.concurrency,
            "elapsed_ms": elapsed,
            "eta_ms": eta_ms,
            "qps": round(done / (elapsed / 1000), 3),
            "message": self._state.message,
            "current": list(self._state.current),
            "browser_url": getattr(getattr(self._active_worker, "browser", None), "last_url", "") or "",
            "network": getattr(self._active_worker, "last_network", "") or "",
        }

    def cancel(self) -> CancelToken:
        """创建并使用一个取消令牌（内部保存，供 Web 停止接口调用）。

        Returns:
            新的 :class:`CancelToken`。
        """
        token = CancelToken(reason="用户通过 API 取消")
        self._external_cancel = token
        return token

    @property
    def is_running(self) -> bool:
        """是否正在跑。"""
        return self._running

    @property
    def task_id(self) -> str:
        """当前任务 UUID。"""
        return self._task_id
