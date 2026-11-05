"""pydantic Settings：代码默认 < data/config.yaml < SGM_* 环境变量。

设计要点（对应架构文档 §7.1）：
    * 唯一入口 :func:`get_settings`（lru_cache 单例），并提供 :func:`reload_settings` 热更新；
    * 加载优先级：``DEFAULT_CONFIG`` < ``data/config.yaml`` < 环境变量 ``SGM_*``；
    * 其他模块**不得**私自 ``os.getenv``；
    * 环境变量嵌套分隔符为双下划线 ``__``，例如 ``SGM_SMTP__PASSWORD``；
      为兼容口语化写法，同时支持省略 ``engine.`` 段的别名（``SGM_BAIDU__MIN_INTERVAL_SEC``）。
"""

from __future__ import annotations

import copy
import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings

# 包根目录（site_group_monitor 包目录），项目根目录为其上一级
def _is_frozen() -> bool:
    """判断当前是否运行在 PyInstaller 打包后的 exe 中。"""
    return bool(getattr(sys, "frozen", False))


def _bundle_root() -> Path:
    """只读资源根目录（schema.sql / 模板 / 前端静态文件）。

    打包后资源被解压到 ``sys._MEIPASS`` 临时目录；源码运行时是**项目根**
    （包目录的上一级），这样 ``config/config.example.yaml``、
    ``site_group_monitor/storage/schema.sql`` 等相对路径才成立。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return Path(__file__).resolve().parents[2]


def _data_root() -> Path:
    """可写数据根目录（data/ 库、报告、证据、日志）。

    打包后**必须**用 exe 所在目录，不能用临时解压目录——否则：
    * 用户的数据会在进程退出时被 PyInstaller 清理；
    * 每次启动都是全新空库，站点与历史全丢。

    源码运行时同样是**项目根**（包目录的上一级），避免把用户数据写进
    Python 包目录里（那会跟着源码树到处跑，且污染 site-packages）。
    """
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


PACKAGE_ROOT = _bundle_root()
PROJECT_ROOT = _data_root()

#: 环境变量别名：口语化段名 -> 真实配置路径（点分）
ENV_ALIASES: dict[str, str] = {
    "BAIDU": "engine.baidu",
    "SMTP": "smtp",
    "HTTP": "http",
    "PROXY": "proxy",
    "ALERT": "alert",
    "WEB": "web",
    "STORAGE": "storage",
    "RETENTION": "retention",
    "SCHEDULER": "scheduler",
    "CONCURRENCY": "concurrency",
    "TDK": "tdk",
    "IMPORT": "import",
    "REPORT": "report",
    "APP": "app",
    "ENGINE": "engine",
}

#: 代码内权威默认配置（与 config/config.example.yaml 保持一致）
DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "name": "site_group_monitor",
        "display_timezone": "Asia/Shanghai",
        "log_level": "INFO",
    },
    "storage": {
        "db_path": "data/monitor.db",
        "wal": True,
        "busy_timeout_ms": 5000,
        "backup": {"enabled": True, "keep": 7},
    },
    "http": {
        "timeout_connect": 5,
        "timeout_read": 10,
        "timeout_total": 20,
        "timeout_pool": 3,
        "max_redirects": 5,
        "verify_ssl": True,
        "pool_size": 200,
        "headers_pool_size": 20,
        "user_agent_rotation": True,
    },
    "concurrency": {
        "global_workers": 6,
        "per_proxy_workers": 1,
        "no_proxy_workers": 3,
        "batch_size": 50,
        "batch_pause_sec": [20, 40],
        "no_proxy_interval_factor": 1.8,
    },
    "engine": {
        "active": "baidu",
        "baidu": {
            "search_url": "https://www.baidu.com/s",
            "rn": 10,
            "min_interval_sec": 3.0,
            "jitter": [0.6, 1.4],
            "max_retry": 4,
            "http2": False,
            "max_requests_per_proxy": 25,
            "rest_after_threshold_sec": 45,
            "timeout_sec": 25,
            "retry_backoff_sec": [2, 4],
            "narrow_recheck": True,
            "captcha": {"mode": "degrade", "max_rotate": 3},
            "evidence_dir": "data/evidence",
            "evidence_days": 30,
            "browser": {
                "enabled": False,
                "channel": "auto",
                "headless": False,
                "via_proxy": True,
                "profile_dir": "data/browser-profile",
                "settle_ms": 600,
                "windows": 1,
            },
        },
    },
    "proxy": {
        "enabled": True,
        "auto_disable_health": 0.3,
        "fail_threshold": 3,
        "select_strategy": "weighted_random",
        "revive_after_minutes": 30,
    },
    "scheduler": {
        "enabled": True,
        "mode": "daily",
        "daily_time": "09:00",
        "jitter_minutes": 30,
        "misfire_grace_sec": 3600,
        "lock_sec": 7200,
        "incremental_sample_ratio": 0.0,
    },
    "alert": {
        "enabled": True,
        "index_rate_threshold": 0.70,
        "ssl_warn_days": 15,
        "unknown_persistent_days": 3,
        "quiet_hours": ["23:00", "07:30"],
        "dedup_hours": 12,
    },
    "smtp": {
        "enabled": False,
        "host": "smtp.qq.com",
        "port": 465,
        "use_ssl": True,
        "starttls": False,
        "user": "",
        "password": "",
        "sender": "",
        "recipients": [],
        "subject_prefix": "[站群监控]",
        "timeout_sec": 20,
        "retry": 3,
        "retry_backoff_sec": [30, 90, 180],
    },
    "retention": {
        "days": 180,
        "evidence_days": 30,
        "audit_days": 365,
    },
    "web": {
        "host": "127.0.0.1",
        "port": 8080,
        "auth_enabled": False,
        "static_dir": "site_group_monitor/web/static",
    },
    "tdk": {
        "max_bytes": 524288,
        "timeout_sec": 15,
        "fallback_meta": True,
        "render_fallback": {
            "enabled": False,
            "hint_selectors": ["__NEXT_DATA__", "#app", "#root"],
            "render_whitelist": [],
        },
    },
    "import": {
        "default_group": "default",
        "tag_separator": "|",
        "csv_delimiter": ",",
    },
    "report": {
        "dir": "data/reports",
        "keep_days": 365,
    },
}


class AppConfig(BaseModel):
    """应用级配置。"""

    model_config = ConfigDict(extra="ignore")

    name: str = "site_group_monitor"
    display_timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"


class BackupConfig(BaseModel):
    """SQLite 备份配置。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    keep: int = 7


class StorageConfig(BaseModel):
    """持久化配置。"""

    model_config = ConfigDict(extra="ignore")

    db_path: str = "data/monitor.db"
    wal: bool = True
    busy_timeout_ms: int = 5000
    backup: BackupConfig = Field(default_factory=BackupConfig)


class HttpConfig(BaseModel):
    """HTTP 客户端与超时配置。"""

    model_config = ConfigDict(extra="ignore")

    timeout_connect: int = 5
    timeout_read: int = 10
    timeout_total: int = 20
    timeout_pool: int = 3
    max_redirects: int = 5
    verify_ssl: bool = True
    pool_size: int = 200
    headers_pool_size: int = 20
    user_agent_rotation: bool = True


class ConcurrencyConfig(BaseModel):
    """并发与错峰配置。"""

    model_config = ConfigDict(extra="ignore")

    global_workers: int = 6
    per_proxy_workers: int = 1
    no_proxy_workers: int = 3
    batch_size: int = 50
    batch_pause_sec: list[float] = Field(default_factory=lambda: [20.0, 40.0])
    no_proxy_interval_factor: float = 1.8

    @field_validator("global_workers", "per_proxy_workers", "no_proxy_workers", "batch_size")
    @classmethod
    def _must_be_positive(cls, value: int) -> int:
        """并发与批量参数必须为正整数。

        这些值最终会传进 ``asyncio.Semaphore(n)``，而 ``Semaphore`` 对负数
        直接抛 ``ValueError``。这里在配置加载期就拦住，给出可读报错，
        而不是等跑到检测阶段才崩。
        """
        if int(value) < 1:
            raise ValueError(f"必须 >= 1，当前为 {value}")
        return int(value)

    @field_validator("no_proxy_interval_factor")
    @classmethod
    def _factor_at_least_one(cls, value: float) -> float:
        """无代理降级系数应 >= 1（降级只能放慢，不能加速）。"""
        if float(value) < 1.0:
            raise ValueError(f"必须 >= 1.0，当前为 {value}")
        return float(value)


class CaptchaConfig(BaseModel):
    """验证码处置策略。"""

    model_config = ConfigDict(extra="ignore")

    mode: str = "degrade"
    max_rotate: int = 3


class BrowserFetchConfig(BaseModel):
    """本机 Edge/Chrome 查收录与 TDK（独立配置目录）。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    channel: str = "auto"
    headless: bool = False
    via_proxy: bool = True
    profile_dir: str = "data/browser-profile"
    settle_ms: int = 600
    windows: int = 1


class BaiduConfig(BaseModel):
    """百度 ``site:`` 检测引擎配置。"""

    model_config = ConfigDict(extra="ignore")

    search_url: str = "https://www.baidu.com/s"
    rn: int = 10
    min_interval_sec: float = 3.0
    jitter: list[float] = Field(default_factory=lambda: [0.6, 1.4])
    max_retry: int = 4
    http2: bool = False
    max_requests_per_proxy: int = 25
    rest_after_threshold_sec: float = 45.0
    timeout_sec: int = 25
    retry_backoff_sec: list[float] = Field(default_factory=lambda: [2.0, 4.0])
    narrow_recheck: bool = True
    captcha: CaptchaConfig = Field(default_factory=CaptchaConfig)
    evidence_dir: str = "data/evidence"
    evidence_days: int = 30
    browser: BrowserFetchConfig = Field(default_factory=BrowserFetchConfig)


class EngineConfig(BaseModel):
    """搜索引擎选择配置（预留 R-019 多引擎扩展）。"""

    model_config = ConfigDict(extra="ignore")

    active: str = "baidu"
    baidu: BaiduConfig = Field(default_factory=BaiduConfig)


class ProxyConfig(BaseModel):
    """代理池配置。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    auto_disable_health: float = 0.3
    fail_threshold: int = 3
    select_strategy: str = "weighted_random"
    revive_after_minutes: int = 30
    acquire_timeout_sec: float = 5.0


class SchedulerConfig(BaseModel):
    """定时调度配置。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    mode: str = "daily"
    daily_time: str = "09:00"
    jitter_minutes: int = 30
    misfire_grace_sec: int = 3600
    lock_sec: int = 7200
    incremental_sample_ratio: float = 0.0


class AlertConfig(BaseModel):
    """告警阈值与静默时段配置。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    index_rate_threshold: float = 0.70
    ssl_warn_days: int = 15
    unknown_persistent_days: int = 3
    quiet_hours: list[str] = Field(default_factory=lambda: ["23:00", "07:30"])
    dedup_hours: int = 12


class SmtpConfig(BaseModel):
    """邮件推送配置（默认禁用，填入凭据后启用）。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    host: str = "smtp.qq.com"
    port: int = 465
    use_ssl: bool = True
    starttls: bool = False
    user: str = ""
    password: str = ""
    sender: str = ""
    recipients: list[str] = Field(default_factory=list)
    subject_prefix: str = "[站群监控]"
    timeout_sec: int = 20
    retry: int = 3
    retry_backoff_sec: list[float] = Field(default_factory=lambda: [30.0, 90.0, 180.0])


class RetentionConfig(BaseModel):
    """数据保留策略。"""

    model_config = ConfigDict(extra="ignore")

    days: int = 180
    evidence_days: int = 30
    audit_days: int = 365


class WebConfig(BaseModel):
    """本地仪表盘配置（默认只绑 127.0.0.1）。"""

    model_config = ConfigDict(extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8080
    auth_enabled: bool = False
    static_dir: str = "site_group_monitor/web/static"


class RenderFallbackConfig(BaseModel):
    """JS 渲染兜底配置（默认关闭，且不进主链路）。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    hint_selectors: list[str] = Field(default_factory=lambda: ["__NEXT_DATA__", "#app", "#root"])
    render_whitelist: list[str] = Field(default_factory=list)


class TdkConfig(BaseModel):
    """TDK 抓取配置。"""

    model_config = ConfigDict(extra="ignore")

    max_bytes: int = 524288
    timeout_sec: int = 15
    fallback_meta: bool = True
    render_fallback: RenderFallbackConfig = Field(default_factory=RenderFallbackConfig)


class ImportCsvConfig(BaseModel):
    """批量导入配置（字段名刻意避免内建 import 关键字冲突）。"""

    model_config = ConfigDict(extra="ignore")

    default_group: str = "default"
    tag_separator: str = "|"
    csv_delimiter: str = ","


class ReportDirConfig(BaseModel):
    """报告输出目录配置。"""

    model_config = ConfigDict(extra="ignore")

    dir: str = "data/reports"
    keep_days: int = 365


class Settings(BaseSettings):
    """全站配置对象（优先级：代码默认 < yaml < 环境变量）。"""

    model_config = ConfigDict(
        extra="ignore",
        env_prefix="SGM_",
        env_nested_delimiter="__",
        protected_namespaces=("settings_",),
        populate_by_name=True,
    )

    app: AppConfig = Field(default_factory=AppConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    concurrency: ConcurrencyConfig = Field(default_factory=ConcurrencyConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    alert: AlertConfig = Field(default_factory=AlertConfig)
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    tdk: TdkConfig = Field(default_factory=TdkConfig)
    import_cfg: ImportCsvConfig = Field(default_factory=ImportCsvConfig, alias="import")
    report: ReportDirConfig = Field(default_factory=ReportDirConfig)

    project_root: Path = PROJECT_ROOT

    # -------------------------------------------------- 构造与合并
    def __init__(self, **kwargs: Any) -> None:
        """构造 Settings：内部统一把写入的 dict 转成对应子模型。"""
        normalized = {**kwargs}
        for key in ("app", "storage", "http", "concurrency", "engine", "proxy",
                    "scheduler", "alert", "smtp", "retention", "web", "tdk", "report"):
            value = normalized.get(key)
            if isinstance(value, dict):
                normalized[key] = _coerce(key, value)
        if isinstance(normalized.get("import"), dict):
            normalized["import_cfg"] = ImportCsvConfig(**normalized.pop("import"))
        super().__init__(**normalized)

    @classmethod
    def load(cls, config_path: str | Path | None = None, *, include_env: bool = True) -> "Settings":
        """按优先级完整加载一次配置。

        Args:
            config_path: 用户级 yaml 路径，缺省为 ``<项目根>/data/config.yaml``。
            include_env: 是否叠加环境变量（测试时可关闭）。

        Returns:
            配置单例的新实例。
        """
        merged = copy.deepcopy(DEFAULT_CONFIG)

        yaml_data = _load_yaml(config_path)
        if yaml_data:
            merged = _deep_merge(merged, yaml_data)

        if include_env:
            merged = _deep_merge(merged, _env_overrides())

        # pydantic 的 import 字段名转成内部别名
        if "import" in merged:
            merged["import_cfg"] = merged.pop("import")
        settings = cls(**merged)
        if include_env and settings.web.host not in ("127.0.0.1", "localhost", "::1"):
            # 安全默认值：Web 只允许本地环回
            settings.web.host = "127.0.0.1"
        return settings

    # -------------------------------------------------- 路径辅助
    def resolve_path(self, raw: str | Path, *, must_exist: bool = False) -> Path:
        """把配置里的相对路径解析为绝对路径。

        Args:
            raw: 相对或绝对路径。
            must_exist: 为真时不存在会抛出 :class:`FileNotFoundError`。

        Returns:
            绝对路径对象。
        """
        path = Path(str(raw))
        if not path.is_absolute():
            path = (self.project_root / path).resolve()
        if must_exist and not path.exists():
            raise FileNotFoundError(f"路径不存在: {path}")
        return path

    @property
    def db_path(self) -> Path:
        """SQLite 主库绝对路径。"""
        return self.resolve_path(self.storage.db_path)

    @property
    def reports_dir(self) -> Path:
        """报告输出目录绝对路径。"""
        return self.resolve_path(self.report.dir)

    @property
    def evidence_dir(self) -> Path:
        """证据目录绝对路径。"""
        return self.resolve_path(self.engine.baidu.evidence_dir)

    @property
    def backups_dir(self) -> Path:
        """备份目录绝对路径。"""
        return self.resolve_path("data/backups")

    @property
    def logs_dir(self) -> Path:
        """日志目录绝对路径。"""
        return self.resolve_path("logs")

    @property
    def static_dir(self) -> Path:
        """Web 静态资源目录绝对路径。"""
        return self.resolve_path(self.web.static_dir)

    def ensure_dirs(self) -> None:
        """确保运行期目录全部存在（幂等）。"""
        for directory in (
            self.db_path.parent,
            self.reports_dir,
            self.evidence_dir,
            self.backups_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def timezone_name(self) -> str:
        """展示时区名。"""
        return self.app.display_timezone or "Asia/Shanghai"

    def to_yaml_dict(self) -> dict[str, Any]:
        """导出为 yaml 友好的字典（还原 ``import`` 段名）。

        Returns:
            配置字典。
        """
        data = self.model_dump(mode="python")
        data["import"] = data.pop("import_cfg", {})
        data.pop("project_root", None)
        data["project_root"] = str(self.project_root)
        return data


def _coerce(section: str, value: dict[str, Any]) -> Any:
    """把某个配置段的 dict 转成对应子模型。

    Args:
        section: 配置段名。
        value: 字典值。

    Returns:
        子模型实例；未知段原样返回 dict。
    """
    factories: dict[str, Any] = {
        "app": AppConfig,
        "storage": StorageConfig,
        "http": HttpConfig,
        "concurrency": ConcurrencyConfig,
        "engine": EngineConfig,
        "proxy": ProxyConfig,
        "scheduler": SchedulerConfig,
        "alert": AlertConfig,
        "smtp": SmtpConfig,
        "retention": RetentionConfig,
        "web": WebConfig,
        "tdk": TdkConfig,
        "report": ReportDirConfig,
    }
    model_cls = factories.get(section)
    if model_cls is None:
        return value
    if section == "engine" and isinstance(value.get("baidu"), dict):
        baidu = dict(value["baidu"])
        if isinstance(baidu.get("browser"), dict):
            baidu["browser"] = BrowserFetchConfig(**baidu["browser"])
        value = {**value, "baidu": BaiduConfig(**baidu)}
    if section == "storage" and isinstance(value.get("backup"), dict):
        value = {**value, "backup": BackupConfig(**value["backup"])}
    if section == "tdk" and isinstance(value.get("render_fallback"), dict):
        value = {**value, "render_fallback": RenderFallbackConfig(**value["render_fallback"])}
    return model_cls(**value)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个字典（后者覆盖前者）。

    Args:
        base: 基础字典。
        override: 覆盖字典。

    Returns:
        合并后的新字典。
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_yaml(config_path: str | Path | None = None) -> dict[str, Any]:
    """读取用户级 yaml 配置。

    Args:
        config_path: 指定路径；缺省依次尝试 ``data/config.yaml``。

    Returns:
        配置字典；文件不存在或解析失败返回空字典。
    """
    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path))
    else:
        candidates.append(PROJECT_ROOT / "data" / "config.yaml")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            loaded = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                return loaded
        except yaml.YAMLError:
            continue
    return {}


def _auto_cast(text: str) -> Any:
    """环境变量字符串智能转型（bool / int / float / list / dict / null）。

    Args:
        text: 原始字符串。

    Returns:
        转型后的 Python 对象。
    """
    stripped = text.strip()
    lowered = stripped.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "none", ""):
        return None
    if stripped.startswith(("[", "{")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
    if stripped.startswith("[") or "," in stripped and stripped.replace(",", "").replace(".", "").isdigit():
        # 形如 "20,40" 或 "23:00,07:30" 的区间/列表写法
        return [item.strip() for item in stripped.split(",") if item.strip()]
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        pass
    return text


def _set_nested(target: dict[str, Any], path: list[str], value: Any) -> None:
    """按点分路径写入嵌套字典。

    Args:
        target: 目标字典。
        path: 路径列表，如 ``["smtp", "password"]``。
        value: 待写入的值。
    """
    cursor = target
    for part in path[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[path[-1]] = value


def _env_overrides() -> dict[str, Any]:
    """扫描 ``SGM_*`` 环境变量，产出嵌套覆盖字典。

    Returns:
        形如 ``{"smtp": {"password": "xxx"}}`` 的覆盖字典。
    """
    overrides: dict[str, Any] = {}
    for key, raw in os.environ.items():
        if not key.startswith("SGM_"):
            continue
        parts = [part.lower() for part in key[len("SGM_"):].split("__") if part]
        if not parts:
            continue
        head = parts[0].upper()
        if head in ENV_ALIASES and len(parts) > 1:
            path = ENV_ALIASES[head].split(".") + parts[1:]
        else:
            path = parts
        # import 是 Python 关键字，内部用 import_cfg
        if path and path[0] == "import":
            path = ["import_cfg"] + path[1:]
        _set_nested(overrides, path, _auto_cast(raw))
    return overrides


@lru_cache(maxsize=1)
def get_settings(config_path: str | Path | None = None) -> Settings:
    """获取配置单例（首次调用会自动 ``ensure_dirs``）。

    Args:
        config_path: 可选的用户级配置路径。

    Returns:
        :class:`Settings` 单例。
    """
    settings = Settings.load(config_path)
    try:
        settings.ensure_dirs()
    except OSError:  # pragma: no cover - 只读环境
        pass
    return settings


def reload_settings(config_path: str | Path | None = None) -> Settings:
    """清空缓存并重新加载配置（Web 设置页热更新用）。

    Args:
        config_path: 可选的用户级配置路径。

    Returns:
        新的 :class:`Settings` 实例。
    """
    get_settings.cache_clear()
    return get_settings(config_path)


def ensure_user_config(config_path: str | Path | None = None) -> Path | None:
    """首次运行时把 ``config/config.example.yaml`` 复制为 ``data/config.yaml``。

    Args:
        config_path: 目标路径，缺省 ``<项目根>/data/config.yaml``。

    Returns:
        已存在的用户级配置文件路径；无法写入时返回 None。
    """
    target = Path(config_path) if config_path else PROJECT_ROOT / "data" / "config.yaml"
    if target.is_file():
        return target
    # 模板是**只读资源**：打包后位于 sys._MEIPASS，而不是 exe 同级。
    # 两个位置都找一遍，否则 exe 首次运行会静默拿不到默认配置。
    example_candidates = [
        PROJECT_ROOT / "config" / "config.example.yaml",
        PACKAGE_ROOT / "config" / "config.example.yaml",
        PACKAGE_ROOT / "config.example.yaml",
    ]
    example = next((p for p in example_candidates if p.is_file()), None)
    if example is None:
        return None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError:  # pragma: no cover
        return None
    return target
