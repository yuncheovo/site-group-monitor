"""百度资源平台浏览器提交服务测试。"""

from __future__ import annotations

from site_group_monitor.services.baidu_submit_service import (
    BaiduSubmitService,
    SUBMIT_PAGE,
    _fill_and_click_script,
)


def test_submit_service_is_browser_only(tmp_settings) -> None:  # noqa: ANN001
    service = BaiduSubmitService(tmp_settings)
    copied = service._browser_settings()

    assert copied.engine.baidu.browser.enabled is True
    assert copied.engine.baidu.browser.headless is False
    assert copied.engine.baidu.browser.via_proxy is False
    assert not hasattr(service, "token")


def test_fill_script_safely_embeds_url() -> None:
    script = _fill_and_click_script('https://example.com/a?x="quoted"')

    assert "example.com" in script
    assert '\\"quoted\\"' in script
    assert "button.click()" in script
    assert SUBMIT_PAGE.startswith("https://ziyuan.baidu.com/")


def test_submit_cancel_updates_state(tmp_settings) -> None:  # noqa: ANN001
    service = BaiduSubmitService(tmp_settings)
    service._running = True
    service.cancel()

    state = service.snapshot()
    assert state["running"] is True
    assert "停止" in state["message"]
