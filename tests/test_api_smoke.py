"""FastAPI 冒烟测试：总览 / 站点 / 报告 / 系统接口返回统一信封。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from site_group_monitor import web
from site_group_monitor.config.settings import Settings
from site_group_monitor.storage.database import Database
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.utils.timeutil import today_str, utcnow_iso
from site_group_monitor.web import deps
from site_group_monitor.web.app import create_app


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    """构造使用临时库的 TestClient。

    Args:
        tmp_path: 临时目录。

    Yields:
        ``TestClient`` 实例。
    """
    settings = Settings()
    settings.project_root = tmp_path
    settings.storage.db_path = str(tmp_path / "db" / "monitor.db")
    settings.report.dir = str(tmp_path / "reports")
    settings.engine.baidu.evidence_dir = str(tmp_path / "evidence")
    settings.web.static_dir = str(Path(web.__file__).resolve().parent / "static")
    settings.app.log_level = "WARNING"

    database = Database(settings.db_path)
    database.init_db()
    container = deps.Container(settings)

    deps._container = container
    test_client = TestClient(create_app(container, settings))
    yield test_client
    container.shutdown()
    deps._container = None


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """校验统一信封结构并断言成功。

    Args:
        payload: 响应 JSON。

    Returns:
        ``data`` 字段。
    """
    assert payload.get("code") == 0, payload
    assert "data" in payload
    return payload["data"]


def test_health(client: TestClient) -> None:
    """健康检查返回 ok 与版本信息。"""
    data = _envelope(client.get("/api/health").json())
    assert data["status"] == "ok"


def test_overview_empty_database(client: TestClient) -> None:
    """空库时总览接口应返回 0 值而不是报错。"""
    data = _envelope(client.get("/api/overview").json())
    assert data["total_sites"] == 0
    assert data["indexed_count"] == 0
    assert data["not_indexed_count"] == 0
    assert data["unknown_count"] == 0
    assert "index_rate" in data


def test_site_crud_roundtrip(client: TestClient) -> None:
    """新增 → 列表 → 详情 → 删除 全链路冒烟。"""
    created = _envelope(client.post("/api/sites", json={"domain": "https://WWW.Example.com/",
                                                        "group": "演示", "tags": ["seo"]}).json())
    assert created["domain"] == "example.com"

    listed = _envelope(client.get("/api/sites?page=1&size=10").json())
    assert listed["total"] == 1
    assert listed["items"][0]["domain"] == "example.com"

    site_id = listed["items"][0]["id"]
    detail = _envelope(client.get(f"/api/sites/{site_id}").json())
    assert detail["site"]["domain"] == "example.com"

    removed = _envelope(client.delete(f"/api/sites/{site_id}").json())
    assert removed["removed"] is True
    assert _envelope(client.get("/api/sites").json())["total"] == 0


def test_invalid_domain_returns_failure(client: TestClient) -> None:
    """非法域名返回非 0 信封（不直接抛 500）。"""
    payload = client.post("/api/sites", json={"domain": "not a domain"}).json()
    assert payload["code"] != 0
    assert payload["message"]


def test_trend_and_stats(client: TestClient) -> None:
    """趋势与统计接口在没有数据时也应返回结构完整的空结果。"""
    trend = _envelope(client.get("/api/overview/trend?days=30").json())
    assert trend["days"] == 30
    assert isinstance(trend["series"], list)
    assert isinstance(trend["diffs"], list)

    stats = _envelope(client.get("/api/overview/stats").json())
    assert "health" in stats and "index_status" in stats


def test_report_detail_and_download(client: TestClient) -> None:
    """报告接口：JSON 详情 + HTML 文件下载。"""
    today = today_str("Asia/Shanghai")
    data = _envelope(client.get(f"/api/reports/{today}").json())
    assert data["stat_date"] == today

    response = client.get(f"/api/reports/{today}/download?fmt=html")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")


def test_system_status_and_settings(client: TestClient) -> None:
    """系统状态与设置读写（敏感字段脱敏）。"""
    status = _envelope(client.get("/api/system/status").json())
    assert "scheduler" in status and "db" in status and "mail" in status

    updated = _envelope(client.post("/api/system/settings", json={"patch": {"smtp": {"password": "secret"}}}).json())
    assert updated["smtp"]["password"] == "***"


def test_web_host_must_be_local(client: TestClient) -> None:
    """安全默认值：拒绝把仪表盘暴露到 0.0.0.0。"""
    payload = client.post("/api/system/settings", json={"patch": {"web": {"host": "0.0.0.0"}}}).json()
    assert payload["code"] != 0
    assert "127.0.0.1" in payload["message"]


def test_audit_logs_record_web_actions(client: TestClient) -> None:
    """Web 操作必须写审计日志（与 CLI 通道一致性要求）。"""
    client.post("/api/sites", json={"domain": "audit-demo.com"})
    data = _envelope(client.get("/api/system/audit?page=1&size=20").json())
    assert data["total"] >= 1
    assert any(item["action"] == "site.add" for item in data["items"])


def test_unknown_site_returns_404(client: TestClient) -> None:
    """查询不存在的站点返回 404 信封。"""
    payload = client.get("/api/sites/9999").json()
    assert payload["code"] != 0
    assert "不存在" in payload["message"]


def test_index_page_available(client: TestClient) -> None:
    """静态单页可访问（index.html 存在时返回 HTML）。"""
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert ("站群监控" in body) or ("静态资源" in body)
    if "站群监控" in body:
        assert "删除站点" in body
        assert "site-del" in body or "drawerDelete" in body
        assert 'id="btnRetryFailed"' in body
        assert 'id="btnCopySelected"' in body
    _ = utcnow_iso()
