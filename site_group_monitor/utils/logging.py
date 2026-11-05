"""日志初始化与获取。

约定（对应架构文档 §7.3）：
    * 统一通过 ``get_logger(name)`` 获取 logger；
    * 同时输出到终端（rich）与 ``logs/sgm.log``（RotatingFileHandler 滚动）；
    * 禁止在业务代码里使用 ``print``，终端输出一律走 rich。
"""

from __future__ import annotations

import logging
import logging.config
import logging.handlers
from pathlib import Path
from typing import Any

import yaml

DEFAULT_LEVEL = "INFO"
_INITIALIZED = False
_LOG_DIR = Path("logs")


def _package_root() -> Path:
    """返回**可写**的根目录（包目录的上一级）。

    打包成 exe 后必须是 exe 所在目录：日志要写在那里，
    写到 PyInstaller 的临时解压目录（``sys._MEIPASS``）会随进程退出被清掉。

    Returns:
        项目根目录绝对路径。
    """
    from site_group_monitor.config.settings import PROJECT_ROOT

    return PROJECT_ROOT


def _resource_root() -> Path:
    """返回**只读资源**根目录（打包后为 ``sys._MEIPASS``）。

    Returns:
        资源根目录绝对路径。
    """
    from site_group_monitor.config.settings import PACKAGE_ROOT

    return PACKAGE_ROOT


def _default_dict_config(level: str, log_dir: Path) -> dict[str, Any]:
    """构造不依赖 rich 的最小可用 dictConfig，作为 YAML 缺失时的兜底。

    Args:
        level: 日志级别名。
        log_dir: 日志目录绝对路径。

    Returns:
        logging.dictConfig 可用的配置字典。
    """
    fmt = "[%(asctime)s][%(levelname)s][%(name)s] %(message)s"
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {"format": fmt, "datefmt": "%Y-%m-%d %H:%M:%S"},
            "brief": {"format": "%(message)s"},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": level,
                "formatter": "brief",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": level,
                "formatter": "standard",
                "filename": str(log_dir / "sgm.log"),
                "maxBytes": 10 * 1024 * 1024,
                "backupCount": 5,
                "encoding": "utf-8",
            },
        },
        "root": {"level": level, "handlers": ["console", "file"]},
        "loggers": {
            "httpx": {"level": "WARNING", "propagate": True},
            "httpcore": {"level": "WARNING", "propagate": True},
            "apscheduler": {"level": "WARNING", "propagate": True},
        },
    }


def _render_config(raw: dict[str, Any], level: str, log_dir: Path) -> dict[str, Any]:
    """替换 logging.yaml 中的 ``{level}`` 与 ``{log_dir}`` 占位符。

    Args:
        raw: 原始 YAML 配置字典。
        level: 日志级别名。
        log_dir: 日志目录绝对路径。

    Returns:
        替换完成后的配置字典（已处理 handler filename 与 handler level）。
    """

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, str):
            return node.replace("{level}", level).replace("{log_dir}", str(log_dir))
        return node

    return walk(raw)


def setup_logging(level: str | None = None, log_dir: str | Path | None = None) -> logging.Logger:
    """初始化全局日志配置（幂等）。

    Args:
        level: 日志级别，默认读取配置文件的 ``app.log_level``，再兜底为 INFO。
        log_dir: 日志目录，默认 ``<项目根>/logs``。

    Returns:
        已配置好的 root logger。
    """
    global _INITIALIZED, _LOG_DIR

    if log_dir is None:
        log_dir = _package_root() / "logs"
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    _LOG_DIR = log_dir

    if level is None:
        level = _configured_level() or DEFAULT_LEVEL
    level = str(level).upper()

    try:
        # logging.yaml 是只读资源：打包后在 sys._MEIPASS，源码运行时在项目根
        config_path = _resource_root() / "config" / "logging.yaml"
        if not config_path.is_file():
            config_path = _package_root() / "config" / "logging.yaml"
        if config_path.is_file():
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            dict_config = _render_config(raw, level, log_dir)
        else:  # pragma: no cover - 模板文件缺失时的兜底
            dict_config = _default_dict_config(level, log_dir)
        logging.config.dictConfig(dict_config)
    except Exception:  # noqa: BLE001 - 日志初始化失败绝不能拖垮主流程
        logging.basicConfig(level=getattr(logging, level, logging.INFO))
    _INITIALIZED = True
    return logging.getLogger("sgm")


def _configured_level() -> str | None:
    """从用户配置里读取日志级别（避免与 settings 形成循环依赖，做惰性导入）。

    Returns:
        日志级别字符串；读取失败时返回 None。
    """
    try:  # pragma: no cover - 依赖运行环境
        from site_group_monitor.config.settings import get_settings

        return str(get_settings().app.log_level).upper()
    except Exception:
        return None


def get_logger(name: str = "sgm") -> logging.Logger:
    """获取 logger，首次调用会自动完成日志初始化。

    Args:
        name: logger 名称，建议使用模块名，或以 ``sgm.`` 前缀开头。

    Returns:
        ``logging.Logger`` 实例。
    """
    if not _INITIALIZED:
        setup_logging()
    if not name.startswith("sgm"):
        name = f"sgm.{name}"
    return logging.getLogger(name)


def get_log_dir() -> Path:
    """返回当前生效的日志目录路径。

    Returns:
        日志目录绝对路径。
    """
    return _LOG_DIR
