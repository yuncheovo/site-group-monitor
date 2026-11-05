"""时间处理工具。

统一约定（对应架构文档 §7.5）：
    * 库内存的是 **UTC ISO8601 字符串**（``2024-05-06T12:00:00Z``）；
    * ``stat_date``（``YYYY-MM-DD``）按展示时区计算，默认 ``Asia/Shanghai``；
    * **禁止**直接使用 ``datetime.now()``（无时区），本项目全部走本模块。

Windows 环境若缺少 IANA 时区数据库，会自动回退到固定的东八区偏移，保证工具可用。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

_UTC = timezone.utc
_DEFAULT_TZ_NAME = "Asia/Shanghai"
_FALLBACK_OFFSET = timezone(timedelta(hours=8), name="Asia/Shanghai(fixed+08:00)")
_tz_cache: dict[str, timezone] = {}

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)


def _resolve_tz(name: str) -> timezone:
    """解析时区对象，Windows 缺 tzdata 时回退到东八区固定偏移。

    Args:
        name: IANA 时区名，例如 ``Asia/Shanghai``。

    Returns:
        ``datetime.timezone`` 兼容对象（ZoneInfo 也是 tzinfo 子类）。
    """
    if name in _tz_cache:
        return _tz_cache[name]
    tz: timezone
    try:
        from zoneinfo import ZoneInfo

        try:
            tz = ZoneInfo(name)  # type: ignore[assignment]
        except Exception:  # noqa: BLE001 - 缺 tzdata 时尝试修复
            import zoneinfo

            zoneinfo.reset_tzpath()  # 重新扫描 tzdata 路径
            try:
                tz = ZoneInfo(name)  # type: ignore[assignment]
            except Exception:  # pragma: no cover - 最终兜底
                tz = _FALLBACK_OFFSET
    except Exception:  # pragma: no cover - Python 无 zoneinfo 的极端情况
        tz = _FALLBACK_OFFSET
    _tz_cache[name] = tz
    return tz


def get_display_tzinfo(name: str = _DEFAULT_TZ_NAME) -> timezone:
    """获取展示时区对象。

    Args:
        name: 时区名，默认 ``Asia/Shanghai``。

    Returns:
        时区对象。
    """
    return _resolve_tz(name)


def utcnow() -> datetime:
    """返回带 UTC 时区的当前时间。

    Returns:
        tz-aware 的 UTC 当前时间。
    """
    return datetime.now(tz=_UTC)


def utcnow_iso() -> str:
    """返回当前 UTC 时间的 ISO8601 字符串（秒级精度，``Z`` 结尾）。

    Returns:
        形如 ``2024-05-06T12:00:00Z`` 的字符串。
    """
    return to_iso(utcnow())


def to_iso(value: datetime) -> str:
    """将 datetime 转为 UTC ISO8601 字符串。

    Args:
        value: 任意 tz-aware datetime；naive datetime 按 UTC 处理。

    Returns:
        ``YYYY-MM-DDTHH:MM:SSZ`` 格式字符串。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    value = value.astimezone(_UTC).replace(microsecond=0)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> datetime | None:
    """解析 ISO8601 字符串为 tz-aware datetime。

    Args:
        value: ISO8601 字符串，允许 ``Z`` 结尾或带偏移，允许纳秒不存在。

    Returns:
        解析成功返回 UTC datetime；输入为空或非法返回 None。
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    match = _ISO_RE.match(text)
    if match:
        base = datetime.fromisoformat(text)
        if base.tzinfo is None:
            base = base.replace(tzinfo=_UTC)
        return base.astimezone(_UTC)
    try:
        base = datetime.fromisoformat(text)
    except ValueError:
        return None
    if base.tzinfo is None:
        base = base.replace(tzinfo=_UTC)
    return base.astimezone(_UTC)


def today_str(tz_name: str = _DEFAULT_TZ_NAME) -> str:
    """按展示时区返回今日日期键 ``stat_date``。

    Args:
        tz_name: 时区名。

    Returns:
        ``YYYY-MM-DD`` 字符串。
    """
    return datetime.now(tz=_resolve_tz(tz_name)).strftime("%Y-%m-%d")


def date_str(value: datetime, tz_name: str = _DEFAULT_TZ_NAME) -> str:
    """把某个时间点换算成展示时区的日期键。

    Args:
        value: 时间点（naive 按 UTC 处理）。
        tz_name: 时区名。

    Returns:
        ``YYYY-MM-DD`` 字符串。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=_UTC)
    return value.astimezone(_resolve_tz(tz_name)).strftime("%Y-%m-%d")


def fmt_local(value: str | datetime | None, tz_name: str = _DEFAULT_TZ_NAME,
              fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """按展示时区格式化时间，供 UI / 报告展示。

    Args:
        value: ISO 字符串或 datetime。
        tz_name: 时区名。
        fmt: strftime 格式串。

    Returns:
        格式化后的时间字符串；输入为空返回空串。
    """
    if value is None:
        return ""
    moment = parse_iso(value) if isinstance(value, str) else value
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_UTC)
    return moment.astimezone(_resolve_tz(tz_name)).strftime(fmt)


def shift_days(stat_date: str, days: int) -> str:
    """对 ``YYYY-MM-DD`` 日期键做加减天数。

    Args:
        stat_date: 基准日期键。
        days: 位移天数，可为负。

    Returns:
        新的日期键字符串。

    Raises:
        ValueError: 日期键不是合法的 ``YYYY-MM-DD``。
    """
    if not is_date_key(stat_date):
        raise ValueError(f"非法日期键 {stat_date!r}，应形如 2026-10-27")
    base = datetime.strptime(stat_date, "%Y-%m-%d")
    return (base + timedelta(days=days)).strftime("%Y-%m-%d")


def is_date_key(value: str) -> bool:
    """判断字符串是否为合法的 ``YYYY-MM-DD`` 日期键。

    会拒绝格式正确但日期不存在的输入（如 ``2026-02-30`` / ``2026-13-01``）。

    Args:
        value: 待校验字符串。

    Returns:
        合法返回 True。
    """
    text = str(value or "").strip()
    if len(text) != 10 or text[4] != "-" or text[7] != "-":
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def yesterday(stat_date: str) -> str:
    """返回前一天的日期键。"""
    return shift_days(stat_date, -1)


def last_week(stat_date: str) -> str:
    """返回上周同日的日期键。"""
    return shift_days(stat_date, -7)


def date_range(end_date: str, days: int) -> list[str]:
    """生成截止到 ``end_date``（含）的最后 N 天日期键列表，升序。

    Args:
        end_date: 结束日期键。
        days: 天数。

    Returns:
        升序日期键列表。
    """
    return [shift_days(end_date, offset) for offset in range(-(days - 1), 1)]


def parse_hhmm(value: str) -> tuple[int, int]:
    """解析 ``HH:MM`` 时间表达式。

    Args:
        value: 形如 ``09:00`` 的字符串。

    Returns:
        ``(hour, minute)`` 元组。

    Raises:
        ValueError: 格式非法或超出 00:00–23:59 范围。

    Note:
        不再静默钳制（旧实现把 ``25:00`` 悄悄变成 ``23:00``、``09:70`` 变成
        ``09:59``）。配置里写错的时间被悄悄改写，会导致定时任务在用户
        完全没预期的时刻运行，排查起来极难定位。宁可启动即报错。

    Example:
        >>> parse_hhmm("08:30")
        (8, 30)
    """
    raw = str(value or "").strip()
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式应为 HH:MM，收到 {value!r}")
    hour_text, minute_text = parts
    try:
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"时间格式应为 HH:MM，收到 {value!r}") from exc
    if not (0 <= hour <= 23):
        raise ValueError(f"小时必须在 0-23 之间，收到 {hour}")
    if not (0 <= minute <= 59):
        raise ValueError(f"分钟必须在 0-59 之间，收到 {minute}")
    return hour, minute


def in_quiet_hours(now: datetime | None = None, quiet_hours: Iterable[str] | None = None,
                   tz_name: str = _DEFAULT_TZ_NAME) -> bool:
    """判断当前是否处于静默时段（支持跨零点）。

    Args:
        now: 当前时间点，默认取当前 UTC 时间。
        quiet_hours: ``[起, 止]``，例如 ``["23:00", "07:30"]``。
        tz_name: 时区名。

    Returns:
        处于静默时段返回 True。
    """
    if not quiet_hours:
        return False
    bounds = list(quiet_hours)
    if len(bounds) < 2:
        return False
    try:
        start = parse_hhmm(bounds[0])
        end = parse_hhmm(bounds[1])
    except ValueError:
        # 静默时段配错不应阻断主流程（告警该发还得发），此处降级为「非静默」。
        return False
    moment = (now or utcnow()).astimezone(_resolve_tz(tz_name))
    current = moment.hour * 60 + moment.minute
    start_min = start[0] * 60 + start[1]
    end_min = end[0] * 60 + end[1]
    if start_min <= end_min:
        return start_min <= current < end_min
    return current >= start_min or current < end_min  # 跨零点


def duration_human(ms: int) -> str:
    """把毫秒时长格式化为人类可读形式。

    Args:
        ms: 毫秒数。

    Returns:
        形如 ``1m02s`` / ``32min 10s`` 的字符串。
    """
    seconds = max(0, int(ms // 1000))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def epoch_ms(value: datetime | None = None) -> int:
    """返回毫秒级时间戳，用于耗时统计。

    Args:
        value: 时间点，默认当前 UTC 时间。

    Returns:
        毫秒时间戳。
    """
    return int(((value or utcnow()).timestamp()) * 1000)
