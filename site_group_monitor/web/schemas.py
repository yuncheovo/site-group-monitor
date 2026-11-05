"""Web 层请求/响应模型与统一信封。

统一信封（对应架构文档 §7.8）：
    成功 → ``{"code": 0, "data": {...}, "message": "ok"}``
    失败 → ``{"code": 非0, "data": null, "message": "原因"}``，HTTP 状态码同步（400/404/500）。
分页统一为 ``{items, total, page, size}``。
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一 API 响应信封。"""

    code: int = 0
    data: T | None = None
    message: str = "ok"


class Page(BaseModel, Generic[T]):
    """统一分页结构。"""

    items: list[T] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    size: int = 50


# ------------------------------------------------------------------ 请求模型
class SiteCreateRequest(BaseModel):
    """新增站点请求体。"""

    model_config = ConfigDict(extra="ignore")

    domain: str
    group: str | None = None
    tags: list[str] = Field(default_factory=list)
    root_url: str = ""
    name: str = ""
    notes: str = ""


class ImportRequest(BaseModel):
    """批量导入请求体。"""

    model_config = ConfigDict(extra="ignore")

    payload: str
    fmt: str = "auto"


class TagsUpdateRequest(BaseModel):
    """标签更新请求体。"""

    model_config = ConfigDict(extra="ignore")

    tags: list[str] = Field(default_factory=list)


class EnabledUpdateRequest(BaseModel):
    """启停站点请求体。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True


class ProxyCreateRequest(BaseModel):
    """新增代理请求体。"""

    model_config = ConfigDict(extra="ignore")

    protocol: str = "http"
    host: str
    port: int
    username: str = ""
    password: str = ""
    name: str = ""
    weight: float = 1.0
    source: str = "manual"
    source_ref: str = ""


class ProxyBatchRequest(BaseModel):
    """批量添加固定 IP 代理请求体。"""

    model_config = ConfigDict(extra="ignore")

    protocol: str = "http"
    raw: str
    default_weight: float = 1.0
    username: str = ""
    password: str = ""


class ProxyApiSourceRequest(BaseModel):
    """代理 API 源配置请求体。"""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    url: str
    protocol: str = "http"
    username: str = ""
    password: str = ""
    refresh_interval_min: int = 30
    rotate_per_check: bool = False
    enabled: bool = True


class ProxyApiSourcePolicyRequest(BaseModel):
    """修改代理 API 源的 IP 切换策略。"""

    model_config = ConfigDict(extra="ignore")

    rotate_per_check: bool


class ProxyToggleRequest(BaseModel):
    """代理启停请求体。"""

    model_config = ConfigDict(extra="ignore")

    disabled: bool = True


class SettingsPatchRequest(BaseModel):
    """配置补丁请求体（嵌套字典，合并进 ``config.yaml``）。"""

    model_config = ConfigDict(extra="ignore")

    patch: dict[str, Any] = Field(default_factory=dict)


class RunRequest(BaseModel):
    """手动触发检测请求体。"""

    model_config = ConfigDict(extra="ignore")

    mode: str = "full"
    concurrency: int | None = None
    resume: bool = False
    notify: bool = False
    export: bool = True
    domains: list[str] = Field(default_factory=list)


class BaiduSubmitRequest(BaseModel):
    """百度资源平台浏览器提交请求（仅页面模式）。"""

    model_config = ConfigDict(extra="ignore")

    domains: list[str] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=100)


# ------------------------------------------------------------------ 响应模型
class SiteItem(BaseModel):
    """站点列表项（含最新检测快照字段）。"""

    model_config = ConfigDict(extra="allow")

    id: int | None = None
    domain: str = ""
    group_name: str = ""
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    index_status: str = ""
    index_reason: str = ""
    http_status: int | None = None
    response_ms: int | None = None
    ssl_days_left: int | None = None
    title: str = ""
    checked_at: str | None = None
    last_checked_at: str | None = None
    health: str = "unknown"


class HealthOk(BaseModel):
    """健康检查响应。"""

    status: str = "ok"
    version: str = ""
    timezone: str = ""


# ------------------------------------------------------------------ 工具函数
def ok(data: Any = None, message: str = "ok") -> dict[str, Any]:
    """构造成功信封。

    Args:
        data: 业务数据。
        message: 提示语。

    Returns:
        信封字典。
    """
    return {"code": 0, "data": data, "message": message}


def fail(message: str, code: int = 400, data: Any = None) -> dict[str, Any]:
    """构造失败信封。

    Args:
        message: 错误提示。
        code: 业务错误码。
        data: 附加数据。

    Returns:
        信封字典。
    """
    return {"code": code, "data": data, "message": message}


def page_of(items: list[Any], total: int, page: int, size: int) -> dict[str, Any]:
    """构造分页信封。

    Args:
        items: 当前页数据。
        total: 总数。
        page: 页码。
        size: 每页条数。

    Returns:
        分页字典。
    """
    return {"items": items, "total": total, "page": page, "size": size}
