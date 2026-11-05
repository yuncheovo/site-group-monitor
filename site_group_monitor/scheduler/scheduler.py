"""定时调度器 + 每日流水线（check → 快照/diff → 报告/导出 → 告警/邮件 → 清理备份）。

要点（对应架构文档 §4.1 与 R-009）：
    * ``BackgroundScheduler`` 进程内调度，``cron`` 每日 ``daily_time`` 触发，叠加 0~``jitter_minutes`` 随机抖动；
    * **任务互斥锁**：并发触发只会执行一份（``threading.Lock`` + ``CheckService.is_running`` 双重保护）；
    * 支持手动触发 ``trigger_now(mode)``（CLI / Web 共用同一函数）；
    * 邮件失败有退避重试、静默时段不发信但照常落库报告。
"""

from __future__ import annotations

import asyncio
import random
import threading
from datetime import datetime
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from site_group_monitor.models.enums import RunMode
from site_group_monitor.notify.mailer import MailNotifier
from site_group_monitor.utils.timeutil import parse_hhmm, today_str, utcnow_iso

JOB_ID_DAILY = "sgm_daily_pipeline"

# 代理 API 源自动刷新任务的 id 前缀（每个源一个）
JOB_ID_PROXY_API_PREFIX = "sgm_proxy_api_"


class _ProxyApiJob:
    """代理 API 源刷新任务封装（避免 APScheduler 直接持有重对象）。"""

    def __init__(self, source_id: int, repos: Any, settings: Any, logger: Any) -> None:
        self.source_id = source_id
        self.repos = repos
        self.settings = settings
        self.logger = logger

    def __call__(self) -> None:
        from site_group_monitor.core.proxy_api_fetcher import fetch_and_store

        source = self.repos.proxies_api.get_by_id(self.source_id)
        if source is None or not source.enabled:
            return
        try:
            prune = "delete" if getattr(source, "rotate_per_check", True) else "disable"
            result = fetch_and_store(source, self.repos, self.settings, prune=prune)
            if result.get("ok"):
                from site_group_monitor.core.proxy_probe import probe_source_proxies

                probe_source_proxies(int(source.id), self.repos)
        except Exception as exc:  # noqa: BLE001 - 调度任务异常不应影响进程
            if self.logger is not None:
                self.logger.exception("scheduler: 代理 API 源 %s 刷新失败 %s", self.source_id, exc)


class Scheduler:
    """定时调度器封装。"""

    def __init__(
        self,
        services: dict[str, Any],
        settings: Any,
        logger: Any | None = None,
    ) -> None:
        """构造调度器。

        Args:
            services: 服务字典，需含 ``check`` / ``diff`` / ``report`` / ``alert`` / ``audit``。
            settings: 配置对象。
            logger: 日志记录器。
        """
        self.services = services
        self.settings = settings
        self.logger = logger
        self._scheduler = BackgroundScheduler(timezone=settings.timezone_name())
        self._lock = threading.Lock()
        self._mailer: MailNotifier | None = None
        self._last_result: dict[str, Any] = {}
        self._running_since: str = ""
        self._jitter_applied: int = 0

    # ---------------------------------------------------------------- 生命周期
    @property
    def mailer(self) -> MailNotifier:
        """邮件通知器（惰性构造）。"""
        if self._mailer is None:
            self._mailer = MailNotifier(self.settings, self.logger, self.services.get("audit"))
        return self._mailer

    def add_daily_job(self, daily_time: str | None = None, jitter_minutes: int | None = None) -> dict[str, Any]:
        """注册每日任务。

        Args:
            daily_time: ``HH:MM`` 时间；缺省取配置。
            jitter_minutes: 抖动分钟数；缺省取配置。

        Returns:
            任务信息字典（含实际触发时间与抖动分钟数）。
        """
        time_text = daily_time or str(self.settings.scheduler.daily_time)
        try:
            hour, minute = parse_hhmm(time_text)
        except ValueError as exc:
            raise ValueError(
                f"每日检测时间配置有误：{exc}。"
                f"请检查 scheduler.daily_time（当前为 {time_text!r}），格式应为 HH:MM，如 08:00。"
            ) from exc
        jitter = int(jitter_minutes if jitter_minutes is not None
                     else getattr(self.settings.scheduler, "jitter_minutes", 30) or 0)
        jitter_value = random.randint(0, max(0, jitter))
        # 抖动后重新换算时分：必须先算总分钟数再拆，否则 minute 被覆盖后会算错小时进位
        total_minutes = (hour * 60 + minute + jitter_value) % (24 * 60)
        hour, minute = divmod(total_minutes, 60)
        self._jitter_applied = jitter_value

        trigger = CronTrigger(
            hour=hour, minute=minute, timezone=self.settings.timezone_name()
        )
        self._scheduler.add_job(
            self.run_daily_pipeline,
            trigger=trigger,
            id=JOB_ID_DAILY,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            # misfire_grace_time 是 add_job 的参数，不能传给 CronTrigger
            misfire_grace_time=int(getattr(self.settings.scheduler, "misfire_grace_sec", 3600)),
            name="每日站群检测流水线",
        )
        info = {
            "job_id": JOB_ID_DAILY,
            "scheduled_time": f"{hour:02d}:{minute:02d}",
            "jitter_minutes": jitter_value,
            "timezone": self.settings.timezone_name(),
        }
        if self.logger is not None:
            self.logger.info("scheduler: 已注册每日任务 %s（含 %d 分钟抖动）", info["scheduled_time"], jitter_value)
        return info

    def start_background(self) -> dict[str, Any]:
        """启动后台调度线程。

        Returns:
            调度状态字典。
        """
        self.add_daily_job()
        if not self._scheduler.running:
            self._scheduler.start()
        return self.status()

    def shutdown(self, wait: bool = False) -> None:
        """停止调度器。

        Args:
            wait: 是否等待正在执行的任务结束。
        """
        try:
            if self._scheduler.running:
                self._scheduler.shutdown(wait=wait)
        except Exception:  # noqa: BLE001 - 关闭异常不影响退出
            pass

    def status(self) -> dict[str, Any]:
        """返回调度器状态（Web 设置页展示）。

        Returns:
            状态字典。
        """
        jobs: list[dict[str, Any]] = []
        for job in self._scheduler.get_jobs():
            jobs.append(
                {
                    "id": job.id,
                    "name": job.name,
                    "next_run_time": str(job.next_run_time),
                    "trigger": str(job.trigger),
                }
            )
        return {
            "running": bool(self._scheduler.running),
            "jobs": jobs,
            "enabled": bool(getattr(self.settings.scheduler, "enabled", True)),
            "daily_time": str(self.settings.scheduler.daily_time),
            "jitter_minutes": int(getattr(self.settings.scheduler, "jitter_minutes", 30) or 0),
            "last_jitter": self._jitter_applied,
            "last_result": self._last_result,
            "pipeline_running": bool(self._running_since),
            "running_since": self._running_since,
        }

    def next_run_time(self) -> datetime | None:
        """下一次触发时间。"""
        for job in self._scheduler.get_jobs():
            if job.id == JOB_ID_DAILY:
                return job.next_run_time
        return None

    # ---------------------------------------------------------------- 触发
    def trigger_now(self, mode: RunMode | str = RunMode.FULL, *, do_notify: bool = True,
                    do_export: bool = True, do_cleanup: bool = True) -> dict[str, Any]:
        """立即执行一次流水线（可重入安全，重复触发互斥）。

        Args:
            mode: 检测模式。
            do_notify: 是否发邮件。
            do_export: 是否导出报告。
            do_cleanup: 是否做保留期清理与备份。

        Returns:
            流水线结果字典。
        """
        return self.run_pipeline(
            mode=mode, do_notify=do_notify, do_export=do_export, do_cleanup=do_cleanup
        )

    def run_daily_pipeline(self) -> dict[str, Any]:
        """APScheduler 定时入口。

        Returns:
            流水线结果字典。
        """
        return self.run_pipeline(mode=RunMode.FULL, do_notify=True, do_export=True, do_cleanup=True)

    def run_pipeline(
        self,
        mode: RunMode | str = RunMode.FULL,
        *,
        do_notify: bool = True,
        do_export: bool = True,
        do_cleanup: bool = True,
        stat_date: str | None = None,
        domains: list[str] | None = None,
    ) -> dict[str, Any]:
        """执行一次完整每日流水线。

        Args:
            mode: 检测模式。
            do_notify: 是否发邮件。
            do_export: 是否导出 HTML/Excel。
            do_cleanup: 是否清理过期数据与备份。
            stat_date: 指定日期键（补跑场景）。

        Returns:
            结果字典（含 run/report/alerts/paths）。
        """
        acquired = self._lock.acquire(blocking=False)
        if not acquired:
            message = "已有流水线在执行，本次触发被忽略（互斥）"
            if self.logger is not None:
                self.logger.warning("scheduler: %s", message)
            return {"status": "skipped", "message": message}
        self._running_since = utcnow_iso()
        try:
            stat_date = stat_date or today_str(self.settings.timezone_name())
            check_service = self.services.get("check")
            if check_service is not None and getattr(check_service, "is_running", False):
                return {"status": "skipped", "message": "CheckService 正在运行，跳过本次触发"}

            # ① 检测
            run_summary = (
                self._await(check_service.run_all(mode, domains=domains)) if check_service else None
            )

            # ② 快照
            diff_service = self.services.get("diff")
            task_id = run_summary.task_id if run_summary else ""
            results = []
            if task_id:
                results = self.services.get("repos").results.list_by_task(task_id)
            snapshots = diff_service.build_daily_snapshots(stat_date, results) if diff_service else []

            # ③ diff / 事件
            change_events: list[Any] = []
            unknown_events: list[Any] = []
            health_events: list[Any] = []
            if diff_service:
                change_events = diff_service.diff_yesterday(stat_date)
                unknown_events = diff_service.check_unknown_persistent(stat_date)
                health_events = diff_service.check_health_events(stat_date)

            # ④ 报告 + 导出
            report_service = self.services.get("report")
            report = report_service.build_daily(stat_date) if report_service else None
            paths: dict[str, str] = {}
            if report_service is not None and do_export:
                paths = report_service.save(stat_date, fmt="all")

            # ⑤ 告警
            alerts: list[dict[str, Any]] = []
            alert_service = self.services.get("alert")
            if alert_service is not None and report is not None:
                alerts = alert_service.evaluate(report)
                if alerts:
                    alert_service.mark_notified(alerts, stat_date)

            # ⑥ 通知（SMTP 未启用则自动跳过）
            notified = False
            mail_reason = ""
            if do_notify and report is not None:
                if self.mailer.is_enabled():
                    if alert_service is not None and alert_service.in_quiet_hours():
                        mail_reason = "静默时段，邮件已跳过（报告已落库）"
                    else:
                        notified = self.mailer.send_daily(report)
                        if alerts:
                            self.mailer.send_alert(alerts, stat_date)
                else:
                    mail_reason = self.mailer.reason_disabled()

            # ⑦ 清理 + 备份
            cleanup_stats: dict[str, int] = {}
            backup_path = ""
            if do_cleanup and report_service is not None:
                cleanup_stats = report_service.cleanup()
                backup_path = report_service.backup()

            result = {
                "status": "success",
                "stat_date": stat_date,
                "task_id": task_id,
                "run": run_summary.to_dict() if run_summary else {},
                "snapshots": len(snapshots),
                "change_events": len(change_events),
                "unknown_events": len(unknown_events),
                "health_events": len(health_events),
                "alerts": len(alerts),
                "notified": notified,
                "mail_skipped_reason": mail_reason,
                "paths": paths,
                "cleanup": cleanup_stats,
                "backup": backup_path,
                "index_rate": report.index_rate if report else 0.0,
            }
            self._last_result = result
            if self.logger is not None:
                self.logger.info(
                    "scheduler: 流水线完成 %s（收录率 %.2f%%，新增/掉收录=%d/%d，告警=%d）",
                    stat_date,
                    (report.index_rate if report else 0.0) * 100,
                    len(report.new_indexed) if report else 0,
                    len(report.lost_indexed) if report else 0,
                    len(alerts),
                )
            audit = self.services.get("audit")
            if audit is not None:
                audit.log("scheduler", "pipeline.run", stat_date, result)
            return result
        except Exception as exc:  # noqa: BLE001 - 调度层吞掉异常，保证进程常驻
            if self.logger is not None:
                self.logger.exception("scheduler: 流水线异常 %s", exc)
            return {"status": "failed", "message": str(exc)}
        finally:
            self._running_since = ""
            self._lock.release()

    # ---------------------------------------------------------------- 辅助
    @staticmethod
    def _await(coro: Any) -> Any:
        """在非事件循环线程里安全地执行协程。

        Args:
            coro: 待执行的协程对象。

        Returns:
            协程返回值。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result()
        return asyncio.run(coro)

    def set_job_enabled(self, enabled: bool) -> dict[str, Any]:
        """启用/暂停每日任务。

        Args:
            enabled: 是否启用。

        Returns:
            调度状态字典。
        """
        if enabled:
            self._scheduler.resume_job(JOB_ID_DAILY)
        else:
            self._scheduler.pause_job(JOB_ID_DAILY)
        return self.status()

    def register_manual_task(self, callback: Callable[[], Any], interval_sec: int = 0) -> None:
        """预留：注册一次性手动任务（供未来扩展）。

        Args:
            callback: 回调函数。
            interval_sec: 0 表示立即执行一次（在调度线程内）。
        """
        if interval_sec <= 0:
            self._scheduler.add_job(callback, trigger="date", id="sgm_manual_once",
                                    replace_existing=True, coalesce=True)
        else:
            self._scheduler.add_job(callback, trigger="interval", seconds=interval_sec,
                                    id="sgm_manual_interval", replace_existing=True, coalesce=True)

    def register_proxy_api_sources(self, repos: Any, run_immediately: bool = False) -> int:
        """为每个已启用的代理 API 源注册一个定时刷新任务。

        Args:
            repos: 仓储容器（需含 ``proxies_api``）。
            run_immediately: 是否立刻跑一轮（进程启动时用；创建源时由前端显式刷新）。

        Returns:
            成功注册的任务数量。
        """
        count = 0
        try:
            sources = repos.proxies_api.list_all(include_disabled=False)
        except Exception as exc:  # noqa: BLE001 - 仓储异常不应阻塞启动
            if self.logger is not None:
                self.logger.warning("scheduler: 加载代理 API 源失败 %s", exc)
            return 0
        for source in sources:
            job_id = f"{JOB_ID_PROXY_API_PREFIX}{source.id}"
            try:
                job_kwargs: dict[str, Any] = {
                    "id": job_id,
                    "replace_existing": True,
                    "max_instances": 1,
                    "coalesce": True,
                }
                if run_immediately:
                    from datetime import timezone

                    job_kwargs["next_run_time"] = datetime.now(timezone.utc)
                self._scheduler.add_job(
                    _ProxyApiJob(source.id, repos, self.settings, self.logger),
                    trigger="interval",
                    minutes=max(1, int(source.refresh_interval_min or 30)),
                    **job_kwargs,
                )
                count += 1
            except Exception as exc:  # noqa: BLE001 - 单个源失败不影响其他
                if self.logger is not None:
                    self.logger.warning("scheduler: 注册代理 API 源 %s 失败 %s", source.id, exc)
        if count and self.logger is not None:
            self.logger.info("scheduler: 已注册 %d 个代理 API 源刷新任务", count)
        return count

    def unregister_proxy_api_sources(self) -> None:
        """移除全部代理 API 源刷新任务（best-effort）。"""
        try:
            for job in self._scheduler.get_jobs():
                if str(job.id).startswith(JOB_ID_PROXY_API_PREFIX):
                    try:
                        self._scheduler.remove_job(job.id)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
