"""旋转验证码自动求解模块测试（全离线，不加载真实模型）。"""

from __future__ import annotations

from typing import Any

import pytest

from site_group_monitor.core import rotate_captcha
from site_group_monitor.core.rotate_captcha import (
    RotateCaptchaUnavailableError,
    _CLICK_REFRESH_JS,
    _FIND_CAPTCHA_JS,
    find_captcha_elements,
    solve_or_wait_manual,
    solve_rotate_captcha,
)


class FakeBrowser:
    """模拟 LocalBrowser：按预设序列返回元素，记录鼠标事件。"""

    def __init__(self, elements_seq: list[dict[str, Any]], presence_seq: list[bool]) -> None:
        self._elements = list(elements_seq)
        self._presence = list(presence_seq)
        self.mouse_events: list[tuple[str, float, float]] = []
        self.clicked_refresh = 0

    async def evaluate_value(self, expression: str, *, timeout: float = 10.0) -> Any:
        if "const target" in expression:  # _CLICK_REFRESH_JS 的特征
            self.clicked_refresh += 1
            return {"clicked": True}
        if "containers" in expression:  # _FIND_CAPTCHA_JS 的特征
            if self._elements:
                return self._elements.pop(0)
            return {"found": False}
        return {"found": False}

    async def screenshot(self, clip: dict[str, float] | None = None) -> bytes:
        return b"\x89PNG"  # 占位字节，predict 已被 mock

    async def mouse_move(self, x: float, y: float, *, pressed: bool = False) -> None:
        self.mouse_events.append(("move", x, y))

    async def mouse_down(self, x: float, y: float) -> None:
        self.mouse_events.append(("down", x, y))

    async def mouse_up(self, x: float, y: float) -> None:
        self.mouse_events.append(("up", x, y))


_ELEMENTS = {
    "found": True,
    "img": {"x": 100.0, "y": 100.0, "width": 300.0, "height": 300.0},
    "slider": {"x": 110.0, "y": 430.0, "width": 40.0, "height": 30.0},
    "track": 300.0,
    "hasRefresh": True,
    "refresh": {"x": 420.0, "y": 430.0, "width": 40.0, "height": 20.0},
}


def test_find_js_selectors_cover_baidu_variants() -> None:
    assert ".vcode-spin" in _FIND_CAPTCHA_JS
    assert '[class*="vcode"]' in _FIND_CAPTCHA_JS
    assert '[class*="captcha"]' in _FIND_CAPTCHA_JS
    assert '[class*="slider"]' in _FIND_CAPTCHA_JS
    assert "换一" in _CLICK_REFRESH_JS
    assert "refresh" in _CLICK_REFRESH_JS


def test_predict_raises_when_model_missing(tmp_settings: Any) -> None:
    rotate_captcha.reset_predictor()
    with pytest.raises(RotateCaptchaUnavailableError):
        rotate_captcha.predict_rotation_ratio(b"\x89PNG", settings=tmp_settings)
    # 第二次调用直接抛缓存的错误，不再重复扫盘
    with pytest.raises(RotateCaptchaUnavailableError):
        rotate_captcha.predict_rotation_ratio(b"\x89PNG", settings=tmp_settings)
    rotate_captcha.reset_predictor()


async def test_find_captcha_elements_returns_geometry() -> None:
    browser = FakeBrowser([dict(_ELEMENTS)], [])
    elements = await find_captcha_elements(browser)
    assert elements["track"] == 300.0
    assert elements["slider"]["x"] == 110.0


async def test_solve_success_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    # 一次出现 → 拖完即消失
    browser = FakeBrowser([dict(_ELEMENTS)], [False])
    monkeypatch.setattr(rotate_captcha, "predict_rotation_ratio", lambda *_a, **_k: 0.25)

    async def is_present() -> bool:
        return browser._presence.pop(0) if browser._presence else False

    solved = await solve_rotate_captcha(browser, None, None, is_present, max_retry=3)

    assert solved is True
    events = browser.mouse_events
    assert events[0][0] == "move"
    assert any(event[0] == "down" for event in events)
    assert events[-1][0] == "up"
    # ratio 0.25 × track 300 = 75px；up 的 x 应落在 slider 中心(130) + 75 ≈ 205
    up_x = events[-1][1]
    assert abs(up_x - 205.0) < 1.0


async def test_solve_retries_until_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    # 两次出现、两次未通过 → 第三次通过
    browser = FakeBrowser([dict(_ELEMENTS), dict(_ELEMENTS), dict(_ELEMENTS)], [True, True, False])
    monkeypatch.setattr(rotate_captcha, "predict_rotation_ratio", lambda *_a, **_k: 0.5)

    async def is_present() -> bool:
        return browser._presence.pop(0) if browser._presence else False

    solved = await solve_rotate_captcha(browser, None, None, is_present, max_retry=5)

    assert solved is True
    assert browser.clicked_refresh >= 1  # 失败后点了「换一张」


async def test_solve_or_wait_manual_fallback_on_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_solve(*_a: Any, **_k: Any) -> bool:
        raise RotateCaptchaUnavailableError("no torch")

    monkeypatch.setattr(rotate_captcha, "solve_rotate_captcha", fake_solve)
    calls: list[str] = []

    async def is_present() -> bool:
        return True

    async def manual() -> bool:
        calls.append("manual")
        return True

    result = await solve_or_wait_manual(
        None, None, None, is_present, manual_cb=manual
    )
    assert result is True
    assert calls == ["manual"]


async def test_solve_or_wait_manual_uses_auto_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_solve(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(rotate_captcha, "solve_rotate_captcha", fake_solve)
    calls: list[str] = []

    async def is_present() -> bool:
        return True

    async def manual() -> bool:
        calls.append("manual")
        return True

    result = await solve_or_wait_manual(
        None, None, None, is_present, manual_cb=manual
    )
    assert result is True
    assert calls == []  # 自动成功则不触发人工
