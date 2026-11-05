"""代理池子命令：``sgm proxy add|remove|list|test|health``。

支持两种录入方式：
    * 单条 ``sgm proxy add --host 1.2.3.4 --port 8080 --protocol http``；
    * 批量 ``sgm proxy import proxies.txt``（每行 ``protocol://user:pass@host:port``）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from site_group_monitor.cli.output import echo_error, echo_ok, echo_warn
from site_group_monitor.core.proxy_pool import ProxyPool
from site_group_monitor.models.entities import ProxyEndpoint
from site_group_monitor.utils.netutil import mask_url
from site_group_monitor.utils.timeutil import utcnow_iso
from site_group_monitor.web.deps import get_container


def _pool() -> ProxyPool:
    """构造代理池对象（读取当前库内代理）。

    Returns:
        代理池实例。
    """
    container = get_container()
    return ProxyPool(container.repos.proxies, container.settings, None, container.logger)


@click.group("proxy", help="代理池管理")
def proxy_group() -> None:
    """代理池命令组。"""


@click.command("add", help="新增/更新一条代理")
@click.option("--host", required=True, help="代理主机")
@click.option("--port", required=True, type=int, help="代理端口")
@click.option("--protocol", default="http", type=click.Choice(["http", "https", "socks5"]),
              show_default=True)
@click.option("--username", default="", help="用户名")
@click.option("--password", default="", help="密码（会明文入库，展示一律脱敏）")
@click.option("--name", default="", help="备注名")
@click.option("--weight", default=1.0, show_default=True, help="权重")
def proxy_add(host: str, port: int, protocol: str, username: str, password: str, name: str,
              weight: float) -> None:
    """新增/更新代理。"""
    container = get_container()
    proxy = ProxyEndpoint(
        name=name, protocol=protocol, host=host, port=port, username=username,
        password_enc=password, weight=weight, created_at=utcnow_iso(), source="cli",
    )
    proxy_id = container.repos.proxies.save(proxy)
    container.audit.log("cli", "proxy.add", f"{host}:{port}", {"protocol": protocol})
    echo_ok(f"已保存代理 #{proxy_id}：{mask_url(proxy.url)}")


@click.command("import", help="批量导入代理文件（每行 protocol://user:pass@host:port）")
@click.argument("path", required=True)
def proxy_import(path: str) -> None:
    """批量导入代理。"""
    target = Path(path)
    if not target.is_file():
        echo_error(f"文件不存在：{path}")
        return
    container = get_container()
    added = 0
    invalid = 0
    for line_no, line in enumerate(target.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        proxy = _parse_proxy_line(text)
        if proxy is None:
            echo_warn(f"第 {line_no} 行无法解析：{text}")
            invalid += 1
            continue
        proxy.created_at = utcnow_iso()
        proxy.source = "cli"
        container.repos.proxies.save(proxy)
        added += 1
    container.audit.log("cli", "proxy.import", path, {"added": added, "invalid": invalid})
    echo_ok(f"代理导入完成：新增/更新 {added}，非法 {invalid}")


@click.command("remove", help="删除代理（id 或 host）")
@click.argument("identifier", required=True)
def proxy_remove(identifier: str) -> None:
    """删除代理。"""
    container = get_container()
    removed = container.repos.proxies.remove(identifier)
    if removed:
        container.audit.log("cli", "proxy.remove", identifier, {})
        echo_ok(f"已删除代理：{identifier}")
    else:
        echo_warn(f"未找到代理：{identifier}")


@click.command("list", help="列出代理（脱敏展示）")
def proxy_list() -> None:
    """列出代理列表。"""
    pool = _pool()
    stats = pool.stats()
    if not stats["items"]:
        echo_warn("代理池为空，检测会自动降级为直连（并发下降、间隔放大）")
        return
    for item in stats["items"]:
        state_text = "禁用" if item["disabled"] else "启用"
        click.echo(
            f"#{item['id']}\t{state_text}\t{item['url']}\t"
            f"健康度={item['health_score']:.2f}\t成功={item['success_count']}\t失败={item['fail_count']}"
        )
    click.echo(
        f"-- 合计 {stats['total']}｜可用 {stats['enabled']}｜健康 {stats['healthy']}｜"
        f"健康合格率 {stats['health_rate'] * 100:.1f}%"
    )


@click.command("test", help="测试代理连通性（访问百度首页）")
@click.argument("identifier", required=False, default="")
@click.option("--timeout", default=10.0, show_default=True, help="单次超时秒数")
@click.pass_context
def proxy_test(ctx: click.Context, identifier: str, timeout: float) -> None:
    """测试代理可用性。"""
    container = get_container()
    from site_group_monitor.core.http_client import HttpClient

    http = HttpClient(container.settings, container.logger)
    targets: list[ProxyEndpoint]
    if identifier:
        found = _find_proxy(identifier)
        targets = [found] if found else []
        if not targets:
            echo_warn(f"未找到代理：{identifier}")
            return
    else:
        targets = [item for item in container.repos.proxies.list_all() if not item.disabled]

    if not targets:
        echo_warn("没有可用代理")
        return

    async def _run() -> list[tuple[ProxyEndpoint, Any]]:
        results: list[tuple[ProxyEndpoint, Any]] = []
        for proxy in targets:
            response = await http.get(
                "https://www.baidu.com/", proxy=proxy, timeout_sec=timeout, follow_redirects=True
            )
            results.append((proxy, response))
        return results

    pairs = _await(_run())
    try:
        for proxy, response in pairs:
            if response.ok and response.status_code == 200:
                echo_ok(f"{proxy.masked_url} 可用（{response.status_code}，{response.elapsed_ms}ms）")
                pool = ProxyPool(container.repos.proxies, container.settings, None, container.logger)
                candidate = next((item for item in pool.proxies if item.key == proxy.key), proxy)
                pool.report_ok(candidate)
            else:
                echo_warn(
                    f"{proxy.masked_url} 不可用（status={response.status_code} "
                    f"error={response.error_type or response.error_msg}）"
                )
                if response.status_code == 0:
                    pool = ProxyPool(container.repos.proxies, container.settings, None, container.logger)
                    candidate = next((item for item in pool.proxies if item.key == proxy.key), proxy)
                    pool.report_fail(candidate, "fail")
    finally:
        _await(http.close())
    container.audit.log("cli", "proxy.test", identifier or "all", {"count": len(pairs)})


@click.command("health", help="代理池健康度统计")
@click.option("--json-output", is_flag=True, help="输出 JSON")
def proxy_health(json_output: bool) -> None:
    """代理池健康度统计。"""
    pool = _pool()
    stats = pool.stats()
    if json_output:
        click.echo(json.dumps(stats, ensure_ascii=False, indent=2))
        return
    click.echo(
        f"总数 {stats['total']}｜可用 {stats['enabled']}｜禁用 {stats['disabled']}｜"
        f"健康 {stats['healthy']}｜平均健康度 {stats['avg_health']:.2f}"
    )
    if stats["empty"]:
        echo_warn("代理池为空：已降级直连模式（并发 no_proxy_workers，间隔 × no_proxy_interval_factor）")


proxy_group.add_command(proxy_add)
proxy_group.add_command(proxy_import)
proxy_group.add_command(proxy_remove)
proxy_group.add_command(proxy_list)
proxy_group.add_command(proxy_test)
proxy_group.add_command(proxy_health)


def _parse_proxy_line(text: str) -> ProxyEndpoint | None:
    """解析一行代理文本。

    Args:
        text: 形如 ``http://user:pass@host:port`` 或 ``host:port``。

    Returns:
        代理实体；解析失败返回 None。
    """
    try:
        from urllib.parse import urlsplit

        candidate = text if "//" in text else f"http://{text}"
        parts = urlsplit(candidate)
        if not parts.hostname or not parts.port:
            return None
        protocol = parts.scheme or "http"
        if protocol.startswith("socks"):
            protocol = "socks5"
        return ProxyEndpoint(
            protocol=protocol,
            host=parts.hostname,
            port=int(parts.port),
            username=parts.username or "",
            password_enc=parts.password or "",
            source="cli",
        )
    except Exception:  # noqa: BLE001
        return None


def _find_proxy(identifier: str) -> ProxyEndpoint | None:
    """按 id 或 host 查找代理。

    Args:
        identifier: id 或 host。

    Returns:
        代理实体或 None。
    """
    container = get_container()
    if identifier.isdigit():
        return container.repos.proxies.get_by_id(int(identifier))
    for proxy in container.repos.proxies.list_all():
        if proxy.host == identifier:
            return proxy
    return None


def _await(coro: Any) -> Any:
    """在同步 CLI 环境执行协程。

    Args:
        coro: 协程对象。

    Returns:
        协程返回值。
    """
    try:
        loop = __import__("asyncio").get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():  # pragma: no cover
        return loop.run_until_complete(coro)
    return __import__("asyncio").run(coro)
