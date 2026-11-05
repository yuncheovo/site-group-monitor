"""site_group_monitor 包初始化。

导出包版本号，供 CLI `--version` 与 FastAPI 健康检查使用。

Example:
    >>> from site_group_monitor import __version__
    >>> isinstance(__version__, str)
    True
"""

from __future__ import annotations

__version__ = "0.1.6"
__all__ = ["__version__"]
