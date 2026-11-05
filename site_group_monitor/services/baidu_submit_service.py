"""百度搜索资源平台浏览器提交服务（不包含 API/token 模式）。"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import quote

from site_group_monitor.core.local_browser import LocalBrowser
from site_group_monitor.models.entities import Site

SUBMIT_PAGE = "https://ziyuan.baidu.com/linksubmit/url?sitename={}"


@dataclass
class SubmitState:
    status: str = "idle"
    total: int = 0
    done: int = 0
    success: int = 0
    failed: int = 0
    current: str = ""
    message: str = ""
    results: list[dict[str, str]] = field(default_factory=list)


class BaiduSubmitService:
    """通过可见浏览器把未收录站点首页提交到百度资源平台。"""

    def __init__(self, settings: Any, logger: Any | None = None) -> None:
        self.settings = settings
        self.logger = logger
        self._state = SubmitState()
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            if self._running:
                self._state.message = "正在停止提交任务…"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            data = dict(vars(self._state))
            data["results"] = list(self._state.results)
            data["running"] = self._running
            return data

    async def run(self, sites: Sequence[Site]) -> None:
        if self._running:
            return
        self._cancel.clear()
        self._running = True
        with self._lock:
            self._state = SubmitState(
                status="running", total=len(sites), message="正在打开百度搜索资源平台…"
            )
        browser = LocalBrowser(
            self._browser_settings(), self.logger, profile_name="baidu-submit"
        )
        try:
            for site in sites:
                if self._cancel.is_set():
                    self._set_status("cancelled", "提交任务已取消")
                    return
                await self._submit_site(browser, site)
                if not self._cancel.is_set() and self._state.done < self._state.total:
                    await self._cancelable_delay(6.0)
            if self._cancel.is_set():
                self._set_status("cancelled", "提交任务已取消")
            else:
                final = "success" if self._state.failed == 0 else "partial"
                self._set_status(
                    final,
                    f"提交结束：成功 {self._state.success}，失败 {self._state.failed}",
                )
        except asyncio.CancelledError:
            self._set_status("cancelled", "提交任务已取消")
        except Exception as exc:  # noqa: BLE001
            if self.logger is not None:
                self.logger.exception("百度浏览器提交异常: %s", exc)
            self._set_status("failed", f"提交异常：{exc}")
        finally:
            await browser.close()
            self._running = False

    def _browser_settings(self) -> Any:
        copied = self.settings.model_copy(deep=True)
        cfg = copied.engine.baidu.browser
        cfg.enabled = True
        cfg.headless = False
        cfg.via_proxy = False
        cfg.windows = 1
        cfg.profile_dir = "data/browser-profile"
        return copied

    async def _submit_site(self, browser: LocalBrowser, site: Site) -> None:
        domain = site.domain
        root_url = site.effective_root_url()
        page_url = SUBMIT_PAGE.format(quote(root_url.rstrip("/"), safe=":/"))
        with self._lock:
            self._state.current = domain
            self._state.message = f"正在打开 {domain} 的链接提交页"
        response = await browser.fetch(page_url)
        if not response.ok:
            self._record(domain, False, f"提交页打开失败：{response.error_msg or response.status_code}")
            return

        page = await self._wait_for_form(browser, timeout=300.0)
        if not page.get("hasInput"):
            self._record(domain, False, "等待登录或提交输入框超时")
            return
        if self._cancel.is_set():
            return

        clicked = await browser.evaluate_value(_fill_and_click_script(root_url), timeout=10)
        if not isinstance(clicked, dict) or not clicked.get("clicked"):
            reason = str((clicked or {}).get("message") or "未找到提交按钮") \
                if isinstance(clicked, dict) else "页面操作失败"
            self._record(domain, False, reason)
            return

        await self._cancelable_delay(2.0)
        if self._cancel.is_set():
            return
        page = await self._page_state(browser)
        if page.get("captcha"):
            self._set_status("waiting_captcha", f"{domain} 出现验证码，请在浏览器中手动完成")
            page = await self._wait_until_captcha_done(browser, timeout=300.0)
            if page.get("captcha"):
                self._record(domain, False, "等待手动验证码超时")
                return
            captcha_feedback = f"{page.get('feedback', '')}\n{page.get('body', '')}"
            if not any(word in captcha_feedback for word in ("提交成功", "已提交", "已收录")) \
                    and str(page.get("inputValue") or "").strip():
                # 有些页面通过验证后不会自动恢复原来的点击动作，需要再提交一次。
                await browser.evaluate_value(_fill_and_click_script(root_url), timeout=10)
                await self._cancelable_delay(2.0)
                page = await self._page_state(browser)
        feedback = str(page.get("feedback") or "").strip()
        body = str(page.get("body") or "")
        combined = f"{feedback}\n{body[-1200:]}"
        success = any(word in combined for word in ("提交成功", "已提交", "提交完成", "已收录"))
        error = next(
            (word for word in ("配额已用完", "超出限额", "提交失败", "无权限", "站点未验证") if word in combined),
            "",
        )
        if error:
            self._record(domain, False, feedback or error)
        elif success:
            self._record(domain, True, feedback or "提交成功")
        elif not str(page.get("inputValue") or "").strip():
            # 百度部分版本提交成功后只清空输入框，不一定显示可稳定读取的 toast。
            self._record(domain, True, feedback or "已点击提交，页面未返回明确错误")
        else:
            self._record(domain, False, feedback or "未读取到百度的提交结果")

    async def _wait_for_form(self, browser: LocalBrowser, timeout: float) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        announced = False
        latest: dict[str, Any] = {}
        while not self._cancel.is_set() and asyncio.get_running_loop().time() < deadline:
            latest = await self._page_state(browser)
            if latest.get("hasInput"):
                self._set_status("running", f"登录状态正常，准备提交 {self._state.current}")
                return latest
            if latest.get("loggedOut") and not announced:
                announced = True
                self._set_status("waiting_login", "请在弹出的浏览器中登录百度账号，登录后将自动继续")
            await asyncio.sleep(0.5)
        return latest

    async def _wait_until_captcha_done(self, browser: LocalBrowser, timeout: float) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        latest: dict[str, Any] = {}
        while not self._cancel.is_set() and asyncio.get_running_loop().time() < deadline:
            latest = await self._page_state(browser)
            if not latest.get("captcha"):
                self._set_status("running", f"验证码已完成，继续提交 {self._state.current}")
                return latest
            await asyncio.sleep(0.5)
        return latest

    async def _page_state(self, browser: LocalBrowser) -> dict[str, Any]:
        value = await browser.evaluate_value(_PAGE_STATE_JS, timeout=10)
        return value if isinstance(value, dict) else {}

    async def _cancelable_delay(self, seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while not self._cancel.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(min(0.2, max(0.01, deadline - asyncio.get_running_loop().time())))

    def _record(self, domain: str, success: bool, message: str) -> None:
        with self._lock:
            self._state.done += 1
            self._state.success += int(success)
            self._state.failed += int(not success)
            self._state.results.append(
                {"domain": domain, "status": "success" if success else "failed", "message": message}
            )
            self._state.results = self._state.results[-50:]
            self._state.message = f"{domain}：{message}"

    def _set_status(self, status: str, message: str) -> None:
        with self._lock:
            self._state.status = status
            self._state.message = message


_PAGE_STATE_JS = r"""
(() => {
  const visible = (el) => !!el && el.getClientRects().length > 0;
  const inputs = Array.from(document.querySelectorAll('textarea,input[type="text"],input:not([type])'));
  const input = inputs.find(el => visible(el) && !/搜索|账号|密码/.test(el.placeholder || ''));
  const body = document.body ? document.body.innerText : '';
  const feedbackNodes = Array.from(document.querySelectorAll(
    '.ant-message-notice-content,.ant-message,.toast,.message,[role="alert"]'
  )).filter(visible);
  const feedback = feedbackNodes.map(el => el.innerText || '').filter(Boolean).join(' | ');
  const captchaNode = Array.from(document.querySelectorAll('[class*="captcha"],[class*="verify"],.ant-modal'))
    .find(el => visible(el) && /验证|验证码|拖动|旋转/.test(el.innerText || body.slice(-1000)));
  return {
    hasInput: !!input,
    loggedOut: /因您未登录|只可以浏览部分内容|立即登录|登录百度账号/.test(body),
    captcha: !!captchaNode,
    inputValue: input ? (input.value || '') : '',
    feedback,
    body: body.slice(-5000),
  };
})()
"""


def _fill_and_click_script(url: str) -> str:
    import json

    encoded = json.dumps(url, ensure_ascii=False)
    return rf"""
(async () => {{
  const visible = (el) => !!el && el.getClientRects().length > 0;
  const inputs = Array.from(document.querySelectorAll('textarea,input[type="text"],input:not([type])'));
  const input = inputs.find(el => visible(el) && !/搜索|账号|密码/.test(el.placeholder || ''));
  if (!input) return {{clicked:false, message:'未找到链接输入框'}};
  const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), 'value');
  if (setter && setter.set) setter.set.call(input, {encoded}); else input.value = {encoded};
  input.dispatchEvent(new Event('input', {{bubbles:true}}));
  input.dispatchEvent(new Event('change', {{bubbles:true}}));
  await new Promise(resolve => setTimeout(resolve, 200));
  const buttons = Array.from(document.querySelectorAll('button,[role="button"]'));
  const button = buttons.find(el => visible(el) && /提交/.test((el.innerText || '').trim()));
  if (!button) return {{clicked:false, message:'未找到提交按钮'}};
  if (button.disabled || button.getAttribute('aria-disabled') === 'true') {{
    return {{clicked:false, message:'提交按钮当前不可用'}};
  }}
  button.click();
  return {{clicked:true, message:'已点击提交'}};
}})()
"""
