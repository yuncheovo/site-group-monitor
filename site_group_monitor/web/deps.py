"""Web 层依赖注入与服务容器。

约定（对应架构文档 §7.7）：
    * Web 与 CLI **共用同一 Service 层**，router 只做参数解析与结果渲染；
    * 同一操作在两条通道行为一致，且都会写 ``audit_logs``（``actor='web'``）；
    * 容器是进程内单例，CLI 可直接取用同一份实例（避免重复建连接）。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from site_group_monitor.config.settings import Settings, get_settings, reload_settings
from site_group_monitor.scheduler.scheduler import Scheduler
from site_group_monitor.services.alert_service import AlertService
from site_group_monitor.services.audit_service import AuditService
from site_group_monitor.services.check_service import CheckService
from site_group_monitor.services.diff_service import DiffService
from site_group_monitor.services.report_service import ReportService
from site_group_monitor.services.site_service import SiteService
from site_group_monitor.storage.database import Database
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.utils.logging import get_logger


class Container:
    """服务容器（进程内单例）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        """构造容器并初始化全部服务。

        Args:
            settings: 配置对象；缺省取当前单例。
        """
        self.settings: Settings = settings or get_settings()
        self.logger = get_logger("web")
        self.db = Database(
            self.settings.db_path,
            wal=self.settings.storage.wal,
            busy_timeout_ms=self.settings.storage.busy_timeout_ms,
        )
        self.db.init_db()
        self.repos = Repositories(self.db)
        self.audit = AuditService(self.repos)
        self.sites = SiteService(self.repos, self.audit, self.settings)
        self.check = CheckService(self.repos, self.settings, self.logger, self.audit)
        self.diff = DiffService(self.repos, self.settings, self.logger)
        self.report = ReportService(self.repos, self.settings, self.logger)
        self.alert = AlertService(self.repos, self.settings, self.logger)
        self.scheduler = Scheduler(
            {
                "check": self.check,
                "diff": self.diff,
                "report": self.report,
                "alert": self.alert,
                "audit": self.audit,
                "repos": self.repos,
            },
            self.settings,
            self.logger,
        )

    # ---------------------------------------------------------------- 生命周期
    def reload(self, patch: dict[str, Any] | None = None) -> Settings:
        """热更新配置：写回 ``data/config.yaml`` 并重建内存中的设置对象。

        Args:
            patch: 需要合并写入的配置补丁（嵌套字典）。

        Returns:
            新的 :class:`Settings`。
        """
        if patch:
            _merge_to_user_config(self.settings, patch)
        # 必须显式传入「当前实例」的配置文件路径：否则 reload 会退回默认的
        # PROJECT_ROOT/data/config.yaml，导致写入 A 处、读取 B 处，补丁静默失效。
        config_path = Path(self.settings.project_root) / "data" / "config.yaml"
        self.settings = reload_settings(config_path)
        self.db = Database(
            self.settings.db_path,
            wal=self.settings.storage.wal,
            busy_timeout_ms=self.settings.storage.busy_timeout_ms,
        )
        self.db.init_db()
        self.repos = Repositories(self.db)
        self.audit = AuditService(self.repos)
        self.sites = SiteService(self.repos, self.audit, self.settings)
        self.check = CheckService(self.repos, self.settings, self.logger, self.audit)
        self.diff = DiffService(self.repos, self.settings, self.logger)
        self.report = ReportService(self.repos, self.settings, self.logger)
        self.alert = AlertService(self.repos, self.settings, self.logger)
        self.scheduler.settings = self.settings
        self.scheduler.services.update(
            {
                "check": self.check,
                "diff": self.diff,
                "report": self.report,
                "alert": self.alert,
                "audit": self.audit,
                "repos": self.repos,
            }
        )
        return self.settings

    def shutdown(self) -> None:
        """释放资源（停止调度器）。"""
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


_container: Container | None = None
_lock = threading.Lock()


def build_container(settings: Settings | None = None) -> Container:
    """获取（必要时构造）全局容器单例。

    该函数**带参数**，仅供进程内普通调用使用（如 ``create_app``）。
    不要直接把它当作 FastAPI 依赖注入——带 pydantic 模型参数的依赖会被
    FastAPI 解析成请求体字段，导致每个 POST/PATCH 的 body 被多包一层
    ``{"payload": {...}, "settings": null}``。依赖注入请用 :func:`get_container`。

    Args:
        settings: 可选的配置对象（首次构造时用）。

    Returns:
        :class:`Container`。
    """
    global _container
    if _container is None:
        with _lock:
            if _container is None:
                _container = Container(settings)
    return _container


def get_container() -> Container:
    """FastAPI 依赖：取全局容器单例（**必须无参数**，见 :func:`build_container`）。

    Returns:
        :class:`Container`。
    """
    return build_container()


def reset_container() -> None:
    """重置容器（测试用）。"""
    global _container
    if _container is not None:
        _container.shutdown()
    _container = None


def _merge_to_user_config(settings: Settings, patch: dict[str, Any]) -> None:
    """把补丁合并写入用户级 ``data/config.yaml``。

    Args:
        settings: 当前配置对象。
        patch: 配置补丁字典。
    """
    import yaml

    target = settings.project_root / "data" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if target.is_file():
        loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            current = loaded

    def merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = merge(base[key], value)
            else:
                base[key] = value
        return base

    merged = merge(current, patch)
    target.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


# ------------------------------------------------------------------ FastAPI 依赖
def get_settings_dep() -> Settings:
    """FastAPI 依赖：当前配置。"""
    return get_container().settings


def get_repos_dep() -> Repositories:
    """FastAPI 依赖：仓储容器。"""
    return get_container().repos


def get_services_dep() -> Container:
    """FastAPI 依赖：服务容器。"""
    return get_container()
