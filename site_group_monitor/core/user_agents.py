"""UA / Referer / Accept-Language 指纹池。

桌面浏览器混合（Chrome / Edge / Firefox / Safari），每次请求轮换 UA，
并可随机搭配 Referer 与 Accept-Language，降低指纹稳定性。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Sequence

#: 桌面 UA 池（20 组，覆盖 Chrome/Edge/Firefox/Safari 主流版本）
USER_AGENTS: tuple[str, ...] = (
    # Chrome / Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 6.1; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    # Chrome / macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome / Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.2365.92",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.2277.128",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.2277.83",
    # Firefox
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    # Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
    # 360 / QQ 浏览器（国内站群常见）
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.5359.125 Safari/537.36 QIHU 360SE",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36 QQBrowser/16.2.5016.400",
)

ACCEPT_LANGUAGES: tuple[str, ...] = (
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9",
    "zh-CN,zh;q=0.8,zh-TW;q=0.7,en;q=0.6",
    "zh,zh-CN;q=0.9,en-US;q=0.8,en;q=0.7",
)

REFERERS: tuple[str, ...] = (
    "https://www.baidu.com/",
    "https://www.baidu.com/index.htm",
    "https://cn.bing.com/",
    "https://www.google.com/",
)

ACCEPT_HEADER = "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"


@dataclass
class UserAgentPool:
    """UA / 指纹池。

    Attributes:
        pool_size: 使用的 UA 数量（<= 池长度）。
        rotate: 是否每次请求轮换（False 时随机取一次后固定）。
        seed: 随机种子（测试可复现）。
    """

    pool_size: int = 20
    rotate: bool = True
    seed: int | None = None
    user_agents: Sequence[str] = field(default=USER_AGENTS)
    _rng: random.Random = field(default_factory=random.Random, init=False, repr=False)

    def __post_init__(self) -> None:
        """初始化随机数发生器与 UA 数量收敛。"""
        self._rng = random.Random(self.seed)
        max_size = len(self.user_agents)
        self.pool_size = max(1, min(int(self.pool_size or 1), max_size))

    def next_ua(self) -> str:
        """返回一个 UA 字符串。

        Returns:
            UA 字符串。
        """
        return self._rng.choice(list(self.user_agents[: self.pool_size]))

    def next_headers(self, referer: str | None = None, *, include_ua: bool = True) -> dict[str, str]:
        """生成一组完整的浏览器风格请求头。

        Args:
            referer: 指定 Referer；为空则随机选取。
            include_ua: 是否包含 User-Agent。

        Returns:
            请求头字典。
        """
        headers: dict[str, str] = {
            "Accept": ACCEPT_HEADER,
            "Accept-Language": self._rng.choice(ACCEPT_LANGUAGES),
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }
        if include_ua:
            headers["User-Agent"] = self.next_ua()
        headers["Referer"] = referer or self._rng.choice(REFERERS)
        return headers

    def baidu_headers(self) -> dict[str, str]:
        """百度检索专用的请求头（Referer 固定为百度首页）。"""
        headers = self.next_headers(referer="https://www.baidu.com/")
        headers["Sec-Fetch-Site"] = "same-site"
        return headers


def random_ua() -> str:
    """便捷函数：取一个随机 UA。

    Returns:
        UA 字符串。
    """
    return random.choice(USER_AGENTS)


def __getattr__(name: str) -> Any:  # pragma: no cover - 兼容旧命名
    """兼容 ``USER_AGENT_POOL`` 等旧引用。"""
    if name == "UA_POOL":
        return USER_AGENTS
    raise AttributeError(name)
