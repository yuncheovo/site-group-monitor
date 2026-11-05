"""运行类子命令：``check`` / ``report`` / ``schedule`` / ``serve`` / ``analyze-snapshot`` / ``system-status``。

设计原则（对应架构文档 §7.7）：CLI 与 Web 共用同一 Service 层，此处只做参数解析与结果渲染。
"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from typing import Any

import click

from site_group_monitor.cli.output import echo_error, echo_ok, echo_warn
from site_group_monitor.config.settings import Settings
from site_group_monitor.core.baidu_parser import decide, parse
from site_group_monitor.models.enums import RunMode
from site_group_monitor.utils.progress import NullProgress, RunProgress
from site_group_monitor.utils.timeutil import duration_human, today_str
from site_group_monitor.web.deps import get_container


# ================================================================ check
@click.command("check", help="执行一轮检测")
@click.option("--full", "mode", flag_value="full", default=True, help="全量检测（默认）")
@click.option("--incremental", "mode", flag_value="incremental", help="增量抽样检测")
@click.option("--domain", "domains", multiple=True, help="只检测指定域名（可重复）")
@click.option("--concurrency", type=int, default=None, help="覆盖全局并发数")
@click.option("--resume", is_flag=True, help="跳过当日已有快照的站点")
@click.option("--with-report", is_flag=True, help="检测完成后继续跑 diff + 报告 + 邮件")
@click.option("--notify", is_flag=True, help="配合 --with-report：允许发送邮件")
@click.pass_context
def check(ctx: click.Context, mode: str, domains: tuple[str, ...], concurrency: int | None,
          resume: bool, with_report: bool, notify: bool) -> None:
    """执行一轮站点检测。"""
    container = get_container()
    settings: Settings = container.settings
    quiet = bool(ctx.obj.get("quiet")) if ctx.obj else False

    if with_report:
        container.audit.log("cli", "pipeline.trigger", today_str(settings.timezone_name()),
                            {"mode": mode, "notify": notify})
        result = container.scheduler.run_pipeline(
            mode=RunMode(mode), do_notify=bool(notify), do_export=True, do_cleanup=False
        )
        _echo_pipeline(result)
        return

    progress = NullProgress() if quiet else RunProgress(total=0, title="站群检测")
    summary = asyncio.run(
        container.check.run_all(
            RunMode(mode),
            domains=list(domains) or None,
            concurrency=concurrency,
            resume=resume,
            progress_cb=progress.as_callback(),
        )
    )
    progress.finish("检测完成")
    container.audit.log("cli", "check.run", summary.task_id, summary.to_dict())
    echo_ok(
        f"检测完成：{summary.status}｜总数 {summary.total}｜收录 {summary.indexed}｜"
        f"未收录 {summary.not_indexed}｜查询失败 {summary.unknown}｜失败 {summary.failed}｜"
        f"耗时 {duration_human(summary.duration_ms)}"
    )
    if summary.unknown:
        echo_warn(f"有 {summary.unknown} 个站点查询失败（验证码/网络/解析），请查看证据目录 {settings.evidence_dir}")


# ================================================================ report
@click.command("report", help="生成每日报告（HTML/Excel）")
@click.option("--date", "stat_date", default="today", help="日期 YYYY-MM-DD，或用 today / yesterday")
@click.option("--format", "fmt", default="all", type=click.Choice(["html", "xlsx", "all"]))
@click.option("--notify", is_flag=True, help="生成后尝试邮件推送")
def report(stat_date: str, fmt: str, notify: bool) -> None:
    """生成/导出每日报告。"""
    container = get_container()
    date_key = _resolve_date(stat_date, container.settings)
    paths = container.report.save(date_key, fmt=fmt)
    echo_ok(f"报告已生成 {date_key}：{json.dumps(paths, ensure_ascii=False)}")
    if notify:
        daily = container.report.build_daily(date_key)
        mailer_enabled = _notify(container, daily)
        if not mailer_enabled:
            echo_warn("邮件未发送（SMTP 未启用或配置不完整），报告已保存在本地")
    container.audit.log("cli", "report.export", date_key, {"fmt": fmt, "paths": paths})


# ================================================================ schedule
@click.command("schedule", help="查看/注册每日调度（常驻需配合 serve）")
@click.option("--time", "daily_time", default=None, help="每日触发时间 HH:MM")
@click.option("--jitter", "jitter_minutes", type=int, default=None, help="抖动分钟数")
@click.option("--run-now", is_flag=True, help="立即执行一次流水线")
def schedule(daily_time: str | None, jitter_minutes: int | None, run_now: bool) -> None:
    """查看或注册每日调度任务。"""
    container = get_container()
    info = container.scheduler.add_daily_job(daily_time, jitter_minutes)
    echo_ok(f"已注册每日任务：{info['scheduled_time']}（随机抖动 {info['jitter_minutes']} 分钟，时区 {info['timezone']}）")
    status = container.scheduler.status()
    click.echo(json.dumps(status, ensure_ascii=False, indent=2, default=str))
    if run_now:
        result = container.scheduler.trigger_now(RunMode.FULL, do_notify=False, do_export=True,
                                                 do_cleanup=False)
        _echo_pipeline(result)


# ================================================================ serve
@click.command("serve", help="启动本地仪表盘（仅 127.0.0.1）+ 内嵌调度器")
@click.option("--host", default=None, help="监听地址（安全限制：仅允许本地）")
@click.option("--port", type=int, default=None, help="端口")
@click.option("--no-scheduler", is_flag=True, help="不启动内嵌调度器")
def serve(host: str | None, port: int | None, no_scheduler: bool) -> None:
    """启动本地 Web 仪表盘。"""
    container = get_container()
    settings: Settings = container.settings
    bind_host = host or settings.web.host
    if bind_host not in ("127.0.0.1", "localhost", "::1"):
        echo_warn("出于安全考虑，仅允许绑定 127.0.0.1，已自动纠正")
        bind_host = "127.0.0.1"
    bind_port = int(port or settings.web.port)

    if not no_scheduler and bool(getattr(settings.scheduler, "enabled", True)):
        container.scheduler.start_background()
        try:
            registered = container.scheduler.register_proxy_api_sources(
                container.repos, run_immediately=True
            )
            if registered:
                echo_ok(f"已注册 {registered} 个代理 API 源刷新任务")
        except Exception as exc:  # noqa: BLE001
            echo_warn(f"代理 API 源任务注册失败：{exc}")
        echo_ok("内嵌调度器已启动")

    click.echo(f"仪表盘地址：http://{bind_host}:{bind_port}  （Ctrl+C 退出）")
    try:
        import uvicorn

        uvicorn.run(container_app(), host=bind_host, port=bind_port, log_level="info")
    except KeyboardInterrupt:  # pragma: no cover - 交互退出
        echo_warn("已收到中断信号，正在退出…")
    finally:
        container.scheduler.shutdown(wait=False)


def container_app() -> Any:
    """构造 FastAPI 应用（复用既有容器，避免重复初始化数据库）。

    Returns:
        FastAPI 应用实例。
    """
    from site_group_monitor.web.app import create_app

    return create_app(get_container())


# ================================================================ gui
@click.command("gui", help="打开原生桌面图形界面（与双击 exe 等效）")
def gui() -> None:
    """打开原生桌面窗口（pywebview + WebView2 封装的 ECharts 仪表盘）。"""
    from site_group_monitor.gui.app import run_gui

    raise SystemExit(run_gui())


# ================================================================ analyze-snapshot
@click.command("analyze-snapshot", help="离线重跑百度结果页解析（HTML 或 .html.gz）")
@click.argument("path", required=True)
@click.option("--domain", required=True, help="该结果页对应的域名")
@click.option("--json-output", is_flag=True, help="以 JSON 输出解析结果")
def analyze_snapshot(path: str, domain: str, json_output: bool) -> None:
    """对已归档的百度结果页做离线解析（不联网，用于回归与人工复核）。"""
    target = Path(path)
    if not target.is_file():
        echo_error(f"文件不存在：{path}")
        return
    raw = target.read_bytes()
    if target.suffix == ".gz":
        raw = gzip.decompress(raw)
    html = raw.decode("utf-8", errors="replace")
    outcome = parse(html, domain)
    result = decide(outcome, domain)
    if json_output:
        click.echo(
            json.dumps(
                {
                    "domain": domain,
                    "status": str(result.status),
                    "reason": str(result.reason),
                    "confidence": result.confidence,
                    "result_count": result.result_count,
                    "top_url": result.top_url,
                    "detail": outcome.model_dump(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    echo_ok(f"{domain}: {result.status} / {result.reason}（置信度 {result.confidence}）")
    click.echo(
        f"  结果数={outcome.nums_count} 命中条目={len(outcome.hits)} "
        f"域名匹配={outcome.domain_matched} 验证码={outcome.has_captcha} "
        f"空结果文案={outcome.has_empty_hint} 页面长度={outcome.page_len}"
    )


# ================================================================ system-status
@click.command("system-status", help="查看系统状态（调度/检测进度/库/邮件）")
def system_status() -> None:
    """输出系统状态概览。"""
    container = get_container()
    from site_group_monitor.notify.mailer import MailNotifier

    mailer = MailNotifier(container.settings, container.logger, container.audit)
    click.echo(json.dumps(
        {
            "scheduler": container.scheduler.status(),
            "progress": container.check.progress_snapshot(),
            "db": container.db.stats(),
            "mail": {"enabled": mailer.is_enabled(), "reason": mailer.reason_disabled()},
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    ))


# ================================================================ 辅助
def _echo_pipeline(result: dict[str, Any]) -> None:
    """打印流水线结果。

    Args:
        result: 流水线结果字典。
    """
    if result.get("status") == "skipped":
        echo_warn(str(result.get("message")))
        return
    if result.get("status") == "failed":
        echo_error(f"流水线失败：{result.get('message')}")
        return
    echo_ok(
        f"流水线完成：日期 {result.get('stat_date')}｜快照 {result.get('snapshots')}｜"
        f"变化事件 {result.get('change_events')}｜未知持续 {result.get('unknown_events')}｜"
        f"健康事件 {result.get('health_events')}｜告警 {result.get('alerts')}"
    )
    paths = result.get("paths") or {}
    if paths:
        click.echo(f"  报告：{json.dumps(paths, ensure_ascii=False)}")
    if result.get("mail_skipped_reason"):
        echo_warn(f"  邮件未发送：{result.get('mail_skipped_reason')}")


def _notify(container: Any, daily_report: Any) -> bool:
    """尝试发送邮件。

    Args:
        container: 服务容器。
        daily_report: 日报聚合体。

    Returns:
        是否实际发送成功。
    """
    from site_group_monitor.notify.mailer import MailNotifier

    mailer = MailNotifier(container.settings, container.logger, container.audit)
    if not mailer.is_enabled():
        return False
    return mailer.send_daily(daily_report)


def _resolve_date(stat_date: str, settings: Settings) -> str:
    """解析日期别名。

    Args:
        stat_date: 输入日期或别名。
        settings: 配置对象（时区）。

    Returns:
        日期键字符串。

    Raises:
        click.BadParameter: 输入既不是别名也不是合法 ``YYYY-MM-DD``。
    """
    from site_group_monitor.utils.timeutil import is_date_key, shift_days, today_str

    text = str(stat_date or "").strip().lower()
    today = today_str(settings.timezone_name())
    if text in ("today", "今天"):
        return today
    if text in ("yesterday", "昨天"):
        return shift_days(today, -1)
    if is_date_key(text):
        return text
    # 不要直接把非法输入透传到下游——那样会在 strptime 里抛裸堆栈，用户只看到 Traceback
    raise click.BadParameter(
        f"{stat_date!r} 不是合法日期，请用 today / yesterday 或 2026-10-27 这种格式",
        param_hint="--date",
    )
