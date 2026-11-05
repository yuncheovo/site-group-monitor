"""查询性能与任务状态的回归用例。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from site_group_monitor.core.check_worker import SiteCheckWorker
from site_group_monitor.core.proxy_pool import ProxyPool
from site_group_monitor.models.entities import Site, SiteStatusInfo, TdkInfo
from site_group_monitor.models.enums import ErrorType, RunMode, RunStatus


class _IndexFailure:
    engine = "baidu"
    proxies = None

    async def check(self, domain, context, browser=None):
        return None


class _TdkOk:
    async def fetch_and_parse(self, domain, root_url, proxy=None, browser=None):
        return TdkInfo(source_url=root_url)


class _StatusOk:
    async def probe(self, domain, root_url, proxy=None):
        return SiteStatusInfo(http_status=200)


def _settings():
    return SimpleNamespace(
        proxy=SimpleNamespace(acquire_timeout_sec=5.0),
        engine=SimpleNamespace(
            baidu=SimpleNamespace(
                timeout_sec=1,
                browser=SimpleNamespace(enabled=False),
            )
        ),
    )


def test_missing_index_result_is_recorded_as_timeout() -> None:
    """默认 error_type=none 不得掩盖真实的收录查询失败。"""
    worker = SiteCheckWorker(_IndexFailure(), _TdkOk(), _StatusOk(), _settings())
    result = asyncio.run(worker.run(Site(id=1, domain="example.com"), "task", RunMode.FULL))
    assert result.error_type == ErrorType.TIMEOUT.value
    assert result.error_msg == "收录检测超时/失败"


def test_batch_status_reflects_result_quality() -> None:
    """大量未知结果必须失败，少量未知应标为部分完成。"""
    # 延迟导入：该模块会加载完整采集依赖，生产/CI 环境由 requirements.txt 提供。
    from site_group_monitor.services.check_service import CheckService, _ProgressState

    service = object.__new__(CheckService)
    service._state = _ProgressState(
        total=10, done=10, indexed=1, not_indexed=0, unknown=9, failed=9
    )
    assert service._result_status()[0] == RunStatus.FAILED.value

    service._state = _ProgressState(
        total=10, done=10, indexed=8, not_indexed=1, unknown=1, failed=1
    )
    assert service._result_status()[0] == RunStatus.PARTIAL.value

    service._state = _ProgressState(
        total=10, done=10, indexed=8, not_indexed=2, unknown=0, failed=0
    )
    assert service._result_status()[0] == RunStatus.SUCCESS.value


def test_captcha_rotation_prefers_on_risk_source() -> None:
    """验证码触发换 IP 时，应优先调用“风控时切换”策略，而不是每站策略。"""
    calls = []
    replacement = SimpleNamespace(key="new", masked_url="http://new:80")
    pool = object.__new__(ProxyPool)
    pool.settings = _settings()
    pool.logger = None

    async def fake_rotate(rotate_per_check=True):
        calls.append(rotate_per_check)
        return replacement if rotate_per_check is False else None

    pool._acquire_rotated = fake_rotate
    pool.release = lambda proxy: None
    current = SimpleNamespace(key="old")
    result = asyncio.run(pool.rotate_after_captcha(current, timeout_sec=1))
    assert result is replacement
    assert calls == [False]
