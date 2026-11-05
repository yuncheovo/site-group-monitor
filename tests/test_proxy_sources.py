"""代理配置新功能测试：批量添加 / API 源 CRUD / fetch_and_store / 幂等。

覆盖验收点：
    (a) POST /api/proxies/batch 解析多行并幂等添加；
    (b) POST /api/proxies/api-sources + GET 列表，密码脱敏 + proxy_count；
    (c) fetch_and_store 入库 source="api"/source_ref，并对消失的 IP 软 retire；
    (d) ProxyApiSourceRepository.save 按 url 幂等。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from site_group_monitor.core.proxy_api_fetcher import fetch_and_store, parse_proxy_line, parse_response_body
from site_group_monitor.models.entities import ProxyApiSource
from site_group_monitor.storage.repository import Repositories
from site_group_monitor.web import deps


# ------------------------------------------------------------------ (a) 批量添加
def test_batch_proxy_parses_and_adds_idempotently(client: TestClient) -> None:
    """batch 接口：2 行有效 + 1 行非法 → added=2, invalid=['badline']。"""
    resp = client.post(
        "/api/system/proxies/batch",
        json={"protocol": "http", "raw": "1.2.3.4:8080\n5.6.7.8:8888:u:p\nbadline"},
    )
    payload = resp.json()
    assert payload["code"] == 0, payload
    data = payload["data"]
    assert data["added"] == 2, data
    assert data["invalid"] == ["badline"], data

    # 再提交一次相同行 → 全部 skipped（幂等）
    resp2 = client.post(
        "/api/system/proxies/batch",
        json={"protocol": "http", "raw": "1.2.3.4:8080\n5.6.7.8:8888:u:p"},
    )
    data2 = resp2.json()["data"]
    assert data2["added"] == 0, data2
    assert data2["skipped"] == 2, data2

    # 列表中可见 source=batch
    listed = client.get("/api/system/proxies").json()["data"]
    batch_items = [it for it in listed["items"] if it["source"] == "batch"]
    assert len(batch_items) == 2, listed
    assert listed["batch_count"] == 2, listed


# ------------------------------------------------------------------ (b) API 源 CRUD
def test_api_source_create_and_list_masks_password(client: TestClient) -> None:
    """API 源：创建 → 列表脱敏密码 + 显示 proxy_count。"""
    create = client.post(
        "/api/system/proxies/api-sources",
        json={"name": "test", "url": "https://example.com/api/proxies",
              "protocol": "http", "username": "u", "password": "secret",
              "refresh_interval_min": 15, "enabled": True},
    )
    assert create.json()["code"] == 0, create.json()
    src_id = create.json()["data"]["id"]
    assert src_id

    listed = client.get("/api/system/proxies/api-sources").json()["data"]
    assert len(listed) == 1, listed
    item = listed[0]
    assert item["id"] == src_id
    assert item["url"] == "https://example.com/api/proxies"
    assert item["password"] == "***", item
    assert "password_enc" not in item, item
    assert item["proxy_count"] == 0, item

    # 策略可以单独修改，不必删除重建 API 源，也不能覆盖密码等配置。
    changed = client.patch(
        f"/api/system/proxies/api-sources/{src_id}/policy",
        json={"rotate_per_check": True},
    ).json()
    assert changed["code"] == 0, changed
    assert "每查询一个站" in changed["data"]["strategy"]
    updated = client.get("/api/system/proxies/api-sources").json()["data"][0]
    assert updated["rotate_per_check"] == 1
    assert updated["password"] == "***"

    # 删除
    deleted = client.delete(f"/api/system/proxies/api-sources/{src_id}").json()
    assert deleted["code"] == 0, deleted
    assert deleted["data"]["removed"] is True

    listed_after = client.get("/api/system/proxies/api-sources").json()["data"]
    assert listed_after == [], listed_after


# ------------------------------------------------------------------ (c) fetch_and_store
class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeClient:
    """替代 httpx.Client：构造时给定响应体。"""

    def __init__(self, text: str, **_: Any) -> None:
        self._text = text

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def get(self, url: str) -> "_FakeResponse":
        return _FakeResponse(self._text)


def test_fetch_and_store_upserts_and_reconciles(repos: Repositories, tmp_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_and_store：拉取 2 个 IP 入库 source=api/source_ref；消失的 IP 被软 retire。"""
    import httpx

    import site_group_monitor.core.proxy_api_fetcher as fetcher

    monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeClient("1.2.3.4:8080\n5.6.7.8:8888"))

    source = ProxyApiSource(url="https://example.com/api/proxies", protocol="http",
                            refresh_interval_min=30, created_at="2024-01-01T00:00:00Z")
    source_id = repos.proxies_api.save(source)

    result = fetch_and_store(source, repos, tmp_settings)
    assert result["ok"] is True, result
    assert result["added"] == 2, result

    proxies = repos.proxies.list_by_source("api", str(source_id))
    assert len(proxies) == 2, proxies
    for p in proxies:
        assert p.source == "api"
        assert p.source_ref == str(source_id)

    # 对账：下一轮只剩 1 个 IP，另一个应被软 retire
    monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeClient("1.2.3.4:8080"))
    result2 = fetch_and_store(source, repos, tmp_settings)
    assert result2["ok"] is True, result2
    assert result2["disabled"] == 1, result2

    after = repos.proxies.list_by_source("api", str(source_id))
    disabled = [p for p in after if p.disabled]
    assert len(disabled) == 1, after
    assert len(after) == 2, after  # 旧 IP 仍保留（仅禁用）


def test_fetch_and_store_handles_network_error(repos: Repositories, tmp_settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_and_store：网络异常时返回 ok=False 且不抛异常。"""
    import httpx

    import site_group_monitor.core.proxy_api_fetcher as fetcher

    def _boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "Client", _boom)
    source = ProxyApiSource(url="https://example.com/api/proxies", protocol="http")
    repos.proxies_api.save(source)
    result = fetch_and_store(source, repos, tmp_settings)
    assert result["ok"] is False, result
    assert "network down" in result["error"], result


def test_parse_proxy_line_formats() -> None:
    """解析单行代理的各种格式。"""
    assert parse_proxy_line("1.2.3.4:8080") == {
        "host": "1.2.3.4", "port": 8080, "protocol": "", "username": "", "password": ""}
    p = parse_proxy_line("5.6.7.8:8888:u:p")
    assert p["host"] == "5.6.7.8" and p["port"] == 8888 and p["username"] == "u" and p["password"] == "p"
    p2 = parse_proxy_line("https://9.9.9.9:3128")
    assert p2["protocol"] == "https" and p2["host"] == "9.9.9.9"
    p3 = parse_proxy_line("socks5://a:b@9.9.9.9:1080")
    assert p3["protocol"] == "socks5" and p3["username"] == "a" and p3["password"] == "b"
    assert parse_proxy_line("not-a-proxy") is None
    assert parse_proxy_line("999.999.999.999:bad") is None


def test_parse_response_body_json() -> None:
    """解析 JSON 列表（list / dict 包裹）与纯文本。"""
    body_list = '["1.2.3.4:8080", {"host": "5.6.7.8", "port": 8888, "protocol": "https"}]'
    items = parse_response_body(body_list, default_protocol="http")
    assert len(items) == 2, items
    assert items[1]["protocol"] == "https"

    wrapped = '{"code":0,"data":[{"ip":"7.7.7.7","port":80}]}'
    items2 = parse_response_body(wrapped, default_protocol="http")
    assert len(items2) == 1, items2
    assert items2[0]["host"] == "7.7.7.7"

    text = "1.1.1.1:80\n2.2.2.2:90"
    items3 = parse_response_body(text, default_protocol="http")
    assert len(items3) == 2, items3


# ------------------------------------------------------------------ (d) 幂等
def test_api_source_save_idempotent_on_url(repos: Repositories) -> None:
    """ProxyApiSourceRepository.save 按 url 幂等。"""
    s1 = ProxyApiSource(url="https://example.com/api/proxies", name="a", refresh_interval_min=30)
    id1 = repos.proxies_api.save(s1)
    s2 = ProxyApiSource(url="https://example.com/api/proxies", name="b", refresh_interval_min=60)
    id2 = repos.proxies_api.save(s2)
    assert id1 == id2, (id1, id2)
    # 更新后的 name 应覆盖
    got = repos.proxies_api.get_by_id(id1)
    assert got is not None
    assert got.name == "b", got.name
    assert got.refresh_interval_min == 60, got.refresh_interval_min
    assert len(repos.proxies_api.list_all()) == 1


def test_migrate_v3_adds_source_ref_and_api_table(tmp_path: Any) -> None:
    """已有 v3 库升级：补齐 source_ref 列与 proxy_api_sources 表，代理可正常写入。"""
    import sqlite3

    from site_group_monitor.models.entities import ProxyEndpoint
    from site_group_monitor.storage.database import Database
    from site_group_monitor.storage.repository import Repositories

    db_path = str(tmp_path / "old.db")
    # 构造一个「旧版」proxies 表（无 source_ref 列）并把版本置为 3
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE proxies (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, "
        "protocol TEXT DEFAULT 'http', host TEXT, port INTEGER, username TEXT, "
        "password_enc TEXT, weight REAL, success_count INTEGER, fail_count INTEGER, "
        "consecutive_fail INTEGER, health_score REAL, disabled INTEGER, source TEXT DEFAULT 'manual', "
        "last_ok_at TEXT, last_fail_at TEXT, created_at TEXT, UNIQUE(protocol, host, port))"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()

    db = Database(db_path, busy_timeout_ms=2000)
    info = db.init_db()
    assert info["schema_version"] == 6, info

    cols = {row[1] for row in db.connect().execute("PRAGMA table_info(proxies)")}
    assert "source_ref" in cols, cols
    assert "last_egress_ip" in cols, cols
    assert "last_location" in cols, cols
    assert "last_latency_ms" in cols, cols
    tables = info["tables"]
    assert "proxy_api_sources" in tables, tables
    api_cols = {row[1] for row in db.connect().execute("PRAGMA table_info(proxy_api_sources)")}
    assert "rotate_per_check" in api_cols, api_cols

    repos = Repositories(db)
    proxy = ProxyEndpoint(host="1.1.1.1", port=80, protocol="http", source="api", source_ref="7")
    pid = repos.proxies.save(proxy)
    got = repos.proxies.get_by_id(pid)
    assert got is not None
    assert got.source == "api"
    assert got.source_ref == "7"


def test_parse_ip_lookup_ip_api_and_ipinfo() -> None:
    """解析 ip-api / ipinfo / httpbin 三种常见出口查询响应。"""
    from site_group_monitor.core.proxy_probe import parse_ip_lookup

    ip, loc = parse_ip_lookup(
        '{"status":"success","country":"中国","regionName":"广东","city":"广州","isp":"电信","query":"1.2.3.4"}'
    )
    assert ip == "1.2.3.4"
    assert "广州" in loc and "中国" in loc

    ip2, loc2 = parse_ip_lookup('{"ip":"8.8.8.8","city":"Mountain View","region":"California","country":"US"}')
    assert ip2 == "8.8.8.8"
    assert "Mountain View" in loc2

    ip3, loc3 = parse_ip_lookup('{"origin":"9.9.9.9"}')
    assert ip3 == "9.9.9.9"
    assert loc3 == ""

    ip4, loc4 = parse_ip_lookup('{"ip":"1.2.3.4","pro":"广东省","city":"广州市"}')
    assert ip4 == "1.2.3.4"
    assert "广州" in loc4

    ip5, loc5 = parse_ip_lookup("218.65.97.92\n")
    assert ip5 == "218.65.97.92"


def test_fetch_and_store_rotate_deletes_old_and_coerces_https(
    repos: Repositories, tmp_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rotate 模式删除旧 IP；界面选 https 时入库仍用 http，并自动启用。"""
    import httpx

    monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeClient("1.2.3.4:8080"))
    source = ProxyApiSource(
        url="https://example.com/getips", protocol="https",
        rotate_per_check=True, created_at="2024-01-01T00:00:00Z",
    )
    source_id = repos.proxies_api.save(source)
    result = fetch_and_store(source, repos, tmp_settings, prune="delete")
    assert result["ok"] is True, result
    proxies = repos.proxies.list_by_source("api", str(source_id))
    assert len(proxies) == 1
    assert proxies[0].protocol == "http"
    assert proxies[0].disabled is False

    monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeClient("5.6.7.8:8888"))
    result2 = fetch_and_store(source, repos, tmp_settings, prune="delete")
    after = repos.proxies.list_by_source("api", str(source_id))
    assert result2["disabled"] == 1, result2
    assert len(after) == 1, after
    assert after[0].host == "5.6.7.8"
    assert after[0].disabled is False


def test_probe_and_store_writes_location(repos: Repositories, monkeypatch: pytest.MonkeyPatch) -> None:
    """探测成功后把出口 IP / 位置 / 延迟写回 proxies 表。"""
    import httpx

    from site_group_monitor.core.proxy_probe import probe_and_store
    from site_group_monitor.models.entities import ProxyEndpoint

    class _Resp:
        status_code = 200
        text = '{"status":"success","country":"中国","regionName":"北京","city":"北京","query":"203.0.113.9"}'
        content = text.encode("utf-8")
        headers: dict[str, str] = {}

    class _Client:
        def __init__(self, **_: Any) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def get(self, url: str) -> _Resp:
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    proxy = ProxyEndpoint(host="10.0.0.1", port=8080, protocol="http", source="api")
    proxy.id = repos.proxies.save(proxy)
    result = probe_and_store(proxy, repos)
    assert result["ok"] is True
    assert result["ip"] == "203.0.113.9"
    assert "北京" in result["location"]
    assert isinstance(result["latency_ms"], int)

    listed = repos.proxies.get_by_id(proxy.id)
    assert listed is not None
    assert listed.last_egress_ip == "203.0.113.9"
    assert "北京" in listed.last_location
    assert listed.last_latency_ms is not None


def test_refresh_api_source_returns_probes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """刷新 API 源接口同步返回拉取结果与探测列表。"""
    created = client.post(
        "/api/system/proxies/api-sources",
        json={"name": "rotating", "url": "https://example.com/get-ip", "protocol": "http", "enabled": True},
    )
    src_id = created.json()["data"]["id"]

    def _fake_fetch(source: Any, repos: Any, settings: Any, **_: Any) -> dict[str, Any]:
        from site_group_monitor.models.entities import ProxyEndpoint

        proxy = ProxyEndpoint(
            name="api#1:1.2.3.4:8080", protocol="http", host="1.2.3.4", port=8080,
            source="api", source_ref=str(source.id), created_at="2024-01-01T00:00:00Z",
        )
        repos.proxies.save(proxy)
        repos.proxies_api.update_fetch_result(source.id, 1, "")
        return {"ok": True, "added": 1, "updated": 0, "disabled": 0, "count": 1}

    def _fake_probe(source_id: int, repos: Any, **_: Any) -> list[dict[str, Any]]:
        return [{"ok": True, "proxy_id": 1, "url": "http://1.2.3.4:8080",
                 "ip": "198.51.100.7", "location": "美国 加州", "latency_ms": 120, "error": ""}]

    monkeypatch.setattr("site_group_monitor.core.proxy_api_fetcher.fetch_and_store", _fake_fetch)
    monkeypatch.setattr("site_group_monitor.core.proxy_probe.probe_source_proxies", _fake_probe)

    refreshed = client.post(f"/api/system/proxies/api-sources/{src_id}/refresh")
    payload = refreshed.json()
    assert payload["code"] == 0, payload
    data = payload["data"]
    assert data["ok"] is True
    assert data["count"] == 1
    assert data["probes"][0]["ip"] == "198.51.100.7"
    assert data["probes"][0]["latency_ms"] == 120

    listed = client.get("/api/system/proxies").json()["data"]
    api_items = [it for it in listed["items"] if it["source"] == "api"]
    assert len(api_items) == 1, listed
