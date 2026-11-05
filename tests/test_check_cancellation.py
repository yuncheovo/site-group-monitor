"""检测任务取消行为回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from site_group_monitor.core.check_worker import _guarded
from site_group_monitor.models.enums import RunMode
from site_group_monitor.services.check_service import CancelToken, CheckService, _ProgressState


def _run_async(coro) -> None:  # noqa: ANN001
    """运行异步场景，同时保留测试进程原有的默认事件循环。"""
    try:
        previous = asyncio.get_event_loop()
    except RuntimeError:
        previous = asyncio.new_event_loop()
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(previous)


def _bare_service() -> CheckService:
    service = object.__new__(CheckService)
    service.logger = None
    service._state = _ProgressState(total=1, concurrency=1)
    service._current_tasks = set()
    service._external_cancel = None
    service._running = False
    return service


def test_cancel_sets_the_active_shared_token() -> None:
    service = _bare_service()
    active = CancelToken()
    service._external_cancel = active

    returned = service.cancel()

    assert returned is active
    assert active.cancelled is True
    assert active.reason == "用户通过 API 取消"


def test_cancel_immediately_after_prepare_is_not_lost() -> None:
    service = _bare_service()
    service._running = False
    prepared = service.prepare_run()

    returned = service.cancel()

    assert returned is prepared
    assert prepared.cancelled is True


def test_run_batch_interrupts_in_flight_worker_quickly() -> None:
    class SlowWorker:
        async def run(self, site, task_id, mode):  # noqa: ANN001, ANN201
            await asyncio.sleep(30)

    async def scenario() -> None:
        service = _bare_service()
        service._task_id = "cancel-test"
        token = CancelToken()
        batch_task = asyncio.create_task(
            service._run_batch(
                [SimpleNamespace(domain="slow.example", id=1)],
                SlowWorker(),
                asyncio.Semaphore(1),
                None,
                None,
                RunMode.MANUAL_SINGLE,
                None,
                token,
            )
        )
        await asyncio.sleep(0.02)
        token.cancel()
        await asyncio.wait_for(batch_task, timeout=1.0)
        assert service._state.running == 0
        assert not service._current_tasks

    _run_async(scenario())


def test_guarded_does_not_swallow_parent_cancellation() -> None:
    async def scenario() -> None:
        inner = asyncio.create_task(asyncio.sleep(30))
        guarded = asyncio.create_task(_guarded(inner, 30, None))
        await asyncio.sleep(0)
        guarded.cancel()
        with pytest.raises(asyncio.CancelledError):
            await guarded
        await asyncio.sleep(0)
        assert inner.cancelled()

    _run_async(scenario())
