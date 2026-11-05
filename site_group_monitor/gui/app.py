"""原生桌面窗口入口：把现有 ECharts 仪表盘封装进 Windows 原生窗口。

实现：后台线程起 FastAPI/uvicorn（127.0.0.1 随机端口，复用既有 Service 层与
仪表盘静态资源），就绪后用 ``pywebview``（edgechromium / WebView2 后端）打开一个
真正的 Win32 窗口加载仪表盘。这样既保留已做好的图表/表格/TDK 详情，又是真·桌面程序，
双击 exe 直接弹窗、不依赖浏览器、无黑框。

退出：窗口关闭后 ``webview.start()`` 返回，优雅关闭 uvicorn 与调度器。
若 WebView2 / pywebview 不可用，降级为打开系统默认浏览器并弹提示框。
"""

from __future__ import annotations

import io
import logging
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from site_group_monitor import __version__

logger = logging.getLogger("gui")

# 窗口化 exe（console=False）没有控制台输出流；uvicorn 的运行日志统一交由应用文件
# 日志记录，禁用它自身的控制台 handler，避免后台请求反复触发 NoneType 写入异常。
_GUI_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "access": {
            "format": '%(asctime)s [%(levelname)s] %(name)s: %(client_addr)s - "%(request_line)s" %(status_code)s',
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "default": {
            "class": "logging.NullHandler",
        },
        "access": {
            "class": "logging.NullHandler",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


def _redirect_windowed_streams(logs_dir: Path) -> None:
    """窗口化 exe（console=False）下 sys.stdout / sys.stderr 为 None，
    重定向到一个日志文件，避免任何模块对 None 调用 isatty() 导致崩溃。

    控制台模式下两者本来就是有效文件对象，无需处理。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_path = Path(logs_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        f = open(log_path / "sgm-gui.log", "a", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001 - 实在写不了就退化为内存缓冲
        f = io.StringIO()
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


def _free_port() -> int:
    """拿一个空闲本地端口。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_ready(base: str, timeout: float = 30.0) -> bool:
    """轮询健康检查接口，直到服务可用或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def _msgbox(text: str, title: str = "站群监控") -> None:
    """无控制台环境下也能给用户一个可见提示（Windows MessageBox）。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, str(text), str(title), 0)
    except Exception:  # noqa: BLE001 - 兜底：实在没有 UI 就静默
        logger.warning("GUI 提示（无 MessageBox）：%s", text)


def _icon_path(settings: Any | None = None) -> str | None:
    """定位窗口 / 任务栏图标（.ico）。找不到时返回 None，打包 exe 会回退用自身嵌入图标。"""
    candidates: list[Path] = []
    if settings is not None:
        try:
            candidates.append(Path(settings.static_dir) / "sgm.ico")
        except Exception:  # noqa: BLE001
            pass
    candidates.append(Path(__file__).resolve().parent.parent / "web" / "static" / "sgm.ico")
    try:
        from site_group_monitor.config.settings import PACKAGE_ROOT

        candidates.append(PACKAGE_ROOT / "site_group_monitor" / "web" / "static" / "sgm.ico")
    except Exception:  # noqa: BLE001
        pass
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _has_webview2_runtime() -> bool:
    """检测系统是否安装 WebView2 Runtime（缺失时 pywebview 会静默失败成白屏，必须预检）。"""
    try:
        import winreg

        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        ]
        for hive, path in keys:
            try:
                with winreg.OpenKey(hive, path) as k:
                    pv, _ = winreg.QueryValueEx(k, "pv")
                    if pv and pv != "0.0.0.0":
                        return True
            except OSError:
                continue
    except Exception:  # noqa: BLE001 - 检测失败不阻塞，交给 pywebview 自己报
        pass
    return False


def run_gui() -> int:
    """启动原生桌面窗口。返回进程退出码。"""
    # 1) 确保配置与库就位（首次双击也能用）
    from site_group_monitor.config.settings import ensure_user_config

    ensure_user_config()

    from site_group_monitor.web import deps

    container = deps.get_container()
    settings = container.settings

    # 2) 启动内嵌调度器（与 serve 一致，支持每日自动检测）
    scheduler_started = False
    if bool(getattr(settings.scheduler, "enabled", True)):
        try:
            container.scheduler.start_background()
            scheduler_started = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("调度器启动失败（GUI 仍可手动检测）：%s", exc)

    # 2.5) 注册代理 API 源的自动刷新任务（失败不应阻塞 GUI 启动）
    if scheduler_started:
        try:
            registered = container.scheduler.register_proxy_api_sources(
                container.repos, run_immediately=True
            )
            if registered:
                logger.info("GUI：已注册 %d 个代理 API 源刷新任务", registered)
        except Exception as exc:  # noqa: BLE001
            logger.warning("代理 API 源任务注册失败（不影响其余功能）：%s", exc)

    # 3) 后台起 uvicorn（仅本地环回）
    import uvicorn
    from site_group_monitor.web.app import create_app

    # 窗口化 exe 下 sys.stdout/stderr 为 None，先重定向再启动 uvicorn，
    # 否则 uvicorn 默认日志配置会因 isatty() 在 None 上崩溃。
    _redirect_windowed_streams(settings.logs_dir)

    port = _free_port()
    app = create_app(container)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=_GUI_LOG_CONFIG,
    )
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    base = f"http://127.0.0.1:{port}"
    if not _wait_ready(base):
        logger.error("仪表盘服务启动失败")
        _msgbox(f"仪表盘服务启动失败，请查看日志目录 {settings.logs_dir}")
        return 1

    # 4) 打开原生窗口（WebView2 / edgechromium 后端）
    # SGM_HEADLESS_GUI=1：无显示环境下只验证「webview 可导入 + 服务就绪」关键路径，
    # 不真正创建窗口（CI / 验收机无桌面）。真实双击走正常分支。
    import os as _os

    headless = bool(_os.environ.get("SGM_HEADLESS_GUI"))
    selftest = bool(_os.environ.get("SGM_GUI_SELFTEST"))
    try:
        import webview

        if headless:
            logger.info("HEADLESS GUI 测试：webview 已导入，仪表盘就绪于 %s", base)
            return 0

        # 预检 WebView2 Runtime：缺失直接走浏览器降级（附明确原因），
        # 避免 pywebview 内部静默失败只留下一个白屏窗口。
        if not _has_webview2_runtime():
            raise RuntimeError(
                "WebView2 Runtime 未安装。请到 https://developer.microsoft.com/microsoft-edge/webview2/ "
                "下载安装 Evergreen Runtime 后重试。"
            )

        window = webview.create_window(
            f"站群监控 v{__version__}",
            base,
            width=1366,
            height=800,
            min_size=(1024, 640),
            text_select=True,
            confirm_close=False,
        )

        if selftest:
            # 真窗口自检：6 秒内页面未触发 loaded 则判定 WebView2 初始化失败（白屏）
            loaded = threading.Event()
            window.events.loaded += lambda: loaded.set()

            def _auto_close() -> None:
                time.sleep(6)
                print("SELFTEST loaded=%s" % loaded.is_set())
                for w in getattr(webview, "windows", []):
                    try:
                        w.destroy()
                    except Exception:  # noqa: BLE001
                        pass

            threading.Thread(target=_auto_close, daemon=True).start()
        # 用户数据目录固定到 exe 同级可写目录（规避 C:/Program Files 等不可写位置的 E_ABORT）
        storage = Path(settings.logs_dir).parent / "webview"
        icon = _icon_path(settings)
        try:
            storage.mkdir(parents=True, exist_ok=True)
            start_kwargs: dict[str, Any] = {"private_mode": False, "storage_path": str(storage)}
            if icon:
                start_kwargs["icon"] = icon
            webview.start(**start_kwargs)
        except TypeError:
            # 老版 pywebview 无 storage_path / icon 时退回默认行为
            try:
                webview.start(icon=icon) if icon else webview.start()
            except TypeError:
                webview.start()
    except Exception as exc:  # noqa: BLE001 - 组件缺失/WebView2 未安装等
        logger.exception("原生窗口启动失败，降级到浏览器：%s", exc)
        reason = f"{type(exc).__name__}: {exc}"
        try:
            webbrowser.open(base)
            _msgbox(
                "原生窗口组件不可用（%s）\n已改用浏览器打开仪表盘：%s\n\n"
                "服务将持续在后台运行；退出请用任务管理器结束“站群监控.exe”进程。" % (reason, base)
            )
        except Exception:  # noqa: BLE001
            pass
        # 降级模式：必须保持服务运行（否则浏览器页面所有请求都会失败）。
        # 主线程在此永久挂起，不落入 finally 的关停逻辑；退出 = 结束进程。
        threading.Event().wait()
        return 0
    finally:
        server.should_exit = True
        if scheduler_started:
            try:
                container.scheduler.unregister_proxy_api_sources()
            except Exception:  # noqa: BLE001
                pass
            try:
                container.scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_gui())
