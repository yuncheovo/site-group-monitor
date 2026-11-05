"""CLI 冒烟测试：命令注册完整性 + 关键路径可用性。

这组测试专门防止「模块能 import 但 CLI 起不来」的回归——
历史上曾出现两处只有真跑 CLI 才会暴露的问题：
    1. ``cli/main.py`` 与子命令模块循环导入（``--help`` 直接崩）；
    2. 顶层 ``add/remove/list/import`` 未注册（文档承诺的快捷命令不存在）。
"""

from __future__ import annotations

from click.testing import CliRunner

from site_group_monitor.cli.main import cli


def test_cli_help_works() -> None:
    """``--help`` 必须能正常输出（防循环导入回归）。"""
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    assert "站群" in result.output


def test_top_level_site_commands_registered() -> None:
    """顶层快捷命令与 ``site`` 分组都必须存在。"""
    for name in ("add", "remove", "list", "import", "site",
                 "check", "report", "schedule", "serve", "proxy", "system-status"):
        assert name in cli.commands, f"缺少命令: {name}"


def test_site_group_has_same_subcommands() -> None:
    """``sgm site`` 分组内含 add/remove/list/import。"""
    site_group = cli.commands["site"]
    for name in ("add", "remove", "list", "import"):
        assert name in site_group.commands, f"site 分组缺少子命令: {name}"


def test_add_remove_roundtrip(tmp_path) -> None:
    """真实执行 ``add`` → ``remove``，验证 CLI 与 Service 层打通。"""
    db_path = tmp_path / "cli.db"
    runner = CliRunner()

    added = runner.invoke(cli, ["--db", str(db_path), "add", "https://WWW.Cli-Demo.COM/x", "--group", "t"])
    assert added.exit_code == 0, added.output
    assert "cli-demo.com" in added.output

    listed = runner.invoke(cli, ["--db", str(db_path), "list"])
    assert listed.exit_code == 0, listed.output
    assert "cli-demo.com" in listed.output

    removed = runner.invoke(cli, ["--db", str(db_path), "remove", "cli-demo.com"])
    assert removed.exit_code == 0, removed.output


def test_add_is_idempotent(tmp_path) -> None:
    """同一域名不同写法重复添加只保留一条（域名归一化 + 幂等）。"""
    db_path = tmp_path / "idem.db"
    runner = CliRunner()
    for raw in ("https://www.same-site.com/a", "same-site.com", "http://SAME-SITE.com/"):
        assert runner.invoke(cli, ["--db", str(db_path), "add", raw]).exit_code == 0

    from site_group_monitor.storage.database import Database
    from site_group_monitor.storage.repository import Repositories

    database = Database(db_path)
    database.init_db()
    sites = Repositories(database).sites.list_all()
    assert len(sites) == 1
    assert sites[0].domain == "same-site.com"
