"""rich 进度条与阶段统计封装（可观测性 R-020）。

导出：
    * :class:`RunProgress`：终端实时进度（并发数 / 成功 / 失败 / unknown / 耗时 / ETA）；
    * :meth:`RunProgress.as_callback`：产出提供给 ``CheckService`` 的回调对象；
    * :class:`NullProgress`：rich 不可用或静默模式下的空实现（接口保持一致）。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from site_group_monitor.utils.timeutil import duration_human


class RunProgress:
    """一轮检测的终端进度与统计。

    Args:
        total: 本轮待检测站点总数。
        title: 进度条标题。
        enabled: 是否真的渲染终端进度条（False 时退化为纯统计）。
    """

    def __init__(self, total: int = 0, title: str = "检测进度", enabled: bool = True) -> None:
        self.total: int = max(0, int(total))
        self.title = title
        self.enabled = enabled and total > 0
        self.done: int = 0
        self.ok: int = 0
        self.failed: int = 0
        self.unknown: int = 0
        self.running: int = 0
        self.concurrency: int = 0
        self.started_at: float = time.perf_counter()
        self._progress: Any | None = None
        self._task_id: Any | None = None
        self._last_message: str = ""
        self._build()

    def _build(self) -> None:
        """创建 rich Progress 实例（失败时静默降级）。"""
        if not self.enabled:
            return
        try:
            from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TextColumn("[cyan]{task.fields[stats]}", justify="right"),
                TimeElapsedColumn(),
                transient=False,
            )
            self._progress.start()
            self._task_id = self._progress.add_task(self.title, total=self.total, stats="")
        except Exception:  # noqa: BLE001 - rich 缺失不影响运行
            self._progress = None
            self._task_id = None
            self.enabled = False

    def set_total(self, total: int) -> None:
        """更新任务总数。

        Args:
            total: 新的总数。
        """
        self.total = max(0, int(total))
        if self._progress is not None and self._task_id is not None:
            try:
                self._progress.update(self._task_id, total=self.total)
            except Exception:  # noqa: BLE001
                pass

    def note(self, message: str) -> None:
        """记录一条阶段说明，随下一次刷新写入进度条描述。

        Args:
            message: 说明文本。
        """
        self._last_message = message
        if self._progress is not None and self._task_id is not None:
            try:
                self._progress.update(self._task_id, description=f"{self.title} · {message}")
            except Exception:  # noqa: BLE001
                pass

    def advance(
        self,
        *,
        index_status: str = "",
        success: bool = True,
        running_delta: int = 0,
        concurrency: int | None = None,
    ) -> None:
        """推进一格进度。

        Args:
            index_status: 本次检测的收录状态（用于 unknown 计数）。
            success: 是否成功完成（False 记 failed）。
            running_delta: 在跑协程数的增量。
            concurrency: 当前并发值（None 表示不更新）。
        """
        self.done += 1
        if not success:
            self.failed += 1
        elif str(index_status) == "unknown_need_manual_check":
            self.unknown += 1
        else:
            self.ok += 1
        self.running = max(0, self.running + running_delta)
        if concurrency is not None:
            self.concurrency = concurrency
        self._render()

    def _render(self) -> None:
        """刷新终端进度条显示。"""
        if self._progress is None or self._task_id is None:
            return
        elapsed_ms = int((time.perf_counter() - self.started_at) * 1000)
        stats = (
            f"ok={self.ok} fail={self.failed} unknown={self.unknown} "
            f"并发={self.concurrency} 耗时={duration_human(elapsed_ms)}"
        )
        try:
            self._progress.update(self._task_id, completed=self.done, stats=stats)
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self, *, total: int | None = None, phase: str = "running") -> dict[str, Any]:
        """导出进度快照，供 ``/api/system/status`` 与 CheckService 使用。

        Args:
            total: 覆盖总数。
            phase: 阶段名。

        Returns:
            包含统计字段的字典。
        """
        elapsed_ms = int((time.perf_counter() - self.started_at) * 1000)
        done = max(1, self.done)
        eta_ms = int(elapsed_ms * (max(0, (total or self.total) - self.done) / done)) if self.done else 0
        qps = round(self.done / (elapsed_ms / 1000), 3) if elapsed_ms > 0 else 0.0
        return {
            "phase": phase,
            "total": total if total is not None else self.total,
            "done": self.done,
            "ok": self.ok,
            "failed": self.failed,
            "unknown": self.unknown,
            "running": self.running,
            "concurrency": self.concurrency,
            "elapsed_ms": elapsed_ms,
            "eta_ms": eta_ms,
            "qps": qps,
            "message": self._last_message,
        }

    def as_callback(self) -> Callable[[dict[str, Any]], None]:
        """生成适配 CheckService 的进度回调闭包。

        Returns:
            接受 ``{"index_status":..., "success":..., ...}`` 字典的可调用对象。
        """

        def _callback(payload: dict[str, Any]) -> None:
            self.advance(
                index_status=str(payload.get("index_status", "")),
                success=bool(payload.get("success", True)),
                running_delta=int(payload.get("running_delta", 0)),
                concurrency=payload.get("concurrency"),
                )

        return _callback

    def finish(self, message: str = "完成") -> None:
        """结束进度条渲染。

        Args:
            message: 结束提示。
        """
        self.note(message)
        self._render()
        if self._progress is not None:
            try:
                self._progress.stop()
            except Exception:  # noqa: BLE001
                pass
            self._progress = None
            self._task_id = None


class NullProgress(RunProgress):
    """静默进度实现：不渲染终端，仅维护计数（用于 Web/调度后台模式）。"""

    def __init__(self, total: int = 0, title: str = "检测进度", enabled: bool = False) -> None:
        super().__init__(total=total, title=title, enabled=False)
