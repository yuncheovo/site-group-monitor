"""SMTP 邮件推送（R-008）。

能力：
    * SMTP_SSL（465）与 STARTTLS（587）双模式；
    * Jinja2 渲染「图文」HTML 正文（摘要卡片 + 告警清单 + 掉收录清单）；
    * 失败按 ``smtp.retry`` 与 ``retry_backoff_sec`` 退避重试；
    * 发送结果写入审计日志（``actor=system``）；
    * ``smtp.enabled=false`` 或凭据缺失时**自动禁用**，不影响本地报告产出。
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl as ssl_module
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape

from site_group_monitor.models.entities import DailyReport
from site_group_monitor.utils.errors import NotifyError
from site_group_monitor.utils.netutil import mask_secret

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


class MailNotifier:
    """邮件通知器。"""

    def __init__(self, settings: Any, logger: Any | None = None, audit: Any | None = None) -> None:
        """构造通知器。

        Args:
            settings: 配置对象。
            logger: 日志记录器。
            audit: 审计服务（写发送结果）。
        """
        self.settings = settings
        self.logger = logger
        self.audit = audit
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "j2"]),
        )

    # ---------------------------------------------------------------- 状态
    def is_enabled(self) -> bool:
        """是否已可用（启用开关 + 必要凭据齐全）。

        Returns:
            可用返回 True。
        """
        smtp = self.settings.smtp
        if not bool(getattr(smtp, "enabled", False)):
            return False
        if not (smtp.host and smtp.user and smtp.password):
            return False
        recipients = self._recipients()
        return bool(recipients)

    def reason_disabled(self) -> str:
        """返回不可用的原因（用于日志与仪表盘提示）。

        Returns:
            原因字符串。
        """
        smtp = self.settings.smtp
        if not bool(getattr(smtp, "enabled", False)):
            return "邮件推送未启用（smtp.enabled=false）"
        if not (smtp.host and smtp.user and smtp.password):
            return "SMTP 凭据不完整（host/user/password）"
        if not self._recipients():
            return "未配置收件人（smtp.recipients）"
        return ""

    def _recipients(self) -> list[str]:
        """收件人列表（过滤空值）。

        Returns:
            收件人邮箱列表。
        """
        raw = list(getattr(self.settings.smtp, "recipients", []) or [])
        return [str(item).strip() for item in raw if str(item).strip()]

    def _sender(self) -> str:
        """发件人地址（缺省退回 user）。"""
        smtp = self.settings.smtp
        return str(smtp.sender or smtp.user or "")

    # ---------------------------------------------------------------- 发送
    def send_daily(self, report: DailyReport) -> bool:
        """发送每日摘要邮件（同步接口，内部含退避重试）。

        Args:
            report: 日报聚合体。

        Returns:
            发送成功返回 True。
        """
        if not self.is_enabled():
            reason = self.reason_disabled()
            if self.logger is not None:
                self.logger.info("mailer: 跳过发送 —— %s", reason)
            return False
        subject = f"{self.settings.smtp.subject_prefix} {report.stat_date} 站群监控日报"
        context = {
            "report": report,
            "index_rate_pct": round(report.index_rate * 100, 2),
            "prev_rate_pct": round((report.prev_rate or 0) * 100, 2),
        }
        html = self._render("mail.html.j2", context)
        return self._send_with_retry(subject, html)

    def send_alert(self, alerts: Sequence[dict[str, Any]], stat_date: str = "") -> bool:
        """发送实时告警邮件。

        Args:
            alerts: 告警字典列表。
            stat_date: 日期键。

        Returns:
            发送成功返回 True。
        """
        if not alerts:
            return False
        if not self.is_enabled():
            if self.logger is not None:
                self.logger.info("mailer: 跳过告警 —— %s", self.reason_disabled())
            return False
        critical = [item for item in alerts if item.get("level") == "critical"]
        subject = (
            f"{self.settings.smtp.subject_prefix} 告警 {len(alerts)} 条"
            f"（严重 {len(critical)}）{stat_date}"
        )
        html = self._render(
            "mail.html.j2",
            {
                "alerts": list(alerts),
                "stat_date": stat_date,
                "report": None,
                "index_rate_pct": 0.0,
                "prev_rate_pct": 0.0,
            },
        )
        return self._send_with_retry(subject, html)

    async def send_daily_async(self, report: DailyReport) -> bool:
        """异步版本：把阻塞的 SMTP 调用放到线程池。

        Args:
            report: 日报聚合体。

        Returns:
            发送成功返回 True。
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.send_daily, report)

    def test_connection(self) -> tuple[bool, str]:
        """测试 SMTP 连通性（不发送邮件）。

        Returns:
            ``(是否成功, 说明)``。
        """
        if not self.is_enabled():
            return False, self.reason_disabled()
        try:
            client = self._connect()
            try:
                client.noop()
            finally:
                with _swallow():
                    client.quit()
            return True, "SMTP 连接正常"
        except Exception as exc:  # noqa: BLE001
            return False, f"SMTP 连接失败: {exc}"

    # ---------------------------------------------------------------- 内部
    def _connect(self) -> Any:
        """建立 SMTP 连接并完成登录。

        Returns:
            SMTP / SMTP_SSL 客户端对象。
        """
        smtp_cfg = self.settings.smtp
        timeout = int(getattr(smtp_cfg, "timeout_sec", 20) or 20)
        if getattr(smtp_cfg, "use_ssl", True):
            client = smtplib.SMTP_SSL(smtp_cfg.host, int(smtp_cfg.port or 465), timeout=timeout,
                                      context=ssl_module.create_default_context())
        else:
            client = smtplib.SMTP(smtp_cfg.host, int(smtp_cfg.port or 25), timeout=timeout)
            if getattr(smtp_cfg, "starttls", False):
                client.starttls(context=ssl_module.create_default_context())
        if smtp_cfg.user and smtp_cfg.password:
            client.login(str(smtp_cfg.user), str(smtp_cfg.password))
        return client

    def _send_with_retry(self, subject: str, html: str) -> bool:
        """按配置重试发送。

        Args:
            subject: 邮件主题。
            html: HTML 正文。

        Returns:
            成功返回 True。
        """
        import time

        attempts = max(1, int(getattr(self.settings.smtp, "retry", 3) or 3))
        backoff = list(getattr(self.settings.smtp, "retry_backoff_sec", []) or [30, 90, 180])
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                self._send_once(subject, html)
                if self.logger is not None:
                    self.logger.info("mailer: 邮件已发送（第 %d 次尝试）subject=%s", attempt, subject)
                self._audit(True, subject, attempt)
                return True
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if self.logger is not None:
                    self.logger.warning("mailer: 第 %d/%d 次发送失败: %s", attempt, attempts, exc)
                if attempt < attempts:
                    time.sleep(float(backoff[min(attempt - 1, len(backoff) - 1)]))
        self._audit(False, subject, attempts, last_error)
        if self.logger is not None:
            self.logger.error("mailer: 邮件最终发送失败: %s", last_error)
        return False

    def _send_once(self, subject: str, html: str) -> None:
        """发送一次邮件。

        Args:
            subject: 主题。
            html: HTML 正文。
        """
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self._sender()
        message["To"] = ", ".join(self._recipients())
        message.set_content("请使用支持 HTML 的邮件客户端查看本报告。")
        message.add_alternative(html, subtype="html")
        client = self._connect()
        try:
            client.send_message(message)
        finally:
            with _swallow():
                client.quit()

    def _render(self, template_name: str, context: dict[str, Any]) -> str:
        """渲染邮件模板。

        Args:
            template_name: 模板文件名。
            context: 上下文字典。

        Returns:
            HTML 文本。
        """
        try:
            template = self.env.get_template(template_name)
            return template.render(**context)
        except Exception as exc:  # noqa: BLE001 - 模板异常降级为简单文本
            if self.logger is not None:
                self.logger.warning("mailer: 模板渲染失败，降级为纯文本: %s", exc)
            report: DailyReport | None = context.get("report")
            if report is None:
                return f"<html><body><p>{context.get('stat_date', '')} 告警 {len(context.get('alerts') or [])} 条</p></body></html>"
            return (
                f"<html><body><h3>{report.stat_date} 站群监控日报</h3>"
                f"<p>收录率 {report.index_rate * 100:.2f}%｜新增 {len(report.new_indexed)}｜"
                f"掉收录 {len(report.lost_indexed)}｜查询失败 {len(report.need_manual)}</p></body></html>"
            )

    def _audit(self, ok: bool, subject: str, attempts: int, error: str = "") -> None:
        """写发送结果到审计日志。

        Args:
            ok: 是否成功。
            subject: 主题。
            attempts: 尝试次数。
            error: 错误信息。
        """
        if self.audit is None:
            return
        self.audit.log(
            "system",
            "notify.mail",
            subject,
            {
                "ok": ok,
                "attempts": attempts,
                "recipients": self._recipients(),
                "user_masked": mask_secret(str(self.settings.smtp.user or "")),
                "error": error,
            },
            level="info" if ok else "warning",
        )


class _swallow:
    """上下文管理器：忽略退出时的异常。"""

    def __enter__(self) -> "_swallow":
        """进入上下文。"""
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        """退出并吞掉异常。"""
        return True


def raise_if_missing_credentials(settings: Any) -> None:
    """校验 SMTP 凭据完整性，缺失时抛出错误（供 CLI 显式提示）。

    Args:
        settings: 配置对象。

    Raises:
        NotifyError: 凭据不完整。
    """
    smtp = settings.smtp
    if not (smtp.host and smtp.user and smtp.password):
        raise NotifyError("SMTP 凭据不完整，请先配置 smtp.host/user/password（推荐用 SGM_SMTP__PASSWORD 注入）")
