"""百度收录检测器的空白风控页重试回归测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from site_group_monitor.config.settings import Settings
from site_group_monitor.core.baidu_checker import BaiduIndexChecker
from site_group_monitor.core.checker_base import IndexCheckContext
from site_group_monitor.core.http_client import RawResponse
from site_group_monitor.models.entities import ProxyEndpoint
from site_group_monitor.models.enums import IndexStatus


def _blank_shell(domain: str) -> str:
    return (
        f"<html><head><title>site:{domain}_百度搜索</title></head>"
        f'<body><div id="wrapper"><input value="site:{domain}">百度一下</div></body></html>'
    )


def _indexed_page(domain: str) -> str:
    return f"""
    <html><head><title>site:{domain}_百度搜索</title></head><body>
      <div id="container"><div id="content_left">
        <div class="result c-container" mu="https://{domain}/">
          <h3><a href="https://{domain}/">目标站点</a></h3>
          <span class="c-showurl">{domain}/</span>
        </div>
      </div></div>
      <div class="nums_text">百度为您找到相关结果约 1 个</div>
    </body></html>
    """


class _Browser:
    def __init__(self, replacement_id: int) -> None:
        self.replacement_id = replacement_id
        self.proxy_ids: list[int | None] = []

    async def fetch(self, url: str, *, proxy=None, referer=None) -> RawResponse:
        del referer
        proxy_id = getattr(proxy, "id", None)
        self.proxy_ids.append(proxy_id)
        domain = "example.com"
        html = _indexed_page(domain) if proxy_id == self.replacement_id else _blank_shell(domain)
        return RawResponse(
            ok=True,
            status_code=200,
            url=url,
            final_url=url,
            text=html,
            elapsed_ms=20,
        )


class _ProxyPool:
    def __init__(self, replacement: ProxyEndpoint) -> None:
        self.replacement = replacement
        self.captcha_ids: list[int | None] = []
        self.ok_ids: list[int | None] = []
        self.released_ids: list[int | None] = []

    def report_captcha(self, proxy: ProxyEndpoint | None) -> None:
        self.captcha_ids.append(getattr(proxy, "id", None))

    def report_ok(self, proxy: ProxyEndpoint | None) -> None:
        self.ok_ids.append(getattr(proxy, "id", None))

    def report_fail(self, proxy: ProxyEndpoint | None, *, kind: str) -> None:
        del proxy, kind

    async def rotate_after_captcha(self, current: ProxyEndpoint | None) -> ProxyEndpoint:
        del current
        return self.replacement

    def release(self, proxy: ProxyEndpoint | None) -> None:
        self.released_ids.append(getattr(proxy, "id", None))


@pytest.mark.asyncio
async def test_blank_shell_refreshes_then_rotates_once(tmp_path: Path) -> None:
    """空白壳先刷新原 IP，仍失败才换 IP；换 IP 后正常结果应直接返回。"""
    settings = Settings()
    settings.project_root = tmp_path
    settings.engine.baidu.evidence_dir = str(tmp_path / "evidence")
    current = ProxyEndpoint(id=1, protocol="http", host="127.0.0.1", port=8001)
    replacement = ProxyEndpoint(id=2, protocol="http", host="127.0.0.1", port=8002)
    pool = _ProxyPool(replacement)
    browser = _Browser(replacement_id=2)
    checker = BaiduIndexChecker(
        http=None,
        proxies=pool,
        limiter=None,
        settings=settings,
    )

    result = await checker.check(
        "example.com",
        IndexCheckContext(task_id="task", attempt=1, proxy=current),
        browser=browser,
    )

    assert result.status == IndexStatus.INDEXED
    assert result.proxy_id == 2
    assert browser.proxy_ids == [1, 1, 2]
    assert pool.captcha_ids == [1]
    assert pool.ok_ids == [2]
    assert pool.released_ids == [2]
