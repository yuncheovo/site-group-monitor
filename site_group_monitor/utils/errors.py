"""统一异常定义。

约定（对应架构文档 §7.4）：
    * 业务层抛出的自定义异常必须继承 :class:`SgmError`；
    * 外部库异常在 ``core/*`` 边界转换为本模块中的类型；
    * 采集内核任何子项异常都不得冒泡到编排层，须降级为中国态 ``unknown``。
"""

from __future__ import annotations

from typing import Any


class SgmError(Exception):
    """所有 site_group_monitor 业务异常的基类。

    Attributes:
        message: 人类可读的错误说明。
        detail: 结构化的附加信息字典，便于写审计日志 / API 返回。
    """

    def __init__(self, message: str = "未知业务异常", **detail: Any) -> None:
        """构造业务异常。

        Args:
            message: 错误说明。
            **detail: 附加上下文信息（如 domain、proxy_id、http_status）。
        """
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写日志 / 可返回的字典。

        Returns:
            形如 ``{"error": "CaptchaError", "message": "...", "detail": {...}}`` 的字典。
        """
        return {
            "error": type(self).__name__,
            "message": self.message,
            "detail": self.detail,
        }


class ConfigError(SgmError):
    """配置加载 / 校验失败（如 YAML 非法、绑定 0.0.0.0 被拒绝）。"""


class StorageError(SgmError):
    """数据库读写失败（含 SQLite 锁竞争、约束冲突等）。"""


class NetworkError(SgmError):
    """网络层失败：连接失败、超时、DNS 失败等。"""


class DnsError(NetworkError):
    """域名解析失败。"""


class TimeoutError(NetworkError):  # noqa: A001 - 语义异常名，屏蔽内建同名告警
    """请求超时（注意：非内建 TimeoutError，导入时需显式区分）。"""


class ProxyError(NetworkError):
    """代理连接失败 / 407 / 被目标拒绝。"""


class CaptchaError(SgmError):
    """命中百度验证码 / 风控页（一定要降级为 unknown，绝不判未收录）。"""


class ParseError(SgmError):
    """结果页 / HTML 结构不可识别。"""


class RateLimitedError(SgmError):
    """被目标站点限速或返回空白页，需要全局降速。"""


class ReportError(SgmError):
    """报告渲染 / 导出失败。"""


class NotifyError(SgmError):
    """邮件等通知通道发送失败。"""
