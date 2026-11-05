"""跨模块唯一语义枚举。

收录状态**只有三态**（对应 PRD G2 底线）：
    * ``indexed`` 已收录；
    * ``not_indexed`` 未收录（仅在证据充分时判定）；
    * ``unknown_need_manual_check`` 查询失败（验证码/网络/解析失败，不计收录率、不产生 diff）。
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """可 JSON 序列化的字符串枚举基类。

    使用 ``StrEnum`` 语义保证写入 SQLite 与返回 API 时都是普通字符串。
    """

    def __str__(self) -> str:
        """返回枚举值本身。"""
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        """返回全部枚举值字符串列表。"""
        return [str(item.value) for item in cls]

    @classmethod
    def from_text(cls, text: str | None, default: "StrEnum | None" = None) -> "StrEnum | None":
        """宽松解析字符串为枚举。

        Args:
            text: 待解析文本。
            default: 解析失败时的默认值。

        Returns:
            枚举实例或默认值 / None。
        """
        if text is None:
            return default
        target = str(text).strip().lower()
        for item in cls:
            if str(item.value).lower() == target or item.name.lower() == target:
                return item
        return default


class IndexStatus(StrEnum):
    """百度收录三态。"""

    INDEXED = "indexed"
    NOT_INDEXED = "not_indexed"
    UNKNOWN = "unknown_need_manual_check"


class IndexReasonCode(StrEnum):
    """收录判定的原因码（用于报告「待人工确认」清单与排障）。"""

    OK_RESULT_FOUND = "ok_result_found"
    EMPTY_RESULT_PAGE = "empty_result_page"
    NO_DOMAIN_MATCH = "no_domain_match"
    CAPTCHA = "captcha"
    NETWORK_ERROR = "network_error"
    PROXY_FAILED = "proxy_failed"
    PARSE_FAILED = "parse_failed"
    TIMEOUT = "timeout"
    DNS_FAILED = "dns_failed"


class EventType(StrEnum):
    """变化事件类型（diff 产物，驱动报告与告警）。"""

    NEW_INDEXED = "new_indexed"
    LOST_INDEXED = "lost_indexed"
    UNKNOWN_PERSISTENT = "unknown_persistent"
    SSL_EXPIRING = "ssl_expiring"
    HTTP_ANOMALY = "http_anomaly"
    REDIRECT_ANOMALY = "redirect_anomaly"
    DNS_FAILED = "dns_failed"
    TDK_CHANGED = "tdk_changed"
    RATE_DROP = "index_rate_drop"


class RunMode(StrEnum):
    """检测批次模式。"""

    FULL = "full"
    INCREMENTAL = "incremental"
    MANUAL_SINGLE = "manual_single"


class ErrorType(StrEnum):
    """站点级错误分类（写入 ``check_results.error_type``）。"""

    DNS_FAILED = "dns_failed"
    CONN_FAILED = "conn_failed"
    TIMEOUT = "timeout"
    TLS_FAILED = "tls_failed"
    HTTP_5XX = "http_5xx"
    CAPTCHA = "captcha"
    PARSE_FAILED = "parse_failed"
    NONE = "none"


class SiteHealth(StrEnum):
    """站点整体健康度（UI 高亮与筛选用）。"""

    HEALTHY = "healthy"
    WARN = "warn"
    ABNORMAL = "abnormal"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    """运行批次状态。"""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProxyProtocol(StrEnum):
    """代理协议。"""

    HTTP = "http"
    HTTPS = "https"
    SOCKS5 = "socks5"


class SortField(StrEnum):
    """站点列表常用排序字段。"""

    DOMAIN = "domain"
    INDEX_STATUS = "index_status"
    HTTP_STATUS = "http_status"
    RESPONSE_MS = "response_ms"
    SSL_DAYS_LEFT = "ssl_days_left"
    LAST_CHECKED_AT = "last_checked_at"
    GROUP_NAME = "group_name"
