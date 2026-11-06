"""百度旋转验证码自动求解（可选能力，失败自动降级人工）。

依赖（缺任一则 :class:`RotateCaptchaUnavailableError`，由调用方降级为人工等待）：

- ``torch`` / ``torchvision``：CPU 推理，见 ``requirements-optional.txt``
- ``data/models/rotate-captcha-crack/`` 目录：

  - ``rotate_captcha_crack/``：模型代码包（rotate-captcha-crack v0.5.1）
  - ``models/RotNetR/<timestamp>/best.pth``：预训练权重（百度验证码跨域误差约 6.6°）

求解流程：JS 定位验证码图片与滑块 → 截图 → RotNetR 预测「顺时针回正角度」→
CDP 输入事件模拟真人拖动滑块 → 未通过则点「换一张」重试。
"""

from __future__ import annotations

import asyncio
import contextlib
import glob
import io
import os
import random
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

_DEFAULT_MODEL_DIR = Path("data/models/rotate-captcha-crack")

# 模型单例：进程内只加载一次（约 2–4 秒）
_PREDICTOR: Callable[[bytes], float] | None = None
_PREDICTOR_ERROR: Exception | None = None


class RotateCaptchaUnavailableError(RuntimeError):
    """模型依赖缺失（未装 torch 或未部署权重），调用方应降级为人工过码。"""


def _model_root(settings: Any | None) -> Path:
    if settings is not None and hasattr(settings, "resolve_path"):
        return Path(settings.resolve_path(str(_DEFAULT_MODEL_DIR)))
    return _DEFAULT_MODEL_DIR


def _build_predictor(settings: Any | None) -> Callable[[bytes], float]:
    root = _model_root(settings)
    weights = sorted(glob.glob(str(root / "models" / "**" / "best.pth"), recursive=True))
    if not weights:
        raise RotateCaptchaUnavailableError(f"未找到旋转验证码模型权重: {root}")
    try:
        import torch  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:
        raise RotateCaptchaUnavailableError(f"缺少依赖 {exc.name}，见 requirements-optional.txt") from exc

    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    # rcc 的 common.py 在 import 时构造 torch.device('cuda', device_count()-1)，
    # 无 GPU 环境 device_count()=0 会抛 "Device index must not be negative"。
    # 打补丁让构造通过，实际推理始终走 CPU。
    if not torch.cuda.is_available():
        torch.cuda.device_count = lambda: 1  # type: ignore[assignment]
    try:
        from rotate_captcha_crack.model import RotNetR  # noqa: PLC0415
        from rotate_captcha_crack.utils import get_state_dict, process_captcha  # noqa: PLC0415
    except ImportError as exc:
        raise RotateCaptchaUnavailableError(f"模型代码包缺失: {root / 'rotate_captcha_crack'}") from exc

    model = RotNetR(cls_num=128, train=False)
    model.load_state_dict(get_state_dict(Path(weights[-1])))
    model = model.to(device=torch.device("cpu"))
    model.eval()

    def predict(img_bytes: bytes) -> float:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_ts = process_captcha(img, target_size=224)
        with torch.no_grad():
            return float(model.predict(img_ts))

    return predict


def predict_rotation_ratio(img_bytes: bytes, *, settings: Any | None = None) -> float:
    """输入验证码图片二进制，返回 0–1 的「顺时针回正比例」。

    百度滑块满程对应 360°，故拖动距离 = 返回值 × 轨道宽度。
    依赖缺失时抛 :class:`RotateCaptchaUnavailableError`。
    """
    global _PREDICTOR, _PREDICTOR_ERROR
    if _PREDICTOR is not None:
        return _PREDICTOR(img_bytes)
    if _PREDICTOR_ERROR is not None:
        raise RotateCaptchaUnavailableError(str(_PREDICTOR_ERROR)) from _PREDICTOR_ERROR
    try:
        _PREDICTOR = _build_predictor(settings)
    except RotateCaptchaUnavailableError as exc:
        _PREDICTOR_ERROR = exc
        raise
    return _PREDICTOR(img_bytes)


def reset_predictor() -> None:
    """清空模型单例（测试用）。"""
    global _PREDICTOR, _PREDICTOR_ERROR
    _PREDICTOR = None
    _PREDICTOR_ERROR = None


# --------------------------- 页面元素定位 ---------------------------

_FIND_CAPTCHA_JS = r"""
(() => {
  const visible = (el) => !!el && el.getClientRects().length > 0;
  const rect = (el) => {
    const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height};
  };
  const containers = Array.from(document.querySelectorAll(
    '.vcode-spin,#vcode-spin,.mod-vcodes,.vcode-body,[class*="vcode"],[id*="vcode"],[class*="captcha"],[id*="captcha"]'
  )).filter(visible);
  const container = containers.find(el => /旋转|拖动|滑块|验证/.test(el.innerText || '')) || containers[0];
  if (!container) return {found: false};
  const imgs = Array.from(container.querySelectorAll('img,canvas')).filter(visible);
  const img = imgs.find(el => {
    const r = el.getBoundingClientRect();
    return r.width >= 60 && r.height >= 60 && Math.abs(r.width - r.height) <= r.width * 0.35;
  });
  if (!img) return {found: false, reason: 'no-image'};
  const imgR = img.getBoundingClientRect();
  const sliders = Array.from(container.querySelectorAll(
    '.vcode-spin-slider,.vcode-slider,.slider-btn,[class*="slider"],button,[role="button"]'
  )).filter(visible);
  const slider = sliders.find(el => {
    const r = el.getBoundingClientRect();
    return r.width >= 20 && r.width <= imgR.width && r.height <= 60 &&
      r.y > imgR.y + imgR.height * 0.5;
  }) || sliders[0];
  if (!slider) return {found: false, reason: 'no-slider'};
  const refresh = Array.from(container.querySelectorAll('a,button,span,div'))
    .filter(visible)
    .find(el => /换一|刷新|refresh/i.test(`${el.innerText || ''} ${el.className || ''} ${el.title || ''}`)
      && el.getBoundingClientRect().width < 120);
  // 百度旋转码滑块满程为 360°，轨道宽度约等于验证码图片宽度
  return {
    found: true,
    img: rect(img),
    slider: rect(slider),
    track: imgR.width,
    hasRefresh: !!refresh,
    refresh: refresh ? rect(refresh) : null,
  };
})()
"""

# 点击刷新按钮（换一张验证码图）
_CLICK_REFRESH_JS = r"""
(() => {
  const visible = (el) => !!el && el.getClientRects().length > 0;
  const target = Array.from(document.querySelectorAll('a,button,span,div'))
    .filter(visible)
    .find(el => /换一|刷新|refresh/i.test(`${el.innerText || ''} ${el.className || ''} ${el.title || ''}`)
      && el.getBoundingClientRect().width < 120);
  if (!target) return {clicked: false};
  target.scrollIntoView({block: 'center'});
  HTMLElement.prototype.click.call(target);
  return {clicked: true};
})()
"""


async def find_captcha_elements(browser: Any) -> dict[str, Any]:
    """通过 JS 在页面定位旋转验证码元素，返回几何信息；未出现返回空 dict。"""
    value = await browser.evaluate_value(_FIND_CAPTCHA_JS, timeout=8)
    if isinstance(value, dict) and value.get("found"):
        return value
    return {}


async def _human_drag(browser: Any, slider_rect: dict[str, float], distance: float) -> None:
    """模拟真人拖动滑块：缓入缓出 + 纵向抖动 + 末端回弹（CDP 输入事件）。"""
    rand = random.Random()
    sx = float(slider_rect["x"]) + float(slider_rect["width"]) / 2
    sy = float(slider_rect["y"]) + float(slider_rect["height"]) / 2

    await browser.mouse_move(sx, sy)
    await asyncio.sleep(rand.uniform(0.2, 0.5))
    await browser.mouse_down(sx, sy)
    await asyncio.sleep(rand.uniform(0.1, 0.3))

    steps = rand.randint(26, 40)
    current = 0.0
    for index in range(1, steps + 1):
        t = index / steps
        eased = 1 - (1 - t) ** 3  # easeOutCubic
        target = distance * eased
        await browser.mouse_move(sx + target, sy + rand.uniform(-1.5, 1.5), pressed=True)
        current = target
        await asyncio.sleep(rand.uniform(0.008, 0.024))

    await browser.mouse_move(sx + current + rand.uniform(2, 6), sy + rand.uniform(-1, 1), pressed=True)
    await asyncio.sleep(rand.uniform(0.05, 0.12))
    await browser.mouse_move(sx + distance, sy, pressed=True)
    await asyncio.sleep(rand.uniform(0.1, 0.25))
    await browser.mouse_up(sx + distance, sy)


async def solve_rotate_captcha(
    browser: Any,
    settings: Any | None,
    logger: Any | None,
    is_captcha_present: Callable[[], Awaitable[bool]],
    *,
    max_retry: int = 8,
    status_cb: Callable[[str], None] | None = None,
) -> bool:
    """自动求解当前页面的旋转验证码。

    Args:
        browser: :class:`~site_group_monitor.core.local_browser.LocalBrowser`。
        settings: 用于定位模型目录，可为 ``None``（走默认相对路径）。
        logger: 项目日志器。
        is_captcha_present: 异步回调，返回页面是否仍显示验证码（调用方复用页面状态检测）。
        max_retry: 最多换图重试次数。
        status_cb: 状态回调（如更新提交服务的进度文案）。

    Returns:
        ``True`` 验证码已通过；``False`` 多次尝试后仍失败（调用方转人工）。

    Raises:
        RotateCaptchaUnavailableError: 模型依赖缺失，调用方应降级人工。
    """
    rand = random.Random()
    flip = False

    def report(message: str) -> None:
        if status_cb is not None:
            status_cb(message)
        if logger is not None:
            logger.info("旋转验证码自动求解: %s", message)

    for attempt in range(1, max_retry + 1):
        elements = await find_captcha_elements(browser)
        if not elements:
            return not await is_captcha_present()

        try:
            # 1. 截取验证码图片区域（含少量边距，避免圆形被裁）
            img_rect = elements["img"]
            clip = {
                "x": max(0.0, float(img_rect["x"]) - 4),
                "y": max(0.0, float(img_rect["y"]) - 4),
                "width": float(img_rect["width"]) + 8,
                "height": float(img_rect["height"]) + 8,
            }
            png = await browser.screenshot(clip)
            if not png:
                report("第 %d 次：截图失败" % attempt)
                continue

            # 2. 模型预测顺时针回正比例
            ratio = await asyncio.to_thread(predict_rotation_ratio, png, settings=settings)
            if flip:
                ratio = 1.0 - ratio
            degree = ratio * 360

            # 3. 换算拖动距离并模拟真人拖动
            track = float(elements["track"])
            distance = ratio * track
            report(
                "第 %d 次：预测回正 %.0f°，轨道 %.0fpx，拖动 %.0fpx%s"
                % (attempt, degree, track, distance, "（反向）" if flip else "")
            )
            await _human_drag(browser, elements["slider"], distance)
            await asyncio.sleep(rand.uniform(1.5, 2.5))

            # 4. 校验结果：验证码消失即通过
            if not await is_captcha_present():
                report("第 %d 次：验证通过" % attempt)
                return True
            report("第 %d 次未通过" % attempt)

            # 5. 连续失败 3 次后尝试反向角度（少数页面滑块方向映射相反）
            if attempt == 3:
                flip = True
                report("连续失败，切换为反向角度重试")

            # 6. 点「换一张」刷新验证码后重试
            if elements.get("hasRefresh"):
                with contextlib.suppress(Exception):
                    await browser.evaluate_value(_CLICK_REFRESH_JS, timeout=5)
                await asyncio.sleep(rand.uniform(1.0, 1.8))
            else:
                await asyncio.sleep(rand.uniform(1.0, 1.8))
        except RotateCaptchaUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            report("第 %d 次异常：%s" % (attempt, exc))
            if not await is_captcha_present():
                return True
            await asyncio.sleep(1.0)

    return False


async def solve_or_wait_manual(
    browser: Any,
    settings: Any | None,
    logger: Any | None,
    is_captcha_present: Callable[[], Awaitable[bool]],
    *,
    max_retry: int = 8,
    status_cb: Callable[[str], None] | None = None,
    manual_cb: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """自动优先、失败/不可用降级人工的总入口。

    - 自动求解成功 → ``True``
    - 自动求解失败 且提供 ``manual_cb`` → 转人工等待，返回人工结果
    - 人工等待也未通过 → ``False``
    """
    try:
        solved = await solve_rotate_captcha(
            browser, settings, logger, is_captcha_present,
            max_retry=max_retry, status_cb=status_cb,
        )
        if solved:
            return True
        if status_cb is not None:
            status_cb("自动过码未成功，转为人工处理")
    except RotateCaptchaUnavailableError as exc:
        if status_cb is not None:
            status_cb("自动过码不可用（%s），请在浏览器中手动完成" % exc)
        if logger is not None:
            logger.warning("旋转验证码自动求解不可用: %s", exc)
    if manual_cb is not None:
        return await manual_cb()
    return False
