# -*- coding: utf-8 -*-
"""PyInstaller 打包入口（`站群监控.exe`）。

只做一件事：在调用 Click 主入口前，把「可写目录」和「只读资源目录」的
差异抹平，让 exe 双击运行时行为与源码 `python -m site_group_monitor` 一致。

关键点：
    * exe 首次运行自动在 exe 同级创建 ``data/``、``logs/``、``data/reports/``；
    * 配置模板从打包资源里拷一份到 ``data/config.yaml``（已存在则不覆盖）；
    * ``multiprocessing.freeze_support()`` 防止被打包后子进程反复重启主程序。
"""

from __future__ import annotations

import multiprocessing
import shutil
import sys
from pathlib import Path


def _app_dir() -> Path:
    """exe 所在目录（源码运行时是项目根）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _bundle_dir() -> Path:
    """打包资源解压目录。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return Path(__file__).resolve().parent


def _bootstrap_dirs() -> None:
    """首次运行时创建可写目录并落地默认配置。"""
    app_dir = _app_dir()
    data_dir = app_dir / "data"
    for sub in ("", "reports", "evidence", "backups", "browser-profile"):
        (data_dir / sub).mkdir(parents=True, exist_ok=True)
    (app_dir / "logs").mkdir(parents=True, exist_ok=True)

    config_path = data_dir / "config.yaml"
    if config_path.exists():
        return
    example = _bundle_dir() / "config" / "config.example.yaml"
    if not example.is_file():
        example = _bundle_dir() / "config.example.yaml"
    if example.is_file():
        shutil.copyfile(example, config_path)


def main() -> None:
    """程序入口。"""
    multiprocessing.freeze_support()
    _bootstrap_dirs()

    from site_group_monitor.cli.main import cli

    cli(prog_name="站群监控")


if __name__ == "__main__":
    main()
