"""站点管理子命令：``add`` / ``remove`` / ``list`` / ``import``。

四条命令同时挂在顶层（``sgm add ...``）与 ``sgm site`` 分组（``sgm site add ...``）下，方便记忆。
所有操作都走 :class:`SiteService`，因此与 Web 行为完全一致且都会写审计日志（``actor='cli'``）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from site_group_monitor.cli.output import echo_error, echo_ok, echo_warn
from site_group_monitor.web.deps import get_container


def _services() -> Any:
    """获取服务容器单例。

    Returns:
        服务容器对象。
    """
    return get_container()


@click.command("add", help="新增站点（幂等）")
@click.argument("domain", required=True)
@click.option("--group", default=None, help="分组名")
@click.option("--tags", default="", help="标签，多个用 | 分隔")
@click.option("--root-url", default="", help="覆盖首页 URL")
@click.option("--name", default="", help="备注名")
@click.option("--notes", default="", help="备注")
def add(domain: str, group: str | None, tags: str, root_url: str, name: str, notes: str) -> None:
    """新增站点。"""
    container = _services()
    tag_list = [item.strip() for item in str(tags).split("|") if item.strip()]
    try:
        site = container.sites.add(
            domain, group=group, tags=tag_list, root_url=root_url, name=name, notes=notes,
            actor="cli",
        )
    except ValueError as exc:
        echo_error(str(exc))
        return
    echo_ok(f"已新增/更新站点：{site.domain}（id={site.id} 分组={site.group_name} 标签={','.join(site.tags) or '-'}）")


@click.command("remove", help="删除站点（级联清理明细/快照/事件）")
@click.argument("identifier", required=True)
def remove(identifier: str) -> None:
    """删除站点。"""
    container = _services()
    removed = container.sites.remove(identifier, actor="cli")
    if removed:
        echo_ok(f"已删除站点：{identifier}")
    else:
        echo_warn(f"未找到站点：{identifier}")


@click.command("list", help="列出站点（可筛选）")
@click.option("--group", default=None, help="分组过滤")
@click.option("--tag", default=None, help="标签过滤")
@click.option("--keyword", default=None, help="域名/备注模糊搜索")
@click.option("--status", "index_status", default=None, help="收录状态：indexed/not_indexed/unknown_need_manual_check")
@click.option("--enabled/--disabled", default=None, help="只看启用/停用")
@click.option("--page", default=1, show_default=True, help="页码")
@click.option("--size", default=50, show_default=True, help="每页条数")
def list_sites(group: str | None, tag: str | None, keyword: str | None, index_status: str | None,
               enabled: bool | None, page: int, size: int) -> None:
    """列出站点表格。"""
    container = _services()
    items, total = container.sites.list(
        group=group, tag=tag, keyword=keyword, index_status=index_status, enabled=enabled,
        page=page, size=size,
    )
    _render_table(items, total, page, size)


@click.command("import", help="批量导入站点（CSV/JSON/纯文本）")
@click.argument("source", required=True)
@click.option("--format", "fmt", default="auto",
              type=click.Choice(["auto", "csv", "json", "text"]), show_default=True)
@click.option("--file/--stdin", "from_file", default=True, help="source 是文件路径还是直接文本")
@click.option("--group", default=None, help="覆盖分组名")
def import_sites(source: str, fmt: str, from_file: bool, group: str | None) -> None:
    """批量导入站点。"""
    container = _services()
    if from_file and not Path(source).is_file():
        echo_error(f"文件不存在：{source}")
        return
    try:
        summary = container.sites.import_many(
            source, fmt=fmt, from_file=from_file, group=group, actor="cli"
        )
    except Exception as exc:  # noqa: BLE001 - CLI 兜底
        echo_error(f"导入失败：{exc}")
        return
    echo_ok(
        f"导入完成：新增 {summary.added} / 更新 {summary.updated} / "
        f"重复跳过 {summary.skipped} / 非法 {len(summary.invalid)}"
    )
    if summary.invalid:
        for row in summary.invalid[:20]:
            echo_warn(f"非法行 {row.get('line_no')}: {row.get('raw')} —— {row.get('reason')}")


@click.group("site", help="站点管理（等价于顶层 add/remove/list/import）")
def site_group() -> None:
    """站点管理命令组。"""


site_group.add_command(add)
site_group.add_command(remove)
site_group.add_command(list_sites)
site_group.add_command(import_sites)


@click.command("groups", help="查看分组统计")
def groups() -> None:
    """输出分组统计。"""
    container = _services()
    data = container.sites.groups()
    if not data:
        echo_warn("暂无站点分组")
        return
    for item in data:
        click.echo(f"{item['group_name']}\t{item['count']}")


def _render_table(items: list[Any], total: int, page: int, size: int) -> None:
    """用 rich 渲染站点表格。

    Args:
        items: 站点列表。
        total: 总数。
        page: 页码。
        size: 每页条数。
    """
    try:
        from rich.console import Console
        from rich.table import Table
    except Exception:  # noqa: BLE001 - 无 rich 时退化为纯文本
        for site in items:
            click.echo(f"{site.domain}\t{site.group_name}\t{site.index_status or '-'}")
        click.echo(f"-- 共 {total} 条，第 {page} 页 / 每页 {size}")
        return

    table = Table(title=f"站点列表（共 {total} 条，第 {page} 页）")
    table.add_column("域名", style="cyan", no_wrap=True)
    table.add_column("分组")
    table.add_column("标签")
    table.add_column("收录", justify="center")
    table.add_column("状态码", justify="right")
    table.add_column("响应ms", justify="right")
    table.add_column("上次检测")
    status_map = {"indexed": "[green]已收录[/green]",
                  "not_indexed": "[red]未收录[/red]",
                  "unknown_need_manual_check": "[yellow]查询失败[/yellow]"}
    for site in items:
        table.add_row(
            site.domain,
            site.group_name or "-",
            "|".join(site.tags) or "-",
            status_map.get(str(site.index_status), "-"),
            str(site.http_status if site.http_status is not None else "-"),
            str(site.response_ms if site.response_ms is not None else "-"),
            str(site.checked_at or site.last_checked_at or "-"),
        )
    Console().print(table)


def _json(items: list[Any]) -> str:  # pragma: no cover - 预留给脚本化消费
    """把站点列表序列化为 JSON。

    Args:
        items: 站点列表。

    Returns:
        JSON 字符串。
    """
    return json.dumps([item.model_dump() for item in items], ensure_ascii=False, indent=2)
