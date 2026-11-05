"""代理池：健康度模型、选择策略、并发闸门、失败/验证码反馈、自动剔除与复活。

要点：
    * 每代理并发上限 ``per_proxy_workers``（默认 1）——用 asyncio.Semaphore 做闸门；
    * 选择策略支持 ``weighted_random``（默认）与 ``round_robin``；
    * ``report_fail`` 连续失败 ``fail_threshold`` 次或健康度 < ``auto_disable_health`` → ``disabled=1``；
    * ``report_captcha`` 扣更多健康度（-0.3），并让该代理短时间内降速（走 rate_limiter.penalty）；
    * 空池/全禁用时 :meth:`acquire` 返回 ``None`` → 上层降级直连。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Sequence

from site_group_monitor.models.entities import ProxyEndpoint


@dataclass
class ProxySlot:
    """单个代理的运行期状态（不落库）。"""

    semaphore: asyncio.Semaphore
    success: int = 0
    fail: int = 0
    captcha: int = 0


class ProxyPool:
    """代理池管理对象。

    Args:
        proxy_repo: 代理仓储。
        settings: 配置对象。
        limiter: 限速器（用于 captcha 后的惩罚窗口）。
        logger: 日志记录器。
    """

    def __init__(
        self,
        proxy_repo: Any,
        settings: Any,
        limiter: Any | None = None,
        logger: Any | None = None,
        repos: Any | None = None,
    ) -> None:
        self.repo = proxy_repo
        self.settings = settings
        self.limiter = limiter
        self.logger = logger
        self.repos = repos
        self._proxies: list[ProxyEndpoint] = []
        self._slots: dict[str, ProxySlot] = {}
        self._rr_index = 0
        self._lock = asyncio.Lock()
        self._rotate_lock = asyncio.Lock()
        self._last_reload = 0.0
        self.reload_from_db()

    # ---------------------------------------------------------------- 加载
    def reload_from_db(self) -> None:
        """从数据库重新加载代理列表（保留运行期计数）。"""
        try:
            rows = self.repo.list_all(include_disabled=True)
        except Exception:  # noqa: BLE001 - 仓储异常不应影响编排
            rows = []
        self._proxies = list(rows)
        for proxy in self._proxies:
            self._slot(proxy)
        self._last_reload = time.monotonic()

    def _slot(self, proxy: ProxyEndpoint) -> ProxySlot:
        """取得（或创建）代理的运行期槽位。"""
        key = proxy.key
        slot = self._slots.get(key)
        if slot is None:
            workers = 1
            if self.settings is not None:
                workers = max(1, int(getattr(self.settings.concurrency, "per_proxy_workers", 1)))
            slot = ProxySlot(semaphore=asyncio.Semaphore(workers))
            self._slots[key] = slot
        return slot

    # ---------------------------------------------------------------- 选择
    @property
    def proxies(self) -> list[ProxyEndpoint]:
        """当前代理列表（含禁用）。"""
        return list(self._proxies)

    def size(self) -> int:
        """代理总数。"""
        return len(self._proxies)

    def available_count(self) -> int:
        """当前可用（未禁用且健康度过阈值）代理数。"""
        return len(self._candidates())

    def is_empty(self) -> bool:
        """代理池是否为空（或全部不可用）—— 决定是否需要降级直连。"""
        return self.available_count() == 0

    def _healthy_low_bound(self) -> float:
        """健康度可用下限。"""
        if self.settings is None:
            return 0.3
        return float(getattr(self.settings.proxy, "auto_disable_health", 0.3))

    def _candidates(self) -> list[ProxyEndpoint]:
        """过滤出未禁用且健康度达标的候选代理。"""
        low = self._healthy_low_bound()
        return [
            proxy
            for proxy in self._proxies
            if (not proxy.disabled) and proxy.health_score >= low
        ]

    def _pick(self, candidates: Sequence[ProxyEndpoint]) -> ProxyEndpoint:
        """按策略挑选一个代理。

        Args:
            candidates: 候选列表。

        Returns:
            选中的代理实体。
        """
        strategy = "weighted_random"
        if self.settings is not None:
            strategy = str(getattr(self.settings.proxy, "select_strategy", "weighted_random"))
        if strategy == "round_robin":
            index = self._rr_index % max(1, len(candidates))
            self._rr_index = (self._rr_index + 1) % max(1, len(candidates))
            return candidates[index]
        weights = [max(0.01, float(proxy.weight) * max(0.05, proxy.health_score)) for proxy in candidates]
        total = sum(weights)
        threshold = random.uniform(0, total) if total > 0 else 0
        accumulate = 0.0
        for proxy, weight in zip(candidates, weights):
            accumulate += weight
            if accumulate >= threshold:
                return proxy
        return candidates[-1]

    async def acquire(
        self, timeout_sec: float | None = None, exclude_ids: set[int] | None = None
    ) -> ProxyEndpoint | None:
        """占用一个可用代理槽位（超时返回 None → 上层降级直连）。

        Args:
            timeout_sec: 等待槽位的总超时。

        Returns:
            代理实体；无可用代理时为 None。
        """
        if self.settings is not None and not getattr(self.settings.proxy, "enabled", True):
            return None
        configured_timeout = float(
            getattr(getattr(self.settings, "proxy", None), "acquire_timeout_sec", 5.0) or 5.0
        )
        wait_budget = max(0.5, float(timeout_sec if timeout_sec is not None else configured_timeout))
        deadline = time.monotonic() + wait_budget
        try:
            # 给现有代理槽位保留一部分预算，动态提取接口慢时不再拖住整站检测。
            rotate_budget = min(3.0, max(0.5, wait_budget * 0.6))
            # shield 保留后台提取任务及其锁，避免超时取消后重复并发调用供应商 API。
            rotate_task = asyncio.create_task(self._acquire_rotated())
            rotated = await asyncio.wait_for(asyncio.shield(rotate_task), timeout=rotate_budget)
        except asyncio.TimeoutError:
            rotated = None
            if self.logger is not None:
                self.logger.warning("proxy_pool: 换新 IP 超时，改用现有代理或直连")
        if rotated is not None and (
            not exclude_ids or rotated.id not in exclude_ids
        ):
            return rotated
        blocked = exclude_ids or set()
        while time.monotonic() < deadline:
            candidates = self._candidates()
            if not candidates:
                return None
            free = [
                proxy
                for proxy in candidates
                if _semaphore_available(self._slot(proxy).semaphore)
                and (proxy.id is None or int(proxy.id) not in blocked)
            ]
            if not free:
                # 所有代理都在忙：短暂轮询等待（等待槽位，不属于限速逻辑）
                await asyncio.sleep(0.1)
                continue
            proxy = self._pick(free)
            slot = self._slot(proxy)
            try:
                await asyncio.wait_for(slot.semaphore.acquire(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except Exception:  # noqa: BLE001 - 信号量异常不应冒泡
                return None
            if self.logger is not None:
                self.logger.debug("proxy_pool: 占用代理 %s (health=%.2f)", proxy.masked_url, proxy.health_score)
            return proxy
        return None

    async def _acquire_rotated(self, rotate_per_check: bool = True) -> ProxyEndpoint | None:
        """从指定轮换策略的 API 源提取一条新代理并占用。"""
        repos = self.repos
        if repos is None or not hasattr(repos, "proxies_api"):
            return None
        try:
            sources = [
                item
                for item in repos.proxies_api.list_all(include_disabled=False)
                if bool(getattr(item, "rotate_per_check", True)) is bool(rotate_per_check)
            ]
        except Exception:  # noqa: BLE001
            return None
        if not sources:
            return None
        source = sources[self._rr_index % len(sources)]
        self._rr_index = (self._rr_index + 1) % max(1, len(sources))
        async with self._rotate_lock:
            try:
                from site_group_monitor.core.proxy_api_fetcher import fetch_and_store

                result = await asyncio.to_thread(
                    fetch_and_store,
                    source,
                    repos,
                    self.settings,
                    prune="delete" if rotate_per_check else "disable",
                )
            except Exception as exc:  # noqa: BLE001
                if self.logger is not None:
                    self.logger.warning("proxy_pool: 提取新 IP 失败 %s", exc)
                return None
            if not result.get("ok"):
                if self.logger is not None:
                    self.logger.warning("proxy_pool: 提取新 IP 失败 %s", result.get("error"))
                return None
            self.reload_from_db()
            fresh_ids = set(result.get("fresh_ids") or [])
            picked = None
            for proxy in self._candidates():
                if proxy.id in fresh_ids or (
                    proxy.source == "api" and str(getattr(proxy, "source_ref", "")) == str(source.id)
                ):
                    picked = proxy
                    break
            if picked is None:
                return None
            slot = self._slot(picked)
            try:
                await asyncio.wait_for(slot.semaphore.acquire(), timeout=2.0)
            except Exception:  # noqa: BLE001
                return None
            if self.logger is not None:
                self.logger.info("proxy_pool: 本轮换新 IP %s", picked.masked_url)
            return picked

    async def rotate_after_captcha(
        self,
        current: ProxyEndpoint | None,
        timeout_sec: float | None = None,
    ) -> ProxyEndpoint | None:
        """验证码/风控后立即换 IP；优先使用“风控时切换”策略的 API 源。"""
        configured = float(
            getattr(getattr(self.settings, "proxy", None), "acquire_timeout_sec", 5.0) or 5.0
        )
        budget = max(0.5, float(timeout_sec if timeout_sec is not None else configured))
        for mode in (False, True):
            try:
                task = asyncio.create_task(self._acquire_rotated(rotate_per_check=mode))
                replacement = await asyncio.wait_for(asyncio.shield(task), timeout=budget)
            except asyncio.TimeoutError:
                replacement = None
            if replacement is None:
                continue
            if current is not None and replacement.key == current.key:
                self.release(replacement)
                continue
            if self.logger is not None:
                self.logger.info("proxy_pool: 百度风控后已切换代理 %s", replacement.masked_url)
            return replacement
        return None

    def release(self, proxy: ProxyEndpoint | None) -> None:
        """释放代理槽位。

        Args:
            proxy: 之前占用的代理；None 表示直连无需处理。
        """
        if proxy is None:
            return
        slot = self._slots.get(proxy.key)
        if slot is None:
            return
        try:
            slot.semaphore.release()
        except ValueError:  # pragma: no cover - 重复释放保护
            pass

    # ---------------------------------------------------------------- 反馈
    def report_ok(self, proxy: ProxyEndpoint | None) -> None:
        """记录一次成功。

        Args:
            proxy: 代理实体。
        """
        if proxy is None:
            return
        proxy.mark_ok()
        slot = self._slot(proxy)
        slot.success += 1
        self._persist(proxy)

    def report_fail(self, proxy: ProxyEndpoint | None, kind: str = "fail") -> None:
        """记录一次失败，并按阈值自动禁用。

        Args:
            proxy: 代理实体。
            kind: ``fail`` / ``captcha`` / ``proxy``。
        """
        if proxy is None:
            return
        proxy.mark_fail(kind)
        slot = self._slot(proxy)
        slot.fail += 1
        threshold = 3
        low = self._healthy_low_bound()
        if self.settings is not None:
            threshold = max(1, int(getattr(self.settings.proxy, "fail_threshold", 3)))
        if proxy.consecutive_fail >= threshold or proxy.health_score < low:
            proxy.disabled = True
            if self.logger is not None:
                self.logger.warning(
                    "proxy_pool: 代理被自动禁用 %s (连续失败=%d, health=%.2f)",
                    proxy.masked_url, proxy.consecutive_fail, proxy.health_score,
                )
        self._persist(proxy)

    def report_captcha(self, proxy: ProxyEndpoint | None) -> None:
        """记录一次验证码命中（扣 0.3 健康度 + 惩罚该代理的发送节奏）。

        Args:
            proxy: 代理实体。
        """
        if proxy is None:
            return
        slot = self._slot(proxy)
        slot.captcha += 1
        if self.limiter is not None:
            penalty_sec = 30.0
            if self.settings is not None:
                penalty_sec = float(getattr(self.settings.engine.baidu, "min_interval_sec", 3.0)) * 10
            self.limiter.penalty(proxy.key, penalty_sec)
        self.report_fail(proxy, kind="captcha")

    def revive_stale(self) -> int:
        """复活被禁用超过配置时长的代理。

        Returns:
            复活条数。
        """
        minutes = 30
        if self.settings is not None:
            minutes = int(getattr(self.settings.proxy, "revive_after_minutes", 30))
        cutoff_iso = _iso_minutes_ago(minutes)
        try:
            revived = self.repo.revive_disabled(cutoff_iso)
        except Exception:  # noqa: BLE001
            return 0
        if revived:
            self.reload_from_db()
            for proxy in self._proxies:
                if self.limiter is not None:
                    self.limiter.reset_key(proxy.key)
        return int(revived)

    def stats(self) -> dict[str, Any]:
        """代理池统计（总览卡片「代理可用率」）。

        Returns:
            统计字典（含每个代理的脱敏信息与运行期计数）。
        """
        items: list[dict[str, Any]] = []
        counts = {"manual": 0, "tunnel": 0, "batch": 0, "api": 0}
        for proxy in self._proxies:
            slot = self._slot(proxy)
            source = proxy.source
            source_ref = getattr(proxy, "source_ref", "")
            if source in counts:
                counts[source] += 1
            items.append(
                {
                    "id": proxy.id,
                    "name": proxy.name,
                    "url": proxy.masked_url,
                    "protocol": proxy.protocol,
                    "host": proxy.host,
                    "port": proxy.port,
                    "disabled": proxy.disabled,
                    "source": source,
                    "source_ref": source_ref,
                    "health_score": proxy.health_score,
                    "success_count": proxy.success_count,
                    "fail_count": proxy.fail_count,
                    "consecutive_fail": proxy.consecutive_fail,
                    "runtime_success": slot.success,
                    "runtime_fail": slot.fail,
                    "runtime_captcha": slot.captcha,
                    "last_ok_at": proxy.last_ok_at,
                    "last_fail_at": proxy.last_fail_at,
                    "last_egress_ip": getattr(proxy, "last_egress_ip", "") or "",
                    "last_location": getattr(proxy, "last_location", "") or "",
                    "last_latency_ms": getattr(proxy, "last_latency_ms", None),
                    "last_probe_at": getattr(proxy, "last_probe_at", None),
                    "last_probe_error": getattr(proxy, "last_probe_error", "") or "",
                }
            )
        enabled = [proxy for proxy in self._proxies if not proxy.disabled]
        healthy = [proxy for proxy in enabled if proxy.health_score >= 0.7]
        return {
            "total": len(self._proxies),
            "enabled": len(enabled),
            "disabled": len(self._proxies) - len(enabled),
            "healthy": len(healthy),
            "health_rate": round(len(healthy) / len(enabled), 4) if enabled else 0.0,
            "empty": len(enabled) == 0,
            "manual_count": counts["manual"],
            "tunnel_count": counts["tunnel"],
            "batch_count": counts["batch"],
            "api_count": counts["api"],
            "items": items,
        }

    # ---------------------------------------------------------------- 持久化
    def _persist(self, proxy: ProxyEndpoint) -> None:
        """把健康度与统计回写数据库（异常静默）。"""
        try:
            self.repo.save_state(proxy)
        except Exception:  # noqa: BLE001
            pass


def _semaphore_available(semaphore: asyncio.Semaphore) -> bool:
    """判断信号量是否还有余量（带兼容兜底，避免依赖私有属性）。

    Args:
        semaphore: 目标信号量。

    Returns:
        有余量返回 True。
    """
    value = getattr(semaphore, "_value", None)
    if isinstance(value, int):
        return value > 0
    return not semaphore.locked()


def _iso_minutes_ago(minutes: int) -> str:
    """生成 N 分钟前的 UTC ISO 字符串（代理复活时间上界）。

    Args:
        minutes: 分钟数。

    Returns:
        ISO8601 字符串。
    """
    from site_group_monitor.utils.timeutil import utcnow, to_iso
    from datetime import timedelta

    return to_iso(utcnow() - timedelta(minutes=max(0, int(minutes))))
