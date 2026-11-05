"""``python -m site_group_monitor`` 入口。

等价于安装后生成的 ``sgm`` console script，便于在未安装的环境下直接调试运行。

Example:
    python -m site_group_monitor --help
    python -m site_group_monitor check --full
"""

from __future__ import annotations

from site_group_monitor.cli.main import cli


def main() -> None:
    """包执行入口，委托给 Click CLI 主命令。"""
    cli()


if __name__ == "__main__":  # pragma: no cover
    main()
