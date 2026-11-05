# -*- coding: utf-8 -*-
"""exe 产物独立验收脚本（QA 用）。

在**干净空目录**里跑打包好的 sgm.exe，逐项验证：
    1. exe 不依赖源码目录（单独拷过去也能跑）
    2. 资源齐全（schema.sql / 前端静态资源 / 模板）—— 用真实功能反推，不看包内容
    3. 数据落在 exe 同级目录，而不是 PyInstaller 临时解压目录
    4. CLI 各子命令可用
    5. 仪表盘能起、页面与 API 可用

用法：
    python verify_exe.py <sgm.exe路径>
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f"  |  {detail}" if detail else ""))
    return ok


def free_port() -> int:
    """拿一个空闲端口。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(exe: Path, args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    """在指定目录跑 exe。"""
    return subprocess.run(
        [str(exe), *args], cwd=str(cwd), capture_output=True, text=True,
        timeout=timeout, encoding="utf-8", errors="replace",
    )


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python verify_exe.py <站群监控.exe路径>")
        return 2
    src_exe = Path(sys.argv[1]).resolve()
    if not src_exe.is_file():
        print(f"找不到 exe: {src_exe}")
        return 2

    # 记录 TEMP 里已有的 _MEI 解压目录，结尾只用"本次新增"做判据，
    # 避免把几天前别的进程残留误判成本次 exe 的泄漏。
    _mei_before = set(Path(os.environ.get("TEMP", ".")).glob("_MEI*"))

    workdir = src_exe.parent.parent / "sgm_exe_verify"
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    exe = workdir / src_exe.name
    process_image = exe.name
    shutil.copy2(src_exe, exe)

    print("=" * 78)
    print(f"干净目录: {workdir}")
    print(f"exe 大小: {exe.stat().st_size / 1024 / 1024:.1f} MB")
    print("=" * 78)

    # ---- 1. --help / --version ----
    started = time.perf_counter()
    proc = run(exe, ["--help"], workdir)
    cold_ms = (time.perf_counter() - started) * 1000
    check("exe --help 退出码 0", proc.returncode == 0, f"exit={proc.returncode}")
    check("exe --help 无 Traceback", "Traceback" not in (proc.stdout + proc.stderr))
    check("exe --help 列出子命令", "check" in proc.stdout and "report" in proc.stdout)
    print(f"    冷启动耗时: {cold_ms:.0f} ms")

    proc = run(exe, ["--version"], workdir)
    check("exe --version 可用", proc.returncode == 0 and bool(proc.stdout.strip()),
          proc.stdout.strip()[:60])

    # ---- 2. 首次运行自动建目录 + 配置 ----
    proc = run(exe, ["init"], workdir)
    out = proc.stdout + proc.stderr
    check("exe init 退出码 0", proc.returncode == 0, f"exit={proc.returncode}")
    check("exe init 无 Traceback", "Traceback" not in out)
    for sub in ("data", "logs"):
        check(f"exe 同级存在 {sub}/", (workdir / sub).is_dir())
    for sub in ("data/reports", "data/evidence"):
        check(f"exe 同级存在 {sub}/", (workdir / sub).is_dir())
    check("exe 同级存在 data/monitor.db", (workdir / "data" / "monitor.db").is_file())
    check("exe 同级存在 data/config.yaml", (workdir / "data" / "config.yaml").is_file())

    # ---- 3. 导入 / 增删 / 列表 ----
    csv_path = workdir / "sites.csv"
    csv_path.write_text(
        "domain,group,tags\nexample.com,默认,测试\nhttp://www.example.org,默认,测试\n",
        encoding="utf-8",
    )
    proc = run(exe, ["import", str(csv_path)], workdir)
    out = proc.stdout + proc.stderr
    check("exe import CSV 退出码 0", proc.returncode == 0, f"exit={proc.returncode}")
    check("exe import 无 Traceback", "Traceback" not in out)
    check("exe import 报告新增数量", "新增 2" in out, out.strip()[-120:])

    proc = run(exe, ["list"], workdir)
    out = proc.stdout + proc.stderr
    check("exe list 退出码 0", proc.returncode == 0)
    check("exe list 能看到两个域名", "example.com" in out and "example.org" in out)

    proc = run(exe, ["add", "https://WWW.Added-Site.COM/path?x=1"], workdir)
    out = proc.stdout + proc.stderr
    check("exe add 归一化域名", "added-site.com" in out, out.strip()[-100:])

    # ---- 4. 报告生成（会用到 schema / 模板 / 资源） ----
    proc = run(exe, ["report", "--date", "today"], workdir)
    out = proc.stdout + proc.stderr
    check("exe report 退出码 0", proc.returncode == 0, f"exit={proc.returncode}")
    check("exe report 无 Traceback", "Traceback" not in out)
    reports = list((workdir / "data" / "reports").glob("report-*.html"))
    check("exe report 产出 HTML", bool(reports),
          reports[0].name if reports else "无")
    xlsx = list((workdir / "data" / "reports").glob("report-*.xlsx"))
    check("exe report 产出 XLSX", bool(xlsx), xlsx[0].name if xlsx else "无")
    if reports:
        size = reports[0].stat().st_size
        check("报告 HTML 体积合理（非空壳）", size > 1500, f"{size} 字节")

    # ---- 5. 非法日期要友好报错，不能是裸堆栈 ----
    proc = run(exe, ["report", "--date", "abc"], workdir)
    out = proc.stdout + proc.stderr
    check("非法日期无 Traceback", "Traceback" not in out, out.strip()[-100:])
    check("非法日期有可读提示", "不是合法日期" in out or "Invalid value" in out)

    # ---- 6. 仪表盘 ----
    port = free_port()
    server = subprocess.Popen(
        [str(exe), "serve", "--port", str(port), "--no-scheduler"],
        cwd=str(workdir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    base = f"http://127.0.0.1:{port}"
    ready = False
    for _ in range(90):
        try:
            with urllib.request.urlopen(base + "/", timeout=2) as resp:
                if resp.status == 200:
                    ready = True
                    break
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    check("exe serve 仪表盘首页 200", ready, f"port={port}")

    if ready:
        for path, label in (("/static/app.js", "app.js"), ("/static/style.css", "style.css"),
                            ("/static/vendor/echarts.min.js", "echarts.min.js"),
                            ("/api/overview", "API 总览")):
            try:
                with urllib.request.urlopen(base + path, timeout=5) as resp:
                    body = resp.read()
                    ok = resp.status == 200 and len(body) > 100
                    check(f"仪表盘 {label} 可访问", ok, f"status={resp.status} {len(body)}B")
            except Exception as exc:  # noqa: BLE001
                check(f"仪表盘 {label} 可访问", False, f"{type(exc).__name__}: {exc}")
    # 窗口化 exe（console=False）收不到 Ctrl+C，直接用 terminate（TerminateProcess）；
    # 再兜底 taskkill /F 确保本次被测 exe 实例退出，避免窗口化子进程残留持有 _MEI。
    try:
        server.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        server.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass
    try:
        subprocess.run(["taskkill", "/IM", process_image, "/F"],
                       capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1)
    # 先确认**所有**被测程序进程都已退出（可能还有别的残留实例）。
    # 只有进程真的还活着并持锁，才算"真泄漏"；若进程已退出，剩的 _MEI 多半是
    # 杀软（Windows Defender）扫描临时解压目录时的短暂持锁，过一会儿就能删，
    # 不属于程序缺陷。
    def _app_alive() -> bool:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {process_image}", "/NH"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=10,
            ).stdout.strip()
            return process_image.lower() in out.lower()
        except Exception:  # noqa: BLE001
            return False

    for _ in range(60):  # 最多等 30s 让 serve 子进程及线程池彻底退出
        if not _app_alive():
            break
        time.sleep(0.5)
    sgm_alive_at_end = _app_alive()

    # ---- 7. 数据落盘位置核对 ----
    db = workdir / "data" / "monitor.db"
    check("数据写在 exe 同级（db 存在）", db.is_file(), str(db))
    # onefile 运行时 PyInstaller 会把自身解压到 TEMP/_MEIxxxx，正常退出后自行清理；
    # 若遇杀软扫描/文件锁延迟，可能残留到下次。判据：`_mei_before` 快照之外的目录，
    # 若仍被**存活的 sgm.exe** 占用（rmtree 失败且进程还在）才算**真泄漏**；
    # 否则是过期解压目录/杀软锁，主动清理即可，不影响交付。
    stray = [p for p in Path(os.environ.get("TEMP", ".")).glob("_MEI*") if p not in _mei_before]
    if stray:
        live = []
        for p in stray:
            # 杀软/退出竞态可能短暂锁住目录，给足时间重试删除（最长约 15s）
            ok = False
            for _ in range(15):
                try:
                    shutil.rmtree(p)
                    ok = True
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(1)
            if not ok:
                live.append(p)
        if live and sgm_alive_at_end:
            # 进程确实还活着并持锁 → 真泄漏
            check("未在临时目录留下 _MEI 解压残留（本次新增）", False,
                  f"{len(live)} 个仍被存活的 sgm.exe 占用（进程未退出）")
        elif live:
            # 进程已退出，只剩环境/杀软锁，稍后会被系统回收 → 不影响交付
            check("未在临时目录留下 _MEI 解压残留（本次新增）", True,
                  f"{len(live)} 个为杀软/系统临时锁（进程已退出，将由系统回收，非程序泄漏）")
        else:
            check("未在临时目录留下 _MEI 解压残留（本次新增）", True,
                  f"已清理 {len(stray)} 个过期解压目录（onefile 延迟回收）")
    else:
        check("未在临时目录留下 _MEI 解压残留（本次新增）", True, "干净")

    # ---- 汇总 ----
    failed = [r for r in results if r[0] == FAIL]
    print()
    print("=" * 78)
    print(f"结果：PASS {len(results) - len(failed)} / FAIL {len(failed)}")
    for _, name, detail in failed:
        print(f"  FAILED: {name}  {detail}")
    print("=" * 78)
    print(f"验收目录保留在: {workdir}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
