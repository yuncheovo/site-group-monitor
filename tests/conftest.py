"""pytest 公共 fixtures（临时库 + 轻量配置，保证测试完全离线）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from site_group_monitor.config.settings import Settings
from site_group_monitor.storage.database import Database
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.web import deps


@pytest.fixture()
def tmp_settings(tmp_path: Path) -> Settings:
    """构造指向临时目录的配置对象（不读取/写入任何真实用户配置）。

    Args:
        tmp_path: pytest 提供的临时目录。

    Returns:
        :class:`Settings` 实例。
    """
    settings = Settings()
    settings.project_root = tmp_path
    settings.storage.db_path = str(tmp_path / "data" / "monitor.db")
    settings.report.dir = str(tmp_path / "data" / "reports")
    settings.engine.baidu.evidence_dir = str(tmp_path / "data" / "evidence")
    return settings


@pytest.fixture()
def tmp_db(tmp_settings: Settings) -> Iterator[Database]:
    """初始化一个临时 SQLite 库（已建 7 张表）。

    Args:
        tmp_settings: 临时配置。

    Yields:
        :class:`Database` 实例。
    """
    database = Database(tmp_settings.db_path, busy_timeout_ms=2000)
    database.init_db()
    yield database


@pytest.fixture()
def repos(tmp_db: Database) -> Repositories:
    """临时仓储容器。

    Args:
        tmp_db: 临时数据库。

    Returns:
        :class:`Repositories` 实例。
    """
    return Repositories(tmp_db)


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    """构造使用临时库的 TestClient（与 test_api_smoke 共用）。"""
    from site_group_monitor import web
    from site_group_monitor.utils.timeutil import today_str, utcnow_iso
    from site_group_monitor.web.app import create_app

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
    _ = (today_str, utcnow_iso)


@pytest.fixture()
def sample_site() -> dict[str, Any]:
    """一个标准站点字典（用于快速造数据）。"""
    from site_group_monitor.utils.timeutil import utcnow_iso

    return {
        "domain": "example.com",
        "root_url": "https://example.com/",
        "name": "示例站",
        "group_name": "默认",
        "tags": ["seo", "news"],
        "created_at": utcnow_iso(),
        "updated_at": utcnow_iso(),
    }
