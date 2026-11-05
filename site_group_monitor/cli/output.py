"""CLI 输出辅助（独立模块，避免与 ``cli/main.py`` 形成循环导入）。

``cli/main.py`` 需要 import 各子命令模块来注册命令，而子命令模块又需要这些
echo 辅助函数——若它们定义在 ``main.py`` 里就会循环导入。因此单独抽到这里：
依赖方向固定为 ``子命令模块 → output``，``main.py → 子命令模块``。

本模块只依赖 ``click``，不 import 任何项目内模块。
"""

from __future__ import annotations

import click

__all__ = ["echo_error", "echo_ok", "echo_warn", "echo_info"]


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


def echo_info(message: str) -> None:
    """向 stdout 输出普通信息。

    Args:
        message: 提示文本。
    """
    click.echo(message)
