"""操作审计服务（R-016）。

CLI / Web / Scheduler 三条通道的「同一操作」都会经这里留痕：
``actor`` 分别是 ``cli`` / ``web`` / ``scheduler``，保证 auditor observable 一致性。
"""

from __future__ import annotations

from typing import Any

from site_group_monitor.storage.repository import Repositories


class AuditService:
    """审计写入与查询服务。"""

    def __init__(self, repos: Repositories) -> None:
        """构造审计服务。

        Args:
            repos: 仓储容器。
        """
        self.repos = repos

    def log(
        self,
        actor: str,
        action: str,
        target: str = "",
        detail: dict[str, Any] | None = None,
        level: str = "info",
        request_id: str = "",
    ) -> int:
        """写一条审计日志（内部异常不外抛，避免影响主流程）。

        Args:
            actor: 操作来源。
            action: 动作名。
            target: 作用对象。
            detail: 结构化详情。
            level: 级别。
            request_id: 请求号。

        Returns:
            审计记录 id；失败返回 0。
        """
        try:
            return self.repos.audit.log(actor, action, target, detail, level, request_id)
        except Exception:  # noqa: BLE001 - 审计失败不得阻断业务
            return 0

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        keyword: str | None = None,
        since_iso: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        """查询审计日志。

        Returns:
            ``(记录列表, 总数)``。
        """
        return self.repos.audit.query(
            actor=actor, action=action, keyword=keyword, since_iso=since_iso, page=page, size=size
        )

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """返回最近 N 条审计。

        Args:
            limit: 条数。
        """
        records, _ = self.repos.audit.query(page=1, size=limit)
        return records
