"""Click CLI 主入口 ``sgm``。

全局选项：
    * ``--config PATH``：指定用户级配置文件；
    * ``--verbose / -v``：提高终端日志级别；
    * ``--db PATH``：覆盖 SQLite 路径（写环境变量 ``SGM_STORAGE__DB_PATH``，保持「配置唯一入口」原则）；
    * ``--quiet / -q``：静默模式（不出进度条）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import click

from site_group_monitor import __version__
from site_group_monitor.cli import init_cmds, proxy_cmds, run_cmds, site_cmds
from site_group_monitor.config.settings import Settings, ensure_user_config, get_settings, reload_settings
from site_group_monitor.utils.logging import get_logger, setup_logging


def _force_utf8_output() -> None:
    """把 stdout/stderr 强制切到 UTF-8，保证中文在管道与脚本里不乱码。

    Windows 控制台默认代码页是 GBK(936)，中文输出被脚本捕获后必然乱码。
    这里同时做两件事：
        * ``SetConsoleOutputCP(65001)`` —— 让 cmd/PowerShell 直接显示 UTF-8；
        * ``sys.stdout.reconfigure(encoding="utf-8")`` —— 让重定向到文件/管道的
          字节流也是 UTF-8。

    全程包在 try/except 里：非 Windows 环境、终端不支持重配等情况必须
    **静默降级**，绝不能让 CLI 直接崩掉。
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001 - 环境不支持时忽略
            pass
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 不支持 reconfigure 的流忽略
            pass


@click.group(invoke_without_command=True, help="站群百度收录 / TDK / 健康状态监控工具（双击 exe 直接进入图形界面）")
@click.option("--config", "config_path", default=None, help="指定用户级配置文件路径")
@click.option("--db", "db_path", default=None, help="覆盖 SQLite 数据库路径")
@click.option("-v", "--verbose", is_flag=True, help="输出 DEBUG 级日志")
@click.option("-q", "--quiet", is_flag=True, default=False, help="静默模式（不出进度条）")
@click.version_option(version=__version__, prog_name="站群监控")
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, db_path: str | None, verbose: bool,
        quiet: bool) -> None:
    """CLI 主命令组：完成全局初始化并把公共参数放进 ``ctx.obj``。

    无子命令（双击 exe）时直接进入原生桌面图形界面；也可用 ``sgm gui`` 显式进入。
    """
    _force_utf8_output()
    ensure_user_config(config_path)
    if db_path:
        import os

        os.environ["SGM_STORAGE__DB_PATH"] = db_path
    settings: Settings = reload_settings(config_path) if (config_path or db_path) else get_settings()
    level = "DEBUG" if verbose else str(settings.app.log_level or "INFO")
    logger = setup_logging(level=level, log_dir=settings.logs_dir)
    ctx.ensure_object(dict)
    ctx.obj["settings"] = settings
    ctx.obj["logger"] = logger
    ctx.obj["quiet"] = quiet
    ctx.obj["verbose"] = verbose

    # 关键：子命令统一通过 get_container() 取服务容器单例。若进程内已存在容器
    # （例如测试中多次 invoke，或同一进程切换 --db / --config），必须重建容器，
    # 否则新旧配置混用——表现为「换了 --db 却还读写上一个库」。
    from site_group_monitor.web import deps as web_deps

    existing = web_deps._container
    if existing is None or Path(existing.settings.db_path).resolve() != Path(settings.db_path).resolve():
        web_deps.reset_container()
        web_deps.build_container(settings)
        logger.debug("sgm cli: 已按新配置重建服务容器 db=%s", settings.db_path)
    else:
        # 同一库但配置可能变了（如 SMTP/限速），同步刷新配置对象
        existing.settings = settings

    # 无子命令（双击 exe）时直接进入原生桌面图形界面
    if ctx.invoked_subcommand is None:
        from site_group_monitor.gui.app import run_gui

        ctx.exit(run_gui())

    logger.debug("sgm cli: 配置加载完成 db=%s tz=%s", settings.db_path, settings.timezone_name())


def _register(group: click.Group) -> None:
    """把三组子命令注册到主命令组。

    站点管理命令按文档约定**同时**挂在顶层（``sgm add``）与 ``sgm site`` 分组下，
    两条路径指向同一个 Command 对象，行为完全一致。

    Args:
        group: Click 主命令组。
    """
    group.add_command(site_cmds.site_group)
    # 顶层快捷方式（与 sgm site add/remove/list/import 等价）
    group.add_command(site_cmds.add, name="add")
    group.add_command(site_cmds.remove, name="remove")
    group.add_command(site_cmds.list_sites, name="list")
    group.add_command(site_cmds.import_sites, name="import")
    group.add_command(run_cmds.check)
    group.add_command(run_cmds.report)
    group.add_command(run_cmds.schedule)
    group.add_command(run_cmds.serve)
    group.add_command(run_cmds.gui)
    group.add_command(run_cmds.analyze_snapshot)
    group.add_command(proxy_cmds.proxy_group)
    group.add_command(run_cmds.system_status)
    group.add_command(init_cmds.init)


_register(cli)


# ================================================================ 通用工具
def run_sync(coro: Any) -> Any:
    """在 CLI 环境中执行协程（统一事件循环出口）。

    Args:
        coro: 协程对象。

    Returns:
        协程返回值。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():  # pragma: no cover - CLI 场景通常无运行中的 loop
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()
    return asyncio.run(coro)


def echo_error(message: str) -> None:
    """向 stderr 输出错误提示（统一风格）。

    Args:
        message: 错误文本。
    """
    click.secho(f"[错误] {message}", fg="red", err=True)


def echo_ok(message: str) -> None:
    """向 stdout 输出成功提示。

    Args:
        message: 提示文本。
    """
    click.secho(message, fg="green")


def echo_warn(message: str) -> None:
    """向 stdout 输出警告提示。

    Args:
        message: 警告文本。
    """
    click.secho(f"[注意] {message}", fg="yellow")


def main() -> int:
    """console_script 入口包装，返回进程退出码。

    Returns:
        退出码（0 正常）。
    """
    try:
        cli(obj={}, standalone_mode=True)
        return 0
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.Abort:  # pragma: no cover - Ctrl+C
        echo_error("操作被用户中断")
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 兜底，避免堆栈外泄
        logger = get_logger("cli")
        logger.exception("cli: 未捕获异常 %s", exc)
        echo_error(str(exc))
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
