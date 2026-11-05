"""Pydantic 实体定义（对应架构文档 §3.1 类图）。

约定：
    * 时间字段一律是 **UTC ISO8601 字符串**；布尔以 0/1 存 SQLite，实体内用 bool；
    * ``from_row`` / ``to_row`` 负责与数据库行的映射，避免业务代码里出现裸字段。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from site_group_monitor.models.enums import (
    ErrorType,
    IndexReasonCode,
    IndexStatus,
    ProxyProtocol,
    RunMode,
    RunStatus,
)
from site_group_monitor.utils.netutil import clean_text, mask_url, normalize_domain, sha1_short


class _BaseEntity(BaseModel):
    """所有实体的公共基类：宽松解析 + 允许额外字段。

    允许额外字段是为了让 Repository 的左连结果（如 ``domain``）可以直接构造视图模型。
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Site(_BaseEntity):
    """站点主表实体。"""

    id: int | None = None
    domain: str = ""
    root_url: str = ""
    name: str = ""
    group_name: str = "default"
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    baseline_done: bool = False
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_checked_at: str | None = None

    # 以下为「列表视图」可选展示字段（来自 latest check_results 左连，不落 sites 表）
    index_status: str = ""
    index_reason: str = ""
    http_status: int | None = None
    response_ms: int | None = None
    ssl_days_left: int | None = None
    title: str = ""
    checked_at: str | None = None

    @field_validator("domain", mode="before")
    @classmethod
    def _normalize_domain(cls, value: Any) -> str:
        """写入实体前自动归一化域名。"""
        return normalize_domain(str(value or ""))

    @field_validator("index_status", "index_reason", "title", mode="before")
    @classmethod
    def _none_to_empty_str(cls, value: Any) -> str:
        """左连 ``check_results`` 未命中时列值为 NULL，统一转空串（避免校验失败）。"""
        return "" if value is None else str(value)

    def effective_root_url(self) -> str:
        """返回实际用于抓取的首页 URL。

        Returns:
            用户覆盖过的 root_url，否则 ``https://{domain}/``。
        """
        if self.root_url:
            return self.root_url
        return f"https://{self.domain}/" if self.domain else ""

    def tags_text(self, separator: str = "|") -> str:
        """标签列表转文本（CSV 导出用）。

        Args:
            separator: 分隔符。

        Returns:
            标签文本。
        """
        return separator.join(self.tags)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Site":
        """从数据库行构造站点（自动处理 tags JSON 与 bool 字段）。

        Args:
            row: sqlite3.Row 或 dict。

        Returns:
            :class:`Site` 实例。
        """
        data = dict(row)
        tags_raw = data.pop("tags", None)
        tags: list[str] = []
        if isinstance(tags_raw, (list, tuple)):
            tags = [str(item) for item in tags_raw]
        elif tags_raw:
            import json

            try:
                loaded = json.loads(str(tags_raw))
                tags = [str(item) for item in loaded]
            except Exception:  # noqa: BLE001 - 兼容历史脏数据
                tags = [part for part in str(tags_raw).split("|") if part]
        data["tags"] = tags
        data["enabled"] = bool(data.get("enabled", 1))
        data["baseline_done"] = bool(data.get("baseline_done", 0))
        return cls(**data)

    def to_row(self) -> dict[str, Any]:
        """转为 sites 表可插入的行字典。

        Returns:
            行字典（tags 已 JSON 序列化，bool 已转 int）。
        """
        import json

        return {
            "domain": self.domain,
            "root_url": self.root_url,
            "name": self.name,
            "group_name": self.group_name,
            "tags": json.dumps(self.tags, ensure_ascii=False),
            "enabled": int(self.enabled),
            "baseline_done": int(self.baseline_done),
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_checked_at": self.last_checked_at,
        }


class TdkInfo(_BaseEntity):
    """首页 TDK 信息。"""

    title: str = ""
    description: str = ""
    keywords: str = ""
    source_url: str = ""
    encoding: str = ""
    title_missing: bool = True
    desc_missing: bool = True
    keywords_missing: bool = True
    desc_source: str = ""          # meta / og / twitter / ""
    parse_warning: str = ""
    error_type: str = ErrorType.NONE.value
    title_hash: str = ""
    desc_hash: str = ""

    def apply_hashes(self) -> None:
        """计算 title/description 摘要，用于变更检测。"""
        self.title_hash = sha1_short(self.title)
        self.desc_hash = sha1_short(self.description)


class SslInfo(_BaseEntity):
    """SSL 证书探测结果。"""

    ok: bool | None = None
    issuer: str = ""
    subject: str = ""
    not_before: str = ""
    not_after: str = ""
    serial: str = ""
    days_left: int | None = None
    fail_reason: str = ""
    error_type: str = ErrorType.NONE.value


class SiteStatusInfo(_BaseEntity):
    """站点可访问性探测结果（DNS / HTTP / 重定向 / SSL / 混合内容）。"""

    dns_ok: bool = True
    http_status: int | None = None
    response_ms: int | None = None
    final_url: str = ""
    redirect_chain: list[dict[str, Any]] = Field(default_factory=list)
    anomalies: dict[str, Any] = Field(default_factory=dict)
    mixed_content: bool = False
    ssl: SslInfo = Field(default_factory=SslInfo)
    error_type: str = ErrorType.NONE.value
    error_msg: str = ""


class IndexResult(_BaseEntity):
    """搜索引擎收录检测产出（三态 + 原因码 + 证据路径）。"""

    status: IndexStatus = IndexStatus.UNKNOWN
    reason: IndexReasonCode = IndexReasonCode.PARSE_FAILED
    result_count: int = 0
    top_url: str = ""
    confidence: float = 0.0
    evidence_path: str = ""
    http_status: int | None = None
    proxy_id: int | None = None
    elapsed_ms: int = 0
    error_type: str = ErrorType.NONE.value
    error_msg: str = ""
    engine: str = "baidu"


class BaiduParseOutcome(_BaseEntity):
    """百度结果页纯解析结果（``baidu_parser.parse`` 的返回值，无 I/O）。"""

    has_captcha: bool = False
    captcha_marker: str = ""
    has_empty_hint: bool = False
    empty_hint_text: str = ""
    has_result_nodes: bool = False
    nums_count: int = -1          # -1 表示未取到「百度为您找到相关结果约 N 个」
    hits: list[dict[str, str]] = Field(default_factory=list)
    domain_matched: bool = False
    raw_hash: str = ""
    page_len: int = 0


class ProxyEndpoint(_BaseEntity):
    """代理端点实体（含健康度反馈方法）。"""

    id: int | None = None
    name: str = ""
    protocol: str = ProxyProtocol.HTTP.value
    host: str = ""
    port: int = 0
    username: str = ""
    password_enc: str = ""        # 明文标记（架构注明：数据不外泄，日志一律脱敏）
    weight: float = 1.0
    success_count: int = 0
    fail_count: int = 0
    consecutive_fail: int = 0
    health_score: float = 1.0
    disabled: bool = False
    source: str = "manual"
    source_ref: str = ""
    last_ok_at: str | None = None
    last_fail_at: str | None = None
    created_at: str = ""
    last_egress_ip: str = ""
    last_location: str = ""
    last_latency_ms: int | None = None
    last_probe_at: str | None = None
    last_probe_error: str = ""

    # 运行期状态（不落库）
    in_flight: int = 0
    captcha_hits: int = 0

    @property
    def key(self) -> str:
        """代理唯一 key（用于限速器与 httpx 客户端分片）。"""
        return f"{self.protocol}://{self.host}:{self.port}"

    @property
    def url(self) -> str:
        """带鉴权信息的代理 URL（httpx 使用）。"""
        if self.username and self.password_enc:
            return f"{self.protocol}://{self.username}:{self.password_enc}@{self.host}:{self.port}"
        return f"{self.protocol}://{self.host}:{self.port}"

    @property
    def masked_url(self) -> str:
        """脱敏后的代理 URL（日志/报告展示用）。"""
        return mask_url(self.url)

    def mark_ok(self) -> None:
        """记录一次成功：健康度回升、重置连续失败。"""
        self.success_count += 1
        self.consecutive_fail = 0
        self.health_score = min(1.0, round(self.health_score + 0.05, 4))
        from site_group_monitor.utils.timeutil import utcnow_iso

        self.last_ok_at = utcnow_iso()

    def mark_fail(self, kind: str = "fail") -> None:
        """记录一次失败：健康度下降、累计失败计数。

        Args:
            kind: 失败类型，``fail`` / ``captcha`` / ``proxy``（captcha 扣更多分）。
        """
        penalty = 0.3 if kind == "captcha" else 0.15
        self.fail_count += 1
        self.consecutive_fail += 1
        if kind == "captcha":
            self.captcha_hits += 1
        self.health_score = max(0.0, round(self.health_score - penalty, 4))
        from site_group_monitor.utils.timeutil import utcnow_iso

        self.last_fail_at = utcnow_iso()

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ProxyEndpoint":
        """从数据库行构造代理实体。

        Args:
            row: sqlite3.Row 或 dict。

        Returns:
            :class:`ProxyEndpoint` 实例。
        """
        data = dict(row)
        data["disabled"] = bool(data.get("disabled", 0))
        latency = data.get("last_latency_ms")
        if latency is not None and latency != "":
            try:
                data["last_latency_ms"] = int(latency)
            except (TypeError, ValueError):
                data["last_latency_ms"] = None
        else:
            data["last_latency_ms"] = None
        return cls(**data)

    def to_row(self) -> dict[str, Any]:
        """转为 proxies 表可插入的行字典。

        Returns:
            行字典（bool 转 int）。
        """
        return {
            "name": self.name,
            "protocol": self.protocol,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "password_enc": self.password_enc,
            "weight": self.weight,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "consecutive_fail": self.consecutive_fail,
            "health_score": self.health_score,
            "disabled": int(self.disabled),
            "source": self.source,
            "source_ref": self.source_ref,
            "last_ok_at": self.last_ok_at,
            "last_fail_at": self.last_fail_at,
            "created_at": self.created_at,
        }


class ProxyApiSource(_BaseEntity):
    """代理 API 源：配置一个返回 IP 列表的接口，系统定时拉取并填充代理池。"""

    id: int | None = None
    name: str = ""
    url: str = ""
    protocol: str = "http"
    username: str = ""
    password_enc: str = ""            # 明文存放（本地工具），展示/日志一律脱敏
    refresh_interval_min: int = 30
    rotate_per_check: bool = False
    enabled: bool = True
    last_fetch_at: str | None = None
    last_fetch_count: int = 0
    last_error: str = ""
    created_at: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ProxyApiSource":
        """从数据库行构造 API 源实体。"""
        data = dict(row)
        data["enabled"] = bool(data.get("enabled", 1))
        data["rotate_per_check"] = bool(data.get("rotate_per_check", 0))
        data["refresh_interval_min"] = int(data.get("refresh_interval_min", 30) or 30)
        data["last_fetch_count"] = int(data.get("last_fetch_count", 0) or 0)
        return cls(**data)

    def to_row(self) -> dict[str, Any]:
        """转为 proxy_api_sources 表可插入的行字典。

        Returns:
            行字典（bool 转 int）。
        """
        return {
            "name": self.name,
            "url": self.url,
            "protocol": self.protocol,
            "username": self.username,
            "password_enc": self.password_enc,
            "refresh_interval_min": self.refresh_interval_min,
            "rotate_per_check": int(self.rotate_per_check),
            "enabled": int(self.enabled),
            "last_fetch_at": self.last_fetch_at,
            "last_fetch_count": self.last_fetch_count,
            "last_error": self.last_error,
            "created_at": self.created_at,
        }


class CheckResult(_BaseEntity):
    """一次站点全维度检测的明细结果。"""

    id: int | None = None
    site_id: int = 0
    task_id: str = ""
    run_mode: str = RunMode.FULL.value
    checked_at: str = ""
    index_status: str = IndexStatus.UNKNOWN.value
    index_reason: str = ""
    index_confidence: float = 0.0
    index_result_count: int = 0
    index_top_url: str = ""
    evidence_path: str = ""
    http_status: int | None = None
    index_http_status: int | None = None
    response_ms: int | None = None
    final_url: str = ""
    redirect_chain: list[dict[str, Any]] = Field(default_factory=list)
    anomalies: dict[str, Any] = Field(default_factory=dict)
    tdk: TdkInfo = Field(default_factory=TdkInfo)
    ssl: SslInfo = Field(default_factory=SslInfo)
    error_type: str = ErrorType.NONE.value
    error_msg: str = ""
    proxy_id: int | None = None
    duration_ms: int = 0

    def to_row(self) -> dict[str, Any]:
        """展平为 check_results 表行字典。

        Returns:
            可直接用于 INSERT 的字典。
        """
        import json

        return {
            "site_id": self.site_id,
            "task_id": self.task_id,
            "run_mode": self.run_mode,
            "checked_at": self.checked_at,
            "index_status": str(self.index_status),
            "index_reason": str(self.index_reason),
            "index_confidence": float(self.index_confidence),
            "index_result_count": int(self.index_result_count),
            "index_top_url": self.index_top_url,
            "evidence_path": self.evidence_path,
            "http_status": self.http_status,
            "index_http_status": self.index_http_status,
            "response_ms": self.response_ms,
            "final_url": self.final_url,
            "redirect_chain": json.dumps(self.redirect_chain, ensure_ascii=False),
            "anomalies": json.dumps(self.anomalies, ensure_ascii=False),
            "title": self.tdk.title,
            "description": self.tdk.description,
            "keywords": self.tdk.keywords,
            "tdk_source_url": self.tdk.source_url,
            "tdk_encoding": self.tdk.encoding,
            "tdk_title_hash": self.tdk.title_hash,
            "ssl_ok": None if self.ssl.ok is None else int(self.ssl.ok),
            "ssl_issuer": self.ssl.issuer,
            "ssl_not_after": self.ssl.not_after,
            "ssl_days_left": self.ssl.days_left,
            "error_type": str(self.error_type),
            "error_msg": self.error_msg,
            "proxy_id": self.proxy_id,
            "duration_ms": int(self.duration_ms),
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "CheckResult":
        """从数据库行还原 CheckResult（含嵌套 TdkInfo / SslInfo）。

        Args:
            row: sqlite3.Row 或 dict。

        Returns:
            :class:`CheckResult` 实例。
        """
        import json

        data = dict(row)

        def _load_json(key: str, fallback: Any) -> Any:
            raw = data.pop(key, None)
            if raw is None or raw == "":
                return fallback
            try:
                return json.loads(raw)
            except Exception:  # noqa: BLE001
                return fallback

        redirect_chain = _load_json("redirect_chain", [])
        anomalies = _load_json("anomalies", {})
        tdk = TdkInfo(
            title=data.pop("title", "") or "",
            description=data.pop("description", "") or "",
            keywords=data.pop("keywords", "") or "",
            source_url=data.pop("tdk_source_url", "") or "",
            encoding=data.pop("tdk_encoding", "") or "",
            title_hash=data.pop("tdk_title_hash", "") or "",
        )
        ssl_ok = data.pop("ssl_ok", None)
        ssl = SslInfo(
            ok=None if ssl_ok is None else bool(ssl_ok),
            issuer=data.pop("ssl_issuer", "") or "",
            not_after=data.pop("ssl_not_after", "") or "",
            days_left=data.pop("ssl_days_left", None),
        )
        result = cls(
            tdk=tdk,
            ssl=ssl,
            redirect_chain=redirect_chain,
            anomalies=anomalies,
            **{k: v for k, v in data.items() if k in cls.model_fields},
        )
        return result

    def health(self, ssl_warn_days: int = 15) -> str:
        """给出该次结果的健康度标签。

        Args:
            ssl_warn_days: SSL 预警天数阈值。

        Returns:
            ``healthy`` / ``warn`` / ``abnormal`` / ``unknown``。
        """
        from site_group_monitor.models.enums import SiteHealth

        if self.index_status == IndexStatus.UNKNOWN.value or self.error_type not in (
            ErrorType.NONE.value,
            "",
        ):
            return SiteHealth.UNKNOWN.value
        abnormal = bool(
            (self.http_status or 0) >= 400
            or self.http_status is None
            or self.anomalies.get("redirect_anomaly")
        )
        if abnormal:
            return SiteHealth.ABNORMAL.value
        warn = bool(
            self.anomalies.get("mixed_content")
            or (self.response_ms or 0) > 3000
        )
        return SiteHealth.WARN.value if warn else SiteHealth.HEALTHY.value


class DailySnapshot(_BaseEntity):
    """每日快照（趋势与 diff 的唯一数据源）。"""

    id: int | None = None
    site_id: int = 0
    stat_date: str = ""
    index_status: str = IndexStatus.UNKNOWN.value
    http_status: int | None = None
    response_ms: int | None = None
    ssl_days_left: int | None = None
    title: str = ""
    tdk_title_hash: str = ""
    is_baseline: int = 0
    created_at: str = ""

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "DailySnapshot":
        """从数据库行构造快照实体。"""
        return cls(**{k: v for k, v in dict(row).items() if k in cls.model_fields})


class ChangeEvent(_BaseEntity):
    """变化事件（diff 产物）。"""

    id: int | None = None
    site_id: int = 0
    event_type: str = ""
    old_value: str = ""
    new_value: str = ""
    stat_date: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    notified: bool = False
    created_at: str = ""
    domain: str = ""          # 展示用，不入详情 JSON

    def to_row(self) -> dict[str, Any]:
        """转为 change_events 表行字典。"""
        import json

        return {
            "site_id": self.site_id,
            "event_type": self.event_type,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "stat_date": self.stat_date,
            "detail": json.dumps(self.detail, ensure_ascii=False),
            "notified": int(self.notified),
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "ChangeEvent":
        """从数据库行构造事件实体。"""
        import json

        data = dict(row)
        raw_detail = data.pop("detail", "{}")
        try:
            detail = json.loads(raw_detail) if raw_detail else {}
        except Exception:  # noqa: BLE001
            detail = {}
        data["detail"] = detail
        data["notified"] = bool(data.get("notified", 0))
        return cls(**{k: v for k, v in data.items() if k in cls.model_fields})


class DailyReport(_BaseEntity):
    """每日报告聚合体。"""

    stat_date: str = ""
    generated_at: str = ""
    total_sites: int = 0
    indexed_count: int = 0
    not_indexed_count: int = 0
    unknown_count: int = 0
    index_rate: float = 0.0
    prev_rate: float | None = None
    anomalous_count: int = 0
    ssl_expiring_count: int = 0
    new_indexed: list[dict[str, Any]] = Field(default_factory=list)
    lost_indexed: list[dict[str, Any]] = Field(default_factory=list)
    need_manual: list[dict[str, Any]] = Field(default_factory=list)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    baseline_created: list[dict[str, Any]] = Field(default_factory=list)
    trend: list[dict[str, Any]] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典（模板渲染 / API 返回用）。"""
        return self.model_dump()

    def checkable_count(self) -> int:
        """收录率分母（排除 unknown）。

        Returns:
            indexed + not_indexed 的数量。
        """
        return self.indexed_count + self.not_indexed_count


class RunSummary(_BaseEntity):
    """一轮检测的汇总。"""

    task_id: str = ""
    run_id: int | None = None
    mode: str = RunMode.FULL.value
    status: str = RunStatus.SUCCESS.value
    total: int = 0
    indexed: int = 0
    not_indexed: int = 0
    unknown: int = 0
    failed: int = 0
    concurrency: int = 0
    proxies_used: int = 0
    duration_ms: int = 0
    started_at: str = ""
    finished_at: str = ""
    report_paths: dict[str, str] = Field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为字典用于 API 与日志。"""
        return self.model_dump()


class ImportSummary(_BaseEntity):
    """批量导入汇总。"""

    total_rows: int = 0
    added: int = 0
    updated: int = 0
    skipped: int = 0
    invalid: list[dict[str, Any]] = Field(default_factory=list)
    duplicates: list[str] = Field(default_factory=list)


class OverviewCard(_BaseEntity):
    """仪表盘总览卡片数据。"""

    total_sites: int = 0
    enabled_sites: int = 0
    index_rate: float = 0.0
    indexed_count: int = 0
    not_indexed_count: int = 0
    unknown_count: int = 0
    unchecked_count: int = 0
    delta_vs_yesterday: int = 0
    anomalous_count: int = 0
    ssl_expiring_count: int = 0
    proxy_health_rate: float = 0.0
    proxy_total: int = 0
    proxy_enabled: int = 0
    proxy_empty: bool = True
    last_run: dict[str, Any] = Field(default_factory=dict)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为 API 友好字典。"""
        return self.model_dump()


class ProgressSnapshot(_BaseEntity):
    """统一的进度快照结构。"""

    phase: str = "idle"
    total: int = 0
    done: int = 0
    ok: int = 0
    failed: int = 0
    unknown: int = 0
    running: int = 0
    concurrency: int = 0
    elapsed_ms: int = 0
    eta_ms: int = 0
    qps: float = 0.0
    message: str = ""
    task_id: str = ""


def clean_text_field(value: str | None, limit: int | None = None) -> str:
    """清洗文本字段的便捷包装（供 parser 使用）。

    Args:
        value: 原始文本。
        limit: 截断长度。

    Returns:
        清洗后的文本。
    """
    return clean_text(value, limit)
