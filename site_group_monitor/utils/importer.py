"""站点批量导入解析（CSV / JSON / 纯文本）。

产出统一的 ``(valid_sites, invalid_rows)`` 结构，逐行做域名归一化与内存去重，
**不访问数据库**（库级去重由 ``SiteService.import_many`` 负责），保证 500 行解析 < 2s。
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from site_group_monitor.models.entities import Site
from site_group_monitor.utils.netutil import clean_text, normalize_domain
from site_group_monitor.utils.timeutil import utcnow_iso

ImportFormat = str  # "csv" | "json" | "text" | "auto"


@dataclass
class InvalidRow:
    """一行非法输入的描述信息。"""

    line_no: int
    raw: str
    reason: str


@dataclass
class ParseOutcome:
    """导入文本的解析结果。

    Attributes:
        sites: 解析成功的站点列表（已按域名去内存重）。
        invalid: 非法行列表。
        duplicates: 输入内部重复的域名列表。
    """

    sites: list[Site] = field(default_factory=list)
    invalid: list[InvalidRow] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)


class SiteImporter:
    """把 CSV / JSON / 纯文本解析为站点列表。

    Args:
        default_group: 缺省分组名。
        tag_separator: 标签分隔符（CSV 的 tags 列）。
        csv_delimiter: CSV 分隔符。
    """

    _CSV_HEADERS = ("domain", "root_url", "name", "group", "tags", "notes", "enabled")

    def __init__(
        self,
        default_group: str = "default",
        tag_separator: str = "|",
        csv_delimiter: str = ",",
    ) -> None:
        self.default_group = default_group or "default"
        self.tag_separator = tag_separator or "|"
        self.csv_delimiter = csv_delimiter or ","

    def detect_format(self, payload: str) -> str:
        """自动识别文本格式。

        Args:
            payload: 原始文本内容。

        Returns:
            ``json`` / ``csv`` / ``text`` 之一。
        """
        stripped = payload.lstrip()
        if stripped.startswith("[") or stripped.startswith("{"):
            return "json"
        first_line = stripped.splitlines()[0] if stripped.splitlines() else ""
        if self.csv_delimiter in first_line and "domain" in first_line.lower():
            return "csv"
        return "text"

    def parse(self, payload: str, fmt: ImportFormat = "auto") -> ParseOutcome:
        """解析文本为站点列表。

        Args:
            payload: 文本内容或文件路径指向的内容。
            fmt: ``auto`` / ``csv`` / ``json`` / ``text``。

        Returns:
            :class:`ParseOutcome` 解析结果。
        """
        if fmt == "auto":
            fmt = self.detect_format(payload)
        if fmt == "json":
            outcome = self._parse_json(payload)
        elif fmt == "csv":
            outcome = self._parse_csv(payload)
        else:
            outcome = self._parse_text(payload)
        return self._dedup(outcome)

    def parse_lines(self, lines: Iterable[str], fmt: ImportFormat = "text") -> ParseOutcome:
        """按行迭代解析（便于流式读取超大文件）。

        Args:
            lines: 文本行迭代器。
            fmt: 格式。

        Returns:
            :class:`ParseOutcome` 解析结果。
        """
        return self.parse("\n".join(lines), fmt)

    def _dedup(self, outcome: ParseOutcome) -> ParseOutcome:
        """按域名做内存去重，保留首次出现的记录。"""
        seen: set[str] = set()
        unique: list[Site] = []
        for site in outcome.sites:
            if site.domain in seen:
                if site.domain not in outcome.duplicates:
                    outcome.duplicates.append(site.domain)
                continue
            seen.add(site.domain)
            unique.append(site)
        outcome.sites = unique
        return outcome

    def _make_site(self, raw: str, **extra: Any) -> Site:
        """用统一的默认值构造站点实体。"""
        now = utcnow_iso()
        payload = {
            "domain": normalize_domain(raw),
            "root_url": clean_text(extra.get("root_url", "")),
            "name": clean_text(extra.get("name", "")),
            "group_name": clean_text(extra.get("group", "")) or self.default_group,
            "tags": self._split_tags(extra.get("tags", "")),
            "enabled": bool(extra.get("enabled", True)),
            "notes": clean_text(extra.get("notes", "")),
            "created_at": now,
            "updated_at": now,
        }
        return Site(**payload)

    def _split_tags(self, value: Any) -> list[str]:
        """把 tags 列的多种写法统一为列表。

        Args:
            value: 可能是列表、``|`` 分隔字符串或 JSON 数组字符串。

        Returns:
            清洗后的标签列表。
        """
        items: list[str] = []
        if isinstance(value, (list, tuple, set)):
            items = [str(item) for item in value]
        elif value:
            text = str(value).strip()
            if text.startswith("[") and text.endswith("]"):
                try:
                    loaded = json.loads(text)
                    items = [str(item) for item in loaded]
                except json.JSONDecodeError:
                    items = text.strip("[]").split(",")
            else:
                items = text.split(self.tag_separator)
        result: list[str] = []
        for item in items:
            tag = clean_text(item)
            if tag and tag not in result:
                result.append(tag)
        return result

    def _parse_csv(self, payload: str) -> ParseOutcome:
        """解析 CSV（支持表头 domain,root_url,name,group,tags,notes,enabled）。"""
        outcome = ParseOutcome()
        reader = csv.reader(io.StringIO(payload), delimiter=self.csv_delimiter)
        headers: Sequence[str] | None = None
        for index, row in enumerate(reader, start=1):
            if not row or all(not str(cell).strip() for cell in row):
                continue
            first = str(row[0]).strip().lower()
            if index == 1 and first in ("domain", "url", "站点", "域名"):
                headers = [str(cell).strip().lower() for cell in row]
                continue
            if headers is None:  # 无表头：仅把第一列当域名
                row_map = {"domain": row[0]}
            else:
                row_map = dict(zip(headers, row))
            raw_domain = str(row_map.get("domain", row_map.get("url", ""))).strip()
            if not raw_domain:
                outcome.invalid.append(InvalidRow(index, self.csv_delimiter.join(row), "缺少 domain"))
                continue
            enabled_raw = str(row_map.get("enabled", "1")).strip().lower()
            site = self._make_site(
                raw_domain,
                root_url=row_map.get("root_url", ""),
                name=row_map.get("name", row_map.get("备注", "")),
                group=row_map.get("group", row_map.get("group_name", "")),
                tags=row_map.get("tags", ""),
                notes=row_map.get("notes", ""),
                enabled=enabled_raw not in ("0", "false", "no", "off", "禁用"),
            )
            if not site.domain:
                outcome.invalid.append(
                    InvalidRow(index, self.csv_delimiter.join(row), f"域名非法: {raw_domain}")
                )
                continue
            outcome.sites.append(site)
        return outcome

    def _parse_json(self, payload: str) -> ParseOutcome:
        """解析 JSON 数组（元素可为字符串或对象）。"""
        outcome = ParseOutcome()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            outcome.invalid.append(InvalidRow(1, payload[:200], f"JSON 解析失败: {exc.msg}"))
            return outcome
        records = data if isinstance(data, list) else [data]
        for index, record in enumerate(records, start=1):
            if isinstance(record, str):
                site = self._make_site(record)
            elif isinstance(record, dict):
                site = self._make_site(
                    str(record.get("domain", record.get("url", ""))),
                    root_url=record.get("root_url", ""),
                    name=record.get("name", ""),
                    group=record.get("group", record.get("group_name", "")),
                    tags=record.get("tags", ""),
                    notes=record.get("notes", ""),
                    enabled=bool(record.get("enabled", True)),
                )
            else:
                outcome.invalid.append(InvalidRow(index, json.dumps(record, ensure_ascii=False)[:200], "元素类型非法"))
                continue
            if not site.domain:
                outcome.invalid.append(InvalidRow(index, json.dumps(record, ensure_ascii=False)[:200], "域名非法"))
                continue
            outcome.sites.append(site)
        return outcome

    def _parse_text(self, payload: str) -> ParseOutcome:
        """解析纯文本：一行一个域名，``#`` 开头为注释。"""
        outcome = ParseOutcome()
        for index, line in enumerate(payload.splitlines(), start=1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            site = self._make_site(text)
            if not site.domain:
                outcome.invalid.append(InvalidRow(index, text, f"域名非法: {text}"))
                continue
            outcome.sites.append(site)
        return outcome


def default_importer() -> SiteImporter:
    """构造使用默认配置的导入器（给 CLI 快速调用）。

    Returns:
        :class:`SiteImporter` 实例。
    """
    return SiteImporter()
