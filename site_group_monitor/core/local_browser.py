"""用本机 Edge / Chrome 经 CDP 拉取页面（不打包 Playwright）。

目的：百度收录和部分 WAF 站点走真实浏览器内核，降低纯 HTTP 被风控的概率。
使用独立 ``user-data-dir``，不占用用户日常登录的浏览器配置。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from site_group_monitor.core.http_client import RawResponse
from site_group_monitor.models.entities import ProxyEndpoint

BAIDU_HOME = "https://www.baidu.com/"
_STEALTH_JS = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)

_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_STARTF_USESHOWWINDOW = 0x00000001
_SW_SHOWNORMAL = 1
_SW_RESTORE = 9
_SW_SHOW = 5
_BROWSER_CANDIDATES = (
    (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", "msedge"),
    (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", "msedge"),
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome"),
    (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", "chrome"),
)


def find_browser_executable(channel: str = "auto") -> str:
    """查找本机 Edge / Chrome 可执行文件。

    Args:
        channel: ``auto`` / ``msedge`` / ``chrome``。

    Returns:
        绝对路径；找不到返回空串。
    """
    wanted = (channel or "auto").strip().lower()
    extra: list[tuple[str, str]] = []
    local = os.environ.get("LOCALAPPDATA") or ""
    pf = os.environ.get("PROGRAMFILES") or ""
    pf86 = os.environ.get("PROGRAMFILES(X86)") or ""
    if local:
        extra.append((str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"), "chrome"))
        extra.append((str(Path(local) / "Microsoft" / "Edge" / "Application" / "msedge.exe"), "msedge"))
    if pf:
        extra.append((str(Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe"), "msedge"))
        extra.append((str(Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe"), "chrome"))
    if pf86:
        extra.append((str(Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"), "msedge"))
        extra.append((str(Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe"), "chrome"))
    seen: set[str] = set()
    for path, kind in (*_BROWSER_CANDIDATES, *extra):
        if wanted not in ("auto", kind):
            continue
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        if path and Path(path).is_file():
            return path
    return ""


def chrome_proxy_server(proxy: ProxyEndpoint | None) -> str:
    """把代理实体转成 Chrome ``--proxy-server`` 参数。"""
    if proxy is None or not proxy.host or not proxy.port:
        return ""
    scheme = "socks5" if str(proxy.protocol).lower() == "socks5" else "http"
    if proxy.username and proxy.password_enc:
        return f"{scheme}://{proxy.username}:{proxy.password_enc}@{proxy.host}:{proxy.port}"
    return f"{scheme}://{proxy.host}:{proxy.port}"


def browser_window_count(settings: Any | None) -> int:
    """读取配置的查询窗口数，限制在 1–8。"""
    if settings is None:
        return 1
    cfg = getattr(getattr(getattr(settings, "engine", None), "baidu", None), "browser", None)
    try:
        count = int(getattr(cfg, "windows", 1) or 1)
    except (TypeError, ValueError):
        count = 1
    return max(1, min(8, count))


def _needs_screenshot(url: str) -> bool:
    """只有百度检索页才截图，首页 TDK 不再截，能省一两秒。"""
    text = (url or "").lower()
    return "baidu.com/s" in text or "/s?" in text


def page_wait_seconds(url: str, settle_ms: int, timeout: float) -> float:
    """返回页面内容轮询时长；百度结果页需等待异步结果区渲染。"""
    configured = max(0.2, float(settle_ms) / 1000.0)
    if _needs_screenshot(url):
        return min(max(0.2, float(timeout)), max(5.0, configured))
    return min(max(0.2, float(timeout)), configured)


def _html_ready(url: str, html: str, href: str) -> bool:
    """页面已经能判定收录或抽 TDK 时提前结束等待。"""
    text = html or ""
    blob = f"{text}{href or ''}"
    if any(mark in blob for mark in ("wappass.baidu.com", "百度安全验证", "请输入验证码")):
        return True
    if "baidu.com" in (url or "") or "baidu.com" in (href or ""):
        if "没有找到与" in text or "未找到相关结果" in text or "很抱歉" in text and "没有找到" in text:
            return True
        if "百度为您找到" in text:
            return True
        if ("result-op" in text or "c-container" in text) and len(text) > 4000:
            return True
        return False
    if "页面加载中" in text[:4000]:
        return False
    return len(text) > 1200


def build_launch_args(
    exe: str,
    port: int,
    profile_dir: str,
    *,
    headless: bool,
    proxy_server: str = "",
    slot: int = 0,
    windows: int = 1,
) -> list[str]:
    """构造 Edge/Chrome 启动参数。有窗口模式必须带初始 URL，否则可能只起后台进程。"""
    args = [
        exe,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-popup-blocking",
        "--disable-features=Translate,MediaRouter",
        "--lang=zh-CN",
        "--remote-allow-origins=*",
        "--edge-skip-compat-layer-relaunch",
    ]
    if headless:
        args.extend(["--headless=new", "--disable-gpu", "--window-size=1200,800"])
    elif int(windows or 1) <= 1:
        args.extend([
            "--start-maximized",
            "--window-size=1200,800",
            "--window-position=80,40",
        ])
    else:
        left = 40 + int(slot) * 48
        top = 30 + int(slot) * 36
        args.extend([
            "--window-size=1100,740",
            f"--window-position={left},{top}",
        ])
    if proxy_server:
        args.append(f"--proxy-server={proxy_server}")
    args.append(BAIDU_HOME if not headless else "about:blank")
    return args


def _windows_startupinfo(*, hidden: bool) -> Any:
    """无控制台 exe 启动子进程时，必须显式指定 ShowWindow，否则 GUI 窗口会被继承成隐藏。"""
    if sys.platform != "win32":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= _STARTF_USESHOWWINDOW
    info.wShowWindow = 0 if hidden else _SW_SHOWNORMAL
    return info


def _force_show_process_windows(root_pid: int) -> None:
    """把本进程树里的 Edge/Chrome 窗口恢复到前台。"""
    if sys.platform != "win32" or not root_pid:
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pids = _process_tree_pids(root_pid)
        if not pids:
            return

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd: int, _lparam: int) -> bool:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) not in pids:
                return True
            user32.ShowWindow(hwnd, _SW_RESTORE)
            user32.ShowWindow(hwnd, _SW_SHOW)
            user32.SetForegroundWindow(hwnd)
            return True

        user32.EnumWindows(_enum, 0)
    except Exception:  # noqa: BLE001
        return


def _process_tree_pids(root_pid: int) -> set[int]:
    """读取 Windows 进程树（根进程 + 子进程）。"""
    if sys.platform != "win32":
        return {root_pid}
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.windll.kernel32
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1:
            return {root_pid}
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        children: dict[int, list[int]] = {}
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            parent = int(entry.th32ParentProcessID)
            children.setdefault(parent, []).append(pid)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        kernel32.CloseHandle(snap)
        found = {root_pid}
        stack = [root_pid]
        while stack:
            current = stack.pop()
            for child in children.get(current, []):
                if child not in found:
                    found.add(child)
                    stack.append(child)
        return found
    except Exception:  # noqa: BLE001
        return {root_pid}


def parse_wmic_profile_pids(text: str, profile: str) -> set[int]:
    """解析 wmic LIST 输出，找出命令行含独立配置目录的进程。"""
    needle = os.path.normcase(os.path.abspath(str(profile))).replace("/", "\\")
    alt = needle.replace("\\", "/")
    pids: set[int] = set()
    record: dict[str, str] = {}

    def flush() -> None:
        pid_text = record.get("processid", "").strip()
        cmd = os.path.normcase(record.get("commandline", ""))
        record.clear()
        if not pid_text.isdigit():
            return
        if needle in cmd or alt in cmd or "browser-profile" in cmd:
            if "--user-data-dir" in cmd or "user-data-dir" in cmd:
                pids.add(int(pid_text))

    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        record[key.strip().lower()] = value.strip()
    flush()
    return pids


def _profile_browser_pids(profile: Path) -> set[int]:
    """找出占用本软件独立配置目录的 Edge/Chrome 进程。"""
    if sys.platform != "win32":
        return set()
    flags = _CREATE_NO_WINDOW
    try:
        completed = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                "name='msedge.exe' or name='chrome.exe'",
                "get",
                "ProcessId,CommandLine",
                "/FORMAT:LIST",
            ],
            capture_output=True,
            timeout=12,
            check=False,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        return set()
    text = (completed.stdout or b"").decode("utf-8", errors="ignore")
    pids = parse_wmic_profile_pids(text, str(profile))
    if pids:
        return pids
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" | "
                "ForEach-Object { 'ProcessId=' + $_.ProcessId; 'CommandLine=' + $_.CommandLine; '' }",
            ],
            capture_output=True,
            timeout=15,
            check=False,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        return set()
    text = (completed.stdout or b"").decode("utf-8", errors="ignore")
    return parse_wmic_profile_pids(text, str(profile))


def kill_profile_browsers(profile: Path) -> None:
    """结束占用独立配置目录的残留浏览器，否则新窗口起不来、调试口也会失败。"""
    pids = _profile_browser_pids(profile)
    for pid in pids:
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=8,
                check=False,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        with contextlib.suppress(Exception):
            (profile / name).unlink(missing_ok=True)


def _shell_open_visible(args: list[str]) -> bool:
    """从无控制台 exe 里用 ShellExecute 弹出可见窗口，避免继承隐藏 ShowWindow。"""
    if sys.platform != "win32" or not args:
        return False
    try:
        import ctypes

        params = subprocess.list2cmdline(args[1:])
        rc = int(ctypes.windll.shell32.ShellExecuteW(None, "open", args[0], params, None, _SW_SHOWNORMAL))
        return rc > 32
    except Exception:  # noqa: BLE001
        return False


def _pick_port() -> int:
    """占用一个本机空闲端口后立即释放，供调试端口使用。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _ws_connect_cm(url: str) -> Any:
    """兼容 websockets 12 / 13+ 的连接上下文。"""
    try:
        from websockets.asyncio.client import connect as ws_connect
    except Exception:  # noqa: BLE001
        from websockets import connect as ws_connect  # type: ignore[no-redef]
    return ws_connect(url, max_size=20_000_000, open_timeout=12)


class _Cdp:
    """极简 Chrome DevTools 客户端。"""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self.ws.recv()
                data = json.loads(raw)
                msg_id = data.get("id")
                if msg_id in self._pending:
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        fut.set_result(data)
                else:
                    await self._events.put(data)
        except Exception:  # noqa: BLE001
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("浏览器调试通道已关闭"))
            self._pending.clear()

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 25.0,
    ) -> dict[str, Any]:
        self._id += 1
        payload: dict[str, Any] = {"id": self._id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[self._id] = fut
        await self.ws.send(json.dumps(payload))
        return await asyncio.wait_for(fut, timeout)

    async def wait_event(self, method: str, *, session_id: str | None = None, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            event = await asyncio.wait_for(self._events.get(), remaining)
            if event.get("method") != method:
                continue
            if session_id and event.get("sessionId") != session_id:
                continue
            return event

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(Exception):
            await self.ws.close()


class LocalBrowser:
    """本机浏览器会话：按代理重启进程，页面拉取串行。"""

    def __init__(
        self,
        settings: Any | None = None,
        logger: Any | None = None,
        *,
        slot: int = 0,
        profile_name: str = "",
    ) -> None:
        self.settings = settings
        self.logger = logger
        self._slot = max(0, int(slot))
        self._profile_name = "".join(
            char for char in str(profile_name or "") if char.isalnum() or char in "-_"
        )
        self._lock = asyncio.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._port = 0
        self._proxy_key = ""
        self._cdp: _Cdp | None = None
        self._ws_cm: Any = None
        self._logged_missing = False
        self._warmed_baidu = False
        self.last_url = ""
        self._target_id = ""
        self._session_id = ""

    def _cfg(self) -> Any:
        if self.settings is None:
            return None
        return getattr(getattr(self.settings.engine, "baidu", None), "browser", None)

    def enabled(self) -> bool:
        """配置是否打开本机浏览器抓取。"""
        cfg = self._cfg()
        return bool(cfg is not None and getattr(cfg, "enabled", False))

    def executable(self) -> str:
        """当前应使用的浏览器路径。"""
        cfg = self._cfg()
        channel = str(getattr(cfg, "channel", "auto") or "auto") if cfg is not None else "auto"
        return find_browser_executable(channel)

    def is_available(self) -> bool:
        """配置已开且本机找得到 Edge/Chrome。"""
        return self.enabled() and bool(self.executable())

    def _profile_dir(self) -> Path:
        cfg = self._cfg()
        raw = str(getattr(cfg, "profile_dir", "data/browser-profile") or "data/browser-profile")
        if self.settings is not None and hasattr(self.settings, "resolve_path"):
            path = self.settings.resolve_path(raw)
        else:
            path = Path(raw)
        path = path / (self._profile_name or f"w{self._slot}")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _headless(self) -> bool:
        cfg = self._cfg()
        return False if cfg is None else bool(getattr(cfg, "headless", False))

    def _settle_ms(self) -> int:
        cfg = self._cfg()
        raw = int(getattr(cfg, "settle_ms", 600) or 600) if cfg is not None else 600
        return max(120, min(4000, raw))

    def _via_proxy(self) -> bool:
        cfg = self._cfg()
        return bool(cfg is not None and getattr(cfg, "via_proxy", True))

    def _launch_proxy(self, proxy: ProxyEndpoint | None) -> ProxyEndpoint | None:
        return proxy if self._via_proxy() else None

    def _timeout_sec(self) -> float:
        if self.settings is None:
            return 25.0
        return float(getattr(self.settings.engine.baidu, "timeout_sec", 25) or 25)

    async def fetch(
        self,
        url: str,
        *,
        proxy: ProxyEndpoint | None = None,
        referer: str | None = None,
        force_proxy: bool = False,
    ) -> RawResponse:
        """用本机浏览器打开 URL 并返回 HTML。失败时 ``ok=False``，不抛业务异常。"""
        if not self.is_available():
            if self.enabled() and not self._logged_missing:
                self._logged_missing = True
                if self.logger is not None:
                    self.logger.warning("未找到本机 Edge/Chrome，收录改回 HTTP 直连")
            return RawResponse(ok=False, url=url, error_type="conn_failed", error_msg="本机浏览器不可用")
        async with self._lock:
            started = time.perf_counter()
            try:
                self.last_url = url
                launch = proxy if force_proxy else self._launch_proxy(proxy)
                await self._ensure_running(launch)
                if "baidu.com" in url:
                    self._warmed_baidu = True
                html, status, final_url, png = await self._navigate(url, referer=referer)
                elapsed = int((time.perf_counter() - started) * 1000)
                text = html or ""
                return RawResponse(
                    ok=bool(text) and int(status or 0) < 500,
                    status_code=int(status or 200),
                    url=url,
                    final_url=final_url or url,
                    text=text,
                    content=text.encode("utf-8", errors="ignore"),
                    elapsed_ms=elapsed,
                    screenshot=png,
                )
            except Exception as exc:  # noqa: BLE001
                if self.logger is not None:
                    self.logger.warning("本机浏览器抓取失败 %s: %s", url, exc)
                proc_alive = self._proc is not None and self._proc.poll() is None
                if not proc_alive:
                    await self._stop_unlocked()
                return RawResponse(
                    ok=False,
                    url=url,
                    error_type="conn_failed",
                    error_msg=str(exc),
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                )

    async def close(self) -> None:
        """结束浏览器进程。"""
        async with self._lock:
            await self._stop_unlocked()

    async def ensure_started(self, proxy: ProxyEndpoint | None = None) -> None:
        """启动浏览器进程（已在跑则复用，不关窗口）。"""
        async with self._lock:
            await self._ensure_running(self._launch_proxy(proxy))

    async def evaluate_value(self, expression: str, *, timeout: float = 10.0) -> Any:
        """在当前浏览器页面执行 JavaScript 并返回可序列化结果。"""
        async with self._lock:
            await self._ensure_running(None)
            if self._cdp is None or not self._session_id:
                raise RuntimeError("浏览器页面尚未就绪")
            response = await self._cdp.send(
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True, "awaitPromise": True},
                session_id=self._session_id,
                timeout=max(1.0, float(timeout)),
            )
            result = (response.get("result") or {}).get("result") or {}
            if result.get("subtype") == "error":
                raise RuntimeError(str(result.get("description") or "页面脚本执行失败"))
            return result.get("value")

    async def _ensure_running(self, proxy: ProxyEndpoint | None) -> None:
        key = proxy.key if proxy else "direct"
        proc_alive = self._proc is not None and self._proc.poll() is None
        if self._cdp is not None and proc_alive and self._proxy_key == key:
            return
        await self._stop_unlocked()
        await self._start_unlocked(proxy)
        self._proxy_key = key

    async def _start_unlocked(self, proxy: ProxyEndpoint | None) -> None:
        exe = self.executable()
        if not exe:
            raise RuntimeError("未找到 Edge/Chrome")
        profile = self._profile_dir()
        await asyncio.to_thread(kill_profile_browsers, profile)
        port = _pick_port()
        hidden = self._headless()
        args = build_launch_args(
            exe,
            port,
            str(profile),
            headless=hidden,
            proxy_server=chrome_proxy_server(proxy),
            slot=self._slot,
            windows=browser_window_count(self.settings),
        )
        kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "startupinfo": _windows_startupinfo(hidden=hidden),
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW if hidden else _CREATE_BREAKAWAY_FROM_JOB
        self._proc = subprocess.Popen(args, **kwargs)  # noqa: S603 - 启动本机已安装的浏览器
        self._port = port
        version = await self._wait_devtools(port)
        ws_url = str(version.get("webSocketDebuggerUrl") or "")
        if not ws_url:
            raise RuntimeError("浏览器未返回调试地址")
        self._ws_cm = _ws_connect_cm(ws_url)
        ws = await self._ws_cm.__aenter__()
        self._cdp = _Cdp(ws)
        await self._attach_page()
        if not hidden:
            await self._reveal_window()
        if self.logger is not None:
            self.logger.info(
                "已启动本机浏览器查收录 (%s, 窗口=%s, proxy=%s)",
                Path(exe).name,
                "隐藏" if hidden else "显示",
                proxy.masked_url if proxy else "direct",
            )

    async def _wait_devtools(self, port: int) -> dict[str, Any]:
        url = f"http://127.0.0.1:{port}/json/version"
        last = ""
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(50):
                try:
                    response = await client.get(url, timeout=1.5)
                    if response.status_code == 200:
                        data = response.json()
                        if isinstance(data, dict):
                            return data
                except Exception as exc:  # noqa: BLE001
                    last = str(exc)
                await asyncio.sleep(0.12)
        raise RuntimeError(f"浏览器调试端口未就绪: {last or url}")

    async def _attach_page(self) -> None:
        cdp = self._cdp
        if cdp is None:
            raise RuntimeError("调试通道未建立")
        timeout = self._timeout_sec()
        page = None
        infos: list[dict[str, Any]] = []
        for _ in range(20):
            listed = await cdp.send("Target.getTargets", timeout=timeout)
            infos = list((listed.get("result") or {}).get("targetInfos") or [])
            page = next((item for item in infos if item.get("type") == "page"), None)
            if page is not None:
                break
            await asyncio.sleep(0.15)
        if page is None:
            created = await cdp.send("Target.createTarget", {"url": BAIDU_HOME}, timeout=timeout)
            target_id = str((created.get("result") or {}).get("targetId") or "")
        else:
            target_id = str(page.get("targetId") or "")
        if not target_id:
            raise RuntimeError("无法打开浏览器标签")
        attached = await cdp.send(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
            timeout=timeout,
        )
        session_id = str((attached.get("result") or {}).get("sessionId") or "")
        self._target_id = target_id
        self._session_id = session_id
        await cdp.send("Page.enable", session_id=session_id, timeout=timeout)
        await cdp.send("Network.enable", session_id=session_id, timeout=timeout)
        listed = await cdp.send("Target.getTargets", timeout=timeout)
        infos = list((listed.get("result") or {}).get("targetInfos") or [])
        for item in infos:
            extra_id = str(item.get("targetId") or "")
            if item.get("type") == "page" and extra_id and extra_id != target_id:
                with contextlib.suppress(Exception):
                    await cdp.send("Target.closeTarget", {"targetId": extra_id}, timeout=5)
        with contextlib.suppress(Exception):
            await cdp.send(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": _STEALTH_JS},
                session_id=session_id,
            )

    async def _reveal_window(self) -> None:
        cdp = self._cdp
        if cdp is None or not self._target_id:
            return
        with contextlib.suppress(Exception):
            await cdp.send("Target.activateTarget", {"targetId": self._target_id})
        with contextlib.suppress(Exception):
            found = await cdp.send("Browser.getWindowForTarget", {"targetId": self._target_id})
            window_id = (found.get("result") or {}).get("windowId")
            if window_id is not None:
                await cdp.send(
                    "Browser.setWindowBounds",
                    {
                        "windowId": window_id,
                        "bounds": {
                            "windowState": "normal",
                            "left": 80,
                            "top": 40,
                            "width": 1200,
                            "height": 800,
                        },
                    },
                )

    async def _navigate(self, url: str, *, referer: str | None = None) -> tuple[str, int, str, bytes]:
        cdp = self._cdp
        if cdp is None:
            raise RuntimeError("调试通道未建立")
        if not self._session_id:
            await self._attach_page()
        timeout = self._timeout_sec()
        session_id = self._session_id
        status = 200
        if referer:
            with contextlib.suppress(Exception):
                await cdp.send(
                    "Network.setExtraHTTPHeaders",
                    {"headers": {"Referer": referer, "Accept-Language": "zh-CN,zh;q=0.9"}},
                    session_id=session_id,
                    timeout=timeout,
                )
        nav = await cdp.send("Page.navigate", {"url": url}, session_id=session_id, timeout=timeout)
        error = (nav.get("result") or {}).get("errorText")
        if error:
            raise RuntimeError(str(error))
        html, final_url = await self._wait_page(session_id, url, timeout)
        png = b""
        if _needs_screenshot(url):
            with contextlib.suppress(Exception):
                shot = await cdp.send(
                    "Page.captureScreenshot",
                    {"format": "png", "fromSurface": True},
                    session_id=session_id,
                    timeout=min(8.0, timeout),
                )
                data = str((shot.get("result") or {}).get("data") or "")
                if data:
                    png = base64.b64decode(data)
        if "wappass.baidu.com" in final_url or "wappass.baidu.com" in html:
            status = 302
        elif not html:
            status = 0
        return html, status, final_url, png

    async def _wait_page(self, session_id: str, url: str, timeout: float) -> tuple[str, str]:
        """等 DOM 就绪后轮询 HTML，结果出来就停，不再固定睡 1.8 秒。"""
        cdp = self._cdp
        if cdp is None:
            return "", url
        ready = await self._eval_ready(session_id)
        if ready not in ("interactive", "complete"):
            try:
                await cdp.wait_event(
                    "Page.domContentEventFired",
                    session_id=session_id,
                    timeout=min(8.0, timeout),
                )
            except TimeoutError:
                with contextlib.suppress(Exception):
                    await cdp.wait_event(
                        "Page.loadEventFired",
                        session_id=session_id,
                        timeout=2.5,
                    )
        deadline = time.monotonic() + page_wait_seconds(url, self._settle_ms(), timeout)
        html, href = "", url
        while True:
            html, href = await self._eval_html(session_id)
            if _html_ready(url, html, href):
                return html, href or url
            if time.monotonic() >= deadline:
                return html, href or url
            await asyncio.sleep(0.12)

    async def _eval_ready(self, session_id: str) -> str:
        cdp = self._cdp
        if cdp is None:
            return ""
        with contextlib.suppress(Exception):
            evaluated = await cdp.send(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
                session_id=session_id,
                timeout=5,
            )
            return str(((evaluated.get("result") or {}).get("result") or {}).get("value") or "")
        return ""

    async def _eval_html(self, session_id: str) -> tuple[str, str]:
        cdp = self._cdp
        if cdp is None:
            return "", ""
        try:
            evaluated = await cdp.send(
                "Runtime.evaluate",
                {
                    "expression": "({html: document.documentElement ? document.documentElement.outerHTML : '', href: location.href})",
                    "returnByValue": True,
                },
                session_id=session_id,
                timeout=8,
            )
        except Exception:  # noqa: BLE001
            return "", ""
        value = ((evaluated.get("result") or {}).get("result") or {}).get("value") or {}
        return str(value.get("html") or ""), str(value.get("href") or "")

    async def _stop_unlocked(self) -> None:
        if self._cdp is not None:
            with contextlib.suppress(Exception):
                await self._cdp.send("Browser.close", timeout=3)
            with contextlib.suppress(Exception):
                await self._cdp.close()
            self._cdp = None
        if self._ws_cm is not None:
            with contextlib.suppress(Exception):
                await self._ws_cm.__aexit__(None, None, None)
            self._ws_cm = None
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(proc.wait), 3)
            except Exception:  # noqa: BLE001
                with contextlib.suppress(Exception):
                    proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(kill_profile_browsers, self._profile_dir())
        self._port = 0
        self._proxy_key = ""
        self._warmed_baidu = False
        self._target_id = ""
        self._session_id = ""


class BrowserPool:
    """按配置打开多个独立查询窗口，并发拉取页面。"""

    def __init__(
        self,
        settings: Any | None = None,
        logger: Any | None = None,
        *,
        max_slots: int | None = None,
    ) -> None:
        count = browser_window_count(settings)
        if max_slots is not None:
            count = min(count, max(1, int(max_slots)))
        self.settings = settings
        self.logger = logger
        self._slots = [LocalBrowser(settings, logger, slot=index) for index in range(count)]
        self._idle: asyncio.Queue[LocalBrowser] | None = None
        self.last_url = ""

    def enabled(self) -> bool:
        return bool(self._slots and self._slots[0].enabled())

    def executable(self) -> str:
        return self._slots[0].executable() if self._slots else ""

    def is_available(self) -> bool:
        return bool(self._slots and self._slots[0].is_available())

    def _queue(self) -> asyncio.Queue[LocalBrowser]:
        if self._idle is None:
            self._idle = asyncio.Queue()
            for browser in self._slots:
                self._idle.put_nowait(browser)
        return self._idle

    async def acquire(self) -> LocalBrowser:
        """占用一个查询窗口，整站收录+TDK 共用，用完必须 release。"""
        return await self._queue().get()

    async def release(self, browser: LocalBrowser | None) -> None:
        if browser is None:
            return
        self.last_url = browser.last_url
        await self._queue().put(browser)

    async def fetch(
        self,
        url: str,
        *,
        proxy: ProxyEndpoint | None = None,
        referer: str | None = None,
        force_proxy: bool = False,
    ) -> RawResponse:
        browser = await self.acquire()
        try:
            result = await browser.fetch(url, proxy=proxy, referer=referer, force_proxy=force_proxy)
            self.last_url = browser.last_url
            return result
        finally:
            await self.release(browser)

    async def warm(self) -> None:
        """本轮检测开始时先把窗口拉起来，站点之间不再反复开关进程。"""
        if not self.is_available():
            return
        cfg = self._slots[0]._cfg() if self._slots else None
        if cfg is not None and getattr(cfg, "via_proxy", True):
            # 代理按站点分配，预热时还不知道代理；此时直连启动会在首站立刻重启，反而更慢。
            return
        await asyncio.gather(
            *(item.ensure_started() for item in self._slots),
            return_exceptions=True,
        )

    async def close(self) -> None:
        for browser in self._slots:
            with contextlib.suppress(Exception):
                await browser.close()
        self._idle = None
        self.last_url = ""
