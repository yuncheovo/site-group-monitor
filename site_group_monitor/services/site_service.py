"""站点管理服务（R-001 / R-018）。

CLI 与 Web 共用；支持新增、删除、查询、批量导入（幂等去重）与分组/标签统计。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from site_group_monitor.models.entities import ImportSummary, Site
from site_group_monitor.models.enums import SiteHealth
from site_group_monitor.services.audit_service import AuditService
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.utils.importer import SiteImporter
from site_group_monitor.utils.netutil import normalize_domain
from site_group_monitor.utils.timeutil import utcnow_iso


class SiteService:
    """站点 CRUD 与导入编排。"""

    def __init__(self, repos: Repositories, audit: AuditService, settings: Any | None = None) -> None:
        """构造站点服务。

        Args:
            repos: 仓储容器。
            audit: 审计服务（**位置参数，必须是 AuditService 实例**）。
            settings: 配置对象（用于导入默认参数）。

        Raises:
            TypeError: ``audit`` 不是 :class:`AuditService` 时立即报错。

        Note:
            这里做一次类型校验而不是静默接受：历史上把 ``settings`` 误传到
            ``audit`` 位置时，错误直到 ``import_many`` 调 ``self.audit.log()``
            才以 ``'Settings' object has no attribute 'log'`` 的形式暴露，
            定位成本高。构造期直接失败更省事。
        """
        if not isinstance(audit, AuditService):
            raise TypeError(
                f"SiteService 第二个参数必须是 AuditService，收到 {type(audit).__name__}"
                "（是否把 settings 传到了 audit 位置？）"
            )
        self.repos = repos
        self.audit = audit
        self.settings = settings

    # ---------------------------------------------------------------- 单站
    def add(
        self,
        domain: str,
        *,
        group: str | None = None,
        tags: Sequence[str] | None = None,
        root_url: str = "",
        name: str = "",
        notes: str = "",
        actor: str = "cli",
    ) -> Site:
        """新增（或幂等覆盖）一个站点。

        Args:
            domain: 原始域名输入，内部自动归一化。
            group: 分组名。
            tags: 标签列表。
            root_url: 首页 URL 覆盖。
            name: 备注名。
            notes: 备注。
            actor: 操作来源（写审计用）。

        Returns:
            写入后的站点实体。

        Raises:
            ValueError: 域名非法。
        """
        normalized = normalize_domain(domain)
        if not normalized:
            raise ValueError(f"非法域名: {domain!r}")
        now = utcnow_iso()
        site = Site(
            domain=normalized,
            root_url=root_url,
            name=name,
            group_name=group or self._default_group(),
            tags=list(tags or []),
            notes=notes,
            created_at=now,
            updated_at=now,
        )
        site_id = self.repos.sites.upsert_by_domain(site)
        saved = self.repos.sites.get_by_id(site_id) or site
        self.audit.log(actor, "site.add", normalized, {"group": saved.group_name, "tags": saved.tags})
        return saved

    def remove(self, identifier: str, *, actor: str = "cli") -> bool:
        """按域名或 id 删除站点。

        Args:
            identifier: domain 或 id 字符串。
            actor: 操作来源。

        Returns:
            是否删除成功。
        """
        target = str(identifier).strip()
        if not target:
            return False
        removed = self.repos.sites.remove(target)
        self.audit.log(actor, "site.remove", target, {"removed": removed})
        return removed

    def get(self, identifier: str | int) -> Site | None:
        """按域名或 id 查询站点。

        Args:
            identifier: domain 或 id。

        Returns:
            站点实体或 None。
        """
        if isinstance(identifier, int):
            return self.repos.sites.get_by_id(identifier)
        text = str(identifier)
        if text.isdigit():
            found = self.repos.sites.get_by_id(int(text))
            if found:
                return found
        return self.repos.sites.get_by_domain(normalize_domain(text) or text)

    def update_tags(self, identifier: str | int, tags: Sequence[str], *, actor: str = "web") -> bool:
        """更新站点标签（R-018）。

        Args:
            identifier: domain 或 id。
            tags: 新标签列表。
            actor: 操作来源。

        Returns:
            是否更新成功。
        """
        site = self.get(identifier)
        if site is None or site.id is None:
            return False
        site.tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        site.updated_at = utcnow_iso()
        updated = self.repos.sites.update(site)
        self.audit.log(actor, "site.update_tags", site.domain, {"tags": site.tags})
        return updated

    def set_enabled(self, identifier: str | int, enabled: bool, *, actor: str = "web") -> bool:
        """启停站点。

        Args:
            identifier: domain 或 id。
            enabled: 是否启用。
            actor: 操作来源。

        Returns:
            是否更新成功。
        """
        site = self.get(identifier)
        if site is None or site.id is None:
            return False
        result = self.repos.sites.set_enabled(site.id, enabled)
        self.audit.log(actor, "site.set_enabled", site.domain, {"enabled": bool(enabled)})
        return result

    def list(
        self,
        *,
        group: str | None = None,
        tag: str | None = None,
        keyword: str | None = None,
        enabled: bool | None = None,
        index_status: str | None = None,
        health: str | None = None,
        page: int = 1,
        size: int = 50,
        sort: str = "domain",
        desc: bool = False,
    ) -> tuple[list[Site], int]:
        """分页查询站点（带最新检测结果左连）。

        Returns:
            ``(站点列表, 总数)``。
        """
        ssl_warn_days = 15
        if self.settings is not None:
            ssl_warn_days = int(getattr(self.settings.alert, "ssl_warn_days", 15))
        return self.repos.sites.list(
            group=group,
            tag=tag,
            keyword=keyword,
            enabled=enabled,
            index_status=index_status,
            health=health,
            page=page,
            size=size,
            sort=sort,
            desc=desc,
            ssl_warn_days=ssl_warn_days,
        )

    def groups(self) -> list[dict[str, Any]]:
        """返回分组统计列表。"""
        return self.repos.sites.list_groups()

    def count_enabled(self) -> int:
        """启用中的站点数。"""
        return self.repos.sites.count_enabled()

    # ---------------------------------------------------------------- 导入
    def import_many(
        self,
        payload_or_path: str,
        *,
        fmt: str = "auto",
        from_file: bool = False,
        group: str | None = None,
        actor: str = "cli",
    ) -> ImportSummary:
        """批量导入站点（幂等去重，500 行 < 10s）。

        Args:
            payload_or_path: 文本内容或文件路径。
            fmt: ``auto`` / ``csv`` / ``json`` / ``text``。
            from_file: True 表示参数视为文件路径。
            group: 覆盖分组名。
            actor: 操作来源。

        Returns:
            :class:`ImportSummary` 汇总。
        """
        if from_file:
            payload = Path(payload_or_path).read_text(encoding="utf-8", errors="replace")
        else:
            payload = payload_or_path
        importer = self._make_importer()
        outcome = importer.parse(payload, fmt)

        summary = ImportSummary(total_rows=len(outcome.sites) + len(outcome.invalid))
        summary.duplicates = outcome.duplicates
        summary.invalid = [
            {"line_no": row.line_no, "raw": row.raw, "reason": row.reason} for row in outcome.invalid
        ]

        # 先按域名去重（同一批里的重复行只保留最后一条），再一次性批量写入。
        # 旧实现逐行 get_by_domain + upsert，每行 3 次 connect，500 行要 60s+。
        unique: dict[str, Site] = {}
        for site in outcome.sites:
            if group:
                site.group_name = group
            unique[site.domain] = site

        added, updated = self.repos.sites.bulk_upsert_by_domain(list(unique.values()))
        summary.added = added
        summary.updated = updated
        summary.skipped = len(outcome.duplicates)
        self.audit.log(
            actor,
            "site.import",
            "",
            {
                "added": summary.added,
                "updated": summary.updated,
                "duplicates": len(summary.duplicates),
                "invalid": len(summary.invalid),
                "fmt": fmt,
            },
        )
        return summary

    def _make_importer(self) -> SiteImporter:
        """按配置构造导入器。"""
        if self.settings is None:
            return SiteImporter()
        cfg = getattr(self.settings, "import_cfg", None)
        return SiteImporter(
            default_group=getattr(cfg, "default_group", "default") if cfg else "default",
            tag_separator=getattr(cfg, "tag_separator", "|") if cfg else "|",
            csv_delimiter=getattr(cfg, "csv_delimiter", ",") if cfg else ",",
        )

    def _default_group(self) -> str:
        """默认分组名。"""
        if self.settings is None:
            return "default"
        cfg = getattr(self.settings, "import_cfg", None)
        return getattr(cfg, "default_group", "default") if cfg else "default"


def health_of(site: Site, ssl_warn_days: int = 15) -> str:
    """根据列表视图字段给出站点健康度标签（UI 表格高亮用）。

    Args:
        site: 带最新结果的站点实体。
        ssl_warn_days: SSL 预警阈值。

    Returns:
        SiteHealth 枚举值字符串。
    """
    if site.index_status == "unknown_need_manual_check" or site.http_status is None:
        return SiteHealth.UNKNOWN.value
    if site.http_status >= 400:
        return SiteHealth.ABNORMAL.value
    warn = (site.response_ms or 0) > 3000
    return SiteHealth.WARN.value if warn else SiteHealth.HEALTHY.value
