"""仪表盘路由集合。"""

from __future__ import annotations

from site_group_monitor.web.routers import overview, reports, sites, system

__all__: list[str] = ["overview", "sites", "reports", "system"]
