"""首启引导命令 ``sgm init``。

exe 首启时用户面对的是一个空目录：没有数据目录、没有库、没有配置。
本命令把「第一次跑起来」这件事变成一条命令，并明确告诉用户产出了什么、
接下来该做什么。

设计要点：
    * **幂等**——重复执行不会覆盖已有配置、不会清空已有数据；
    * **不依赖网络**——只做本地目录 / 库 / 配置准备；
    * **路径自适应**——源码运行时落在项目根，打包后落在 exe 同级。
"""

from __future__ import annotations

from pathlib import Path

import click

from site_group_monitor.cli.output import echo_ok, echo_warn
from site_group_monitor.config.settings import Settings, ensure_user_config
from site_group_monitor.storage.database import Database
from site_group_monitor.storage.repository import Repositories


def _describe(path: Path) -> str:
    """把路径压成一行可读文本。

    Args:
        path: 目标路径。

    Returns:
        路径字符串。
    """
    try:
        return str(path.resolve())
    except OSError:  # pragma: no cover - 极端路径
        return str(path)


@click.command("init", help="初始化运行环境（建目录 / 建库 / 写配置）")
@click.option("--force-config", is_flag=True, help="强制用模板覆盖已有配置文件")
@click.pass_context
def init(ctx: click.Context, force_config: bool) -> None:
    """初始化运行环境。

    会创建数据目录、初始化 SQLite 库表、写出用户级配置文件，
    并打印各产物路径与后续建议命令。**可重复执行，幂等。**
    """
    settings: Settings | None = ctx.obj.get("settings") if ctx.obj else None
    if settings is None:
        from site_group_monitor.config.settings import get_settings

        settings = get_settings()

    created_dirs: list[Path] = []
    for directory in (
        settings.db_path.parent,
        settings.reports_dir,
        settings.evidence_dir,
        settings.backups_dir,
        settings.logs_dir,
    ):
        if not directory.exists():
            created_dirs.append(directory)
        directory.mkdir(parents=True, exist_ok=True)

    # --- 建库（7 张表，幂等）---
    db_existed = settings.db_path.is_file()
    database = Database(settings.db_path)
    try:
        database.init_db()
        repos = Repositories(database)
        table_names = sorted(database.list_tables())
        site_count = repos.sites.count_all()
    finally:
        database.close_all()

    # --- 写配置 ---
    config_target: Path | None = None
    if force_config:
        from site_group_monitor.config.settings import PACKAGE_ROOT, PROJECT_ROOT

        config_target = PROJECT_ROOT / "data" / "config.yaml"
        candidates = [
            PROJECT_ROOT / "config" / "config.example.yaml",
            PACKAGE_ROOT / "config" / "config.example.yaml",
            PACKAGE_ROOT / "config.example.yaml",
        ]
        example = next((p for p in candidates if p.is_file()), None)
        if example is not None:
            try:
                config_target.parent.mkdir(parents=True, exist_ok=True)
                config_target.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                config_target = None
    else:
        found = ensure_user_config()
        config_target = Path(found) if found is not None else None

    click.echo()
    click.secho("初始化完成", fg="green", bold=True)
    click.echo()
    click.echo("产物位置：")
    click.echo(f"  数据目录    {_describe(settings.db_path.parent)}"
               + ("（新建）" if created_dirs else ""))
    click.echo(f"  报告目录    {_describe(settings.reports_dir)}")
    click.echo(f"  证据目录    {_describe(settings.evidence_dir)}")
    click.echo(f"  备份目录    {_describe(settings.backups_dir)}")
    click.echo(f"  日志目录    {_describe(settings.logs_dir)}")
    click.echo(f"  数据库      {_describe(settings.db_path)}"
               + ("（新建）" if not db_existed else "（已存在，已确保表结构）"))
    if config_target is not None:
        click.echo(f"  配置文件    {_describe(config_target)}")
    if table_names:
        click.echo(f"  数据表      {len(table_names)} 张：{', '.join(table_names)}")
    click.echo(f"  现有站点    {site_count} 个")
    click.echo()
    click.secho("接下来：", fg="cyan", bold=True)
    click.echo("  1) 导入站点：  sgm import sites.csv      （CSV/JSON/纯文本均可）")
    click.echo("  2) 跑一轮检测：sgm check --with-report")
    click.echo("  3) 起仪表盘：  sgm serve                  （浏览器打开 http://127.0.0.1:8765）")
    click.echo()
    if site_count == 0:
        echo_warn("还没有站点。批量监控前请先导入站点列表。")
    else:
        echo_ok("环境已就绪，可以直接跑检测。")
    click.echo()
