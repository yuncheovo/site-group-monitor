"""速率限制器：全局/单代理双令牌桶 + 阈值休整 + 冷却 + 批次错峰。

这是**唯一允许 sleep 的地方**（配合 ``proxy_pool`` 的并发闸门），
其余模块（包括 ``check_service`` 的批次错峰）必须调用本类的方法，**禁止**自行 ``asyncio.sleep``。

模型：
    * ``acquire(key)``：同一 key（通常为代理 key）两次请求之间至少间隔 ``min_interval_sec × jitter``；
    * 每累计 ``max_requests_per_proxy`` 次后强制休整 ``rest_after_threshold_sec ± 20%``；
    * ``penalty(key, sec)``：对某个 key 追加惩罚（比如刚出验证码）；
    * ``cooldown(sec)``：全局冷却（连续被限速时承担整体降速）；
    * ``batch_pause()``：批次之间的错峰休眠（20–40s jitter）。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Bucket:
    """单个 key 的令牌桶状态。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_ts: float = 0.0
    count: int = 0
    penalty_until: float = 0.0


class RateLimiter:
    """令牌桶限速器。

    Args:
        min_interval_sec: 同一 key 两次请求的最小间隔。
        jitter: 间隔抖动倍率区间 ``[min, max]``。
        max_requests_per_proxy: 单个 key 累计请求阈值。
        rest_after_threshold_sec: 达到阈值后的休整秒数。
        batch_pause_range: 批次错峰休眠区间。
        sleep_func: 可替换的 sleep 协程（测试用）。
        logger: 日志记录器。
    """

    def __init__(
        self,
        *,
        min_interval_sec: float = 3.0,
        jitter: tuple[float, float] = (0.6, 1.4),
        max_requests_per_proxy: int = 25,
        rest_after_threshold_sec: float = 45.0,
        batch_pause_range: tuple[float, float] = (20.0, 40.0),
        sleep_func: Any | None = None,
        logger: Any | None = None,
    ) -> None:
        self.min_interval_sec = max(0.0, float(min_interval_sec))
        self.jitter = (float(jitter[0]), float(jitter[1]))
        self.max_requests_per_proxy = max(1, int(max_requests_per_proxy))
        self.rest_after_threshold_sec = max(0.0, float(rest_after_threshold_sec))
        self.batch_pause_range = (float(batch_pause_range[0]), float(batch_pause_range[1]))
        self._sleep_func = sleep_func or asyncio.sleep
        self.logger = logger
        self._buckets: dict[str, _Bucket] = {}
        self._global_lock = asyncio.Lock()
        self._global_until: float = 0.0
        self._total_requests: int = 0
        self._total_waited_sec: float = 0.0

    @classmethod
    def from_settings(cls, settings: Any, logger: Any | None = None, sleep_func: Any | None = None) -> "RateLimiter":
        """从配置对象构造限速器（百度引擎参数 + 批次错峰参数）。

        Args:
            settings: 配置对象。
            logger: 日志记录器。
            sleep_func: 可选 sleep 覆盖。

        Returns:
            :class:`RateLimiter` 实例。
        """
        baidu = settings.engine.baidu
        jitter = tuple(baidu.jitter) if getattr(baidu, "jitter", None) else (0.6, 1.4)
        batch = tuple(settings.concurrency.batch_pause_sec) or (20.0, 40.0)
        return cls(
            min_interval_sec=float(baidu.min_interval_sec),
            jitter=(float(jitter[0]), float(jitter[1])),
            max_requests_per_proxy=int(baidu.max_requests_per_proxy),
            rest_after_threshold_sec=float(baidu.rest_after_threshold_sec),
            batch_pause_range=(float(batch[0]), float(batch[1])),
            logger=logger,
            sleep_func=sleep_func,
        )

    # ---------------------------------------------------------------- 核心
    def _bucket(self, key: str) -> _Bucket:
        """取得（或创建）某个 key 的令牌桶。"""
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket()
            self._buckets[key] = bucket
        return bucket

    def interval_with_jitter(self) -> float:
        """计算本次应等待的间隔（含抖动）。

        Returns:
            秒数。
        """
        factor = random.uniform(self.jitter[0], self.jitter[1])
        return max(0.0, self.min_interval_sec * factor)

    async def acquire(self, key: str) -> float:
        """获取一次发送许可（必要时等待）。

        Args:
            key: 代理 key（``direct`` 表示直连）。

        Returns:
            本次实际等待的秒数。
        """
        bucket = self._bucket(key)
        waited_total = 0.0
        async with bucket.lock:
            self._total_requests += 1
            # ① 全局冷却
            await self._wait_global()
            # ② 惩罚窗口
            now = time.monotonic()
            if bucket.penalty_until > now:
                wait_sec = bucket.penalty_until - now
                await self._sleep(wait_sec)
                waited_total += wait_sec
            # ③ 最小间隔 + jitter
            now = time.monotonic()
            elapsed = now - bucket.last_ts
            need = self.interval_with_jitter()
            if bucket.last_ts > 0 and elapsed < need:
                wait_sec = need - elapsed
                await self._sleep(wait_sec)
                waited_total += wait_sec
            bucket.last_ts = time.monotonic()
            bucket.count += 1
            # ④ 累计阈值休整
            if bucket.count >= self.max_requests_per_proxy:
                rest = self.rest_after_threshold_sec * random.uniform(0.8, 1.2)
                await self._sleep(rest)
                waited_total += rest
                bucket.count = 0
                if self.logger is not None:
                    self.logger.debug("rate_limiter: %s 达到 %s 次请求，休整 %.1fs", key,
                                      self.max_requests_per_proxy, rest)
        self._total_waited_sec += waited_total
        return waited_total

    async def _wait_global(self) -> None:
        """等待全局冷却结束。"""
        if self._global_until <= 0:
            return
        async with self._global_lock:
            remaining = self._global_until - time.monotonic()
            if remaining > 0:
                await self._sleep(remaining)
                if self.logger is not None:
                    self.logger.info("rate_limiter: 全局降速 %.1fs", remaining)
            self._global_until = 0.0

    async def _sleep(self, seconds: float) -> None:
        """统一 sleep 出口（便于测试注入）。"""
        if seconds <= 0:
            return
        await self._sleep_func(seconds)

    # ---------------------------------------------------------------- 控制
    def penalty(self, key: str, seconds: float) -> None:
        """对某个代理追加惩罚窗口（如下一次请求前静默 N 秒）。

        Args:
            key: 代理 key。
            seconds: 惩罚秒数。
        """
        bucket = self._bucket(key)
        bucket.penalty_until = max(bucket.penalty_until, time.monotonic() + max(0.0, float(seconds)))

    async def cooldown(self, seconds: float = 60.0) -> None:
        """全局冷却（连续被限速 / 白页时由 Checker 调用）。

        Args:
            seconds: 冷却秒数。
        """
        async with self._global_lock:
            self._global_until = max(self._global_until, time.monotonic() + max(0.0, float(seconds)))
        if self.logger is not None:
            self.logger.warning("rate_limiter: 进入全局冷却 %.1fs", seconds)

    async def batch_pause(self, factor: float = 1.0) -> float:
        """批次之间的错峰休眠。

        Args:
            factor: 放大系数（无代理时放大 1.8）。

        Returns:
            实际休眠秒数。
        """
        low, high = self.batch_pause_range
        seconds = random.uniform(low, high) * max(1.0, factor)
        if self.logger is not None:
            self.logger.info("rate_limiter: 批次错峰休眠 %.1fs", seconds)
        await self._sleep(seconds)
        return seconds

    def reset_key(self, key: str) -> None:
        """重置某个 key 的计数与惩罚（换代理/复活时调用）。"""
        bucket = self._bucket(key)
        bucket.count = 0
        bucket.penalty_until = 0.0

    def stats(self) -> dict[str, Any]:
        """返回限速器统计（可观测性 R-020）。

        Returns:
            统计字典。
        """
        return {
            "min_interval_sec": self.min_interval_sec,
            "jitter": list(self.jitter),
            "max_requests_per_proxy": self.max_requests_per_proxy,
            "rest_after_threshold_sec": self.rest_after_threshold_sec,
            "batch_pause_range": list(self.batch_pause_range),
            "total_requests": self._total_requests,
            "total_waited_sec": round(self._total_waited_sec, 2),
            "global_cooling": time.monotonic() < self._global_until,
            "keys": sorted(self._buckets.keys()),
        }
