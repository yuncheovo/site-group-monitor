"""本机浏览器抓取：路径探测、代理参数、配置默认值（不启动真实浏览器）。"""

from __future__ import annotations

from pathlib import Path

from site_group_monitor.config.settings import BrowserFetchConfig, Settings
from site_group_monitor.core.local_browser import (
    _html_ready,
    _needs_screenshot,
    browser_window_count,
    build_launch_args,
    chrome_proxy_server,
    find_browser_executable,
    parse_wmic_profile_pids,
)
from site_group_monitor.models.entities import ProxyEndpoint


def test_find_browser_executable_hits_existing_file(tmp_path: Path, monkeypatch) -> None:
    from site_group_monitor.core import local_browser as lb

    target = tmp_path / "msedge.exe"
    target.write_bytes(b"mz")
    monkeypatch.setattr(lb, "_BROWSER_CANDIDATES", ((str(target), "msedge"),))
    for name in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        monkeypatch.setenv(name, str(tmp_path / "empty"))
    assert lb.find_browser_executable("msedge") == str(target)
    assert lb.find_browser_executable("chrome") == ""


def test_chrome_proxy_server_http_and_socks() -> None:
    http_proxy = ProxyEndpoint(protocol="http", host="1.2.3.4", port=8080)
    assert chrome_proxy_server(http_proxy) == "http://1.2.3.4:8080"
    socks = ProxyEndpoint(protocol="socks5", host="10.0.0.1", port=1080, username="u", password_enc="p")
    assert chrome_proxy_server(socks) == "socks5://u:p@10.0.0.1:1080"
    assert chrome_proxy_server(None) == ""


def test_html_ready_and_screenshot_only_for_baidu_search() -> None:
    assert _needs_screenshot("https://www.baidu.com/s?wd=site:example.com") is True
    assert _needs_screenshot("https://example.com/") is False
    search_html = "<div id='content_left'>" + ("x" * 900) + "百度为您找到相关结果约"
    assert _html_ready("https://www.baidu.com/s", search_html, "https://www.baidu.com/s") is True
    assert _html_ready("https://www.baidu.com/s", "<div id='content_left'>" + ("x" * 900), "https://www.baidu.com/s") is False
    assert _html_ready("https://example.com/", "<html><title>页面加载中</title></html>", "https://example.com/") is False


def test_headed_launch_args_open_visible_window() -> None:
    hidden = build_launch_args("msedge.exe", 9222, "C:/tmp/profile", headless=True)
    shown = build_launch_args("msedge.exe", 9222, "C:/tmp/profile", headless=False)
    assert "--headless=new" in hidden
    assert hidden[-1] == "about:blank"
    assert "--headless=new" not in shown
    assert "--new-window" not in shown
    assert "--disable-blink-features=AutomationControlled" not in shown
    assert "--disable-blink-features=AutomationControlled" not in hidden
    assert "--start-maximized" in shown
    assert "--edge-skip-compat-layer-relaunch" in shown
    assert "--remote-debugging-address=127.0.0.1" in shown
    assert "baidu.com" in shown[-1]


def test_parse_wmic_profile_pids_ignores_field_order() -> None:
    profile = r"C:\app\data\browser-profile"
    text = (
        "ProcessId=56464\n"
        f'CommandLine="msedge.exe" --user-data-dir="{profile}" --remote-debugging-port=1\n'
        "\n"
        "CommandLine=chrome.exe --user-data-dir=C:\\other\\profile\n"
        "ProcessId=99\n"
    )
    assert parse_wmic_profile_pids(text, profile) == {56464}


def test_browser_fetch_defaults_off() -> None:
    cfg = BrowserFetchConfig()
    assert cfg.enabled is False
    assert cfg.via_proxy is True
    assert cfg.channel == "auto"
    assert cfg.headless is False
    assert cfg.windows == 1
    settings = Settings()
    assert settings.engine.baidu.browser.enabled is False
    assert settings.engine.baidu.browser.via_proxy is True
    assert browser_window_count(settings) == 1
    settings.engine.baidu.browser.windows = 9
    assert browser_window_count(settings) == 8
    settings.engine.baidu.browser.windows = 3
    shown_multi = build_launch_args("msedge.exe", 9222, "C:/tmp/profile", headless=False, slot=2, windows=3)
    assert "--start-maximized" not in shown_multi
    assert "--window-position=136,102" in shown_multi
    from site_group_monitor.core.local_browser import BrowserPool, LocalBrowser

    settings.engine.baidu.browser.windows = 4
    pool = BrowserPool(settings, None)
    assert len(pool._slots) == 4
    assert [item._slot for item in pool._slots] == [0, 1, 2, 3]
    single_pool = BrowserPool(settings, None, max_slots=1)
    assert len(single_pool._slots) == 1
    assert single_pool._slots[0]._slot == 0
    one = LocalBrowser(settings, None, slot=1)
    assert str(one._profile_dir()).replace("\\", "/").endswith("/w1")
    assert hasattr(pool, "acquire") and hasattr(pool, "release") and hasattr(pool, "warm")


def test_describe_network_path_browser_direct_vs_proxy() -> None:
    from site_group_monitor.core.check_worker import describe_network_path
    from site_group_monitor.models.entities import ProxyEndpoint

    settings = Settings()
    settings.engine.baidu.browser.enabled = True
    settings.engine.baidu.browser.via_proxy = False
    proxy = ProxyEndpoint(
        protocol="http",
        host="10.0.0.8",
        port=8080,
        last_egress_ip="203.0.113.9",
        last_location="上海",
        last_latency_ms=28,
    )
    text = describe_network_path(settings, proxy)
    assert "本机直连" in text
    assert "203.0.113.9" in text
    settings.engine.baidu.browser.via_proxy = True
    text = describe_network_path(settings, proxy)
    assert "收录/TDK/网站状态：代理" in text
    assert "本机直连" not in text
    assert describe_network_path(settings, None) == "代理池暂无可用 IP，本次直连降级"
