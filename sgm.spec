# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成单文件 `站群监控-v版本号.exe`（Windows）。

构建：
    pip install pyinstaller
    pyinstaller sgm.spec --noconfirm

产物示例：`dist/站群监控-v0.1.4.exe`
"""

from pathlib import Path
import rich
from site_group_monitor import __version__

ROOT = Path(SPECPATH).resolve()

# 需要随包携带的只读资源（全部用相对 _MEIPASS 的路径落地）
datas = [
    (str(ROOT / "site_group_monitor" / "storage" / "schema.sql"),
     "site_group_monitor/storage"),
    (str(ROOT / "site_group_monitor" / "web" / "static"), "site_group_monitor/web/static"),
    (str(ROOT / "site_group_monitor" / "report" / "templates"),
     "site_group_monitor/report/templates"),
    (str(ROOT / "config" / "config.example.yaml"), "config"),
    (str(ROOT / "config" / "logging.yaml"), "config"),
    # Rich 15 按运行时 Unicode 版本动态 import ``rich._unicode_data.unicode*-*-*``；
    # 文件名含连字符，PyInstaller 静态分析无法识别，必须作为外部数据目录携带。
    (str(Path(rich.__file__).resolve().parent / "_unicode_data"), "rich/_unicode_data"),
]

# 邮件模板目录存在才带上
_mailer_templates = ROOT / "site_group_monitor" / "notify" / "templates"
if _mailer_templates.is_dir():
    datas.append((str(_mailer_templates), "site_group_monitor/notify/templates"))

hiddenimports = [
    # uvicorn / fastapi 动态导入，静态分析抓不到
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "websockets",
    "websockets.asyncio",
    "websockets.asyncio.client",
    # 解析 / 编码
    "parsel",
    "lxml.etree",
    "lxml._elementpath",
    "bs4",
    "charset_normalizer",
    "openpyxl",
    "openpyxl.cell._writer",
    # 调度
    "apscheduler.triggers.cron",
    "apscheduler.executors.pool",
    "apscheduler.jobstores.memory",
    # 加密（SSL 证书解析）
    "cryptography",
    "cryptography.hazmat.backends.openssl",
    # SSL 校验的 CA 集合（status_checker._build_ssl_context 依赖，
    # 缺了 exe 里所有 HTTPS 站都会被误判 tls_failed）
    "certifi",
    # 本包内动态导入
    "site_group_monitor.report.html_report",
    "site_group_monitor.report.excel_report",
    # 原生桌面窗口（pywebview + WebView2 / edgechromium 后端）
    # edgechromium 平台在运行时由 webview 动态选择，静态分析抓不到，必须显式列出；
    # 它依赖 pythonnet/clr_loader 调用 WebView2 COM，一并收集。
    "webview",
    "webview.platforms",
    "webview.platforms.edgechromium",
    "pythonnet",
    "clr_loader",
    "bottle",
    "proxy_tools",
]

excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "PyQt5",
    "PySide2",
    "IPython",
    "notebook",
    "pytest",
    # playwright 是**可选兜底**（tdk.render_fallback 默认关闭），但它的
    # driver 目录带 node.exe + coreBundle.js，实测会把 exe 撑到 72MB、
    # 冷启动拖到 9~14s。这里显式排除，配合 tdk_parser 里的动态导入，
    # 运行时真要用时由用户自行安装 playwright。
    "playwright",
    "playwright._impl",
    "playwright.async_api",
    "playwright.sync_api",
    # Pillow：项目全目录无 import PIL，纯被捎带进来（约 12MB）
    "PIL",
]

a = Analysis(
    [str(ROOT / "sgm_main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=f"站群监控-v{__version__}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon=str(ROOT / "site_group_monitor" / "web" / "static" / "sgm.ico"),
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
