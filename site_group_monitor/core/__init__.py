"""采集内核：HTTP 客户端、指纹池、限速器、代理池、百度/TDK/状态检测与单站编排。

设计红线（对应架构文档 §7.9）：
    * **只有** ``core/rate_limiter.py`` 与 ``core/proxy_pool.py`` 能控制速率与代理选择；
    * ``core/*`` 任何子项异常都必须在内部降级，**不得**冒泡打断整批；
    * 解析逻辑（baidu_parser / tdk_parser 的 parse）为纯函数，**单测不联网**。
"""

from __future__ import annotations

__all__: list[str] = []
