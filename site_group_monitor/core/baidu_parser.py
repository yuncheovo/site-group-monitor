"""百度结果页解析（**纯函数**，零 I/O，可用 fixture 单测）。

判定规则（对应架构文档 §1.5(b)，优先级从上到下命中即返回）：

| 优先级 | 特征 | 判定 | reason_code |
|---|---|---|---|
| 1 | HTTP 非 200 / 超时 / DNS / 代理失败 | unknown | network_error / proxy_failed |
| 2 | 验证码 / 风控特征 | unknown | captcha |
| 3 | 搜索框已出现但结果容器为空（隐性风控/加载中断） | unknown | silent_risk |
| 4 | 200 但正文 < 2KB 且无结果容器 | unknown | parse_failed |
| 5 | 明确空结果文案 | not_indexed | empty_result_page |
| 6 | 有结果 + nums>0 + 至少一个结果根域匹配 | indexed | ok_result_found |
| 7 | 有结果但无一 host 匹配（site: 语法疑似被忽略） | not_indexed | no_domain_match |
| 8 | 结构异常 / XPath 全落空 | unknown | parse_failed |

> 原则是「白名单优先于黑名单」：**只有证据充分才判 not_indexed**，其余一律降级 unknown。
"""

from __future__ import annotations

import re
from typing import Any

from parsel import Selector

from site_group_monitor.models.entities import BaiduParseOutcome, IndexResult
from site_group_monitor.models.enums import IndexReasonCode, IndexStatus
from site_group_monitor.utils.netutil import registrable_domain, same_registrable_domain, sha1_short, strip_scheme_and_path
from site_group_monitor.utils.timeutil import utcnow_iso

# ------------------------------------------------------------------ 选择器集中管理（百度改版时只改这里）
SELECTORS: dict[str, str] = {
    "content_root": "#content_left",
    "result_nodes": (
        "div.result.c-container | div.result-op.c-container | div.result | "
        "div.result-op | div.c-container.xpath-result"
    ),
    "title": "h3::text | h3 a::text | .c-title::text",
    "link": "h3 a::attr(href) | a::attr(href)",
    "showurl": "span.c-showurl::text | a.c-showurl::text | div.f13 a::attr(href)",
    "mu": "::attr(mu)",
    "nums_text": ".nums_text::text | #tsn_inner::text | #content_left .nums::text",
}

#: 验证码 / 风控特征（命中即降级 unknown）
CAPTCHA_MARKERS: tuple[str, ...] = (
    "百度安全验证",
    "安全验证",
    "请输入验证码",
    "SeccodeInput",
    "wappass.baidu.com",
    "verify.baidu.com",
    "/su/",
    "antispider",
    "异常流量",
    "系统检测到您的网络中存在异常访问请求",
    "heidun.baidu.com",
    "请将图片旋转",
    "请完成拼图",
    "请拖动滑块",
    "tcaptcha",
)

#: 明确空结果文案（规则 4）
EMPTY_HINT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"没有找到与.{0,80}相关的网页"),
    re.compile(r"很抱歉.{0,20}没有找到"),
    re.compile(r"未找到相关结果"),
    re.compile(r"没有找到该\s*URL", re.IGNORECASE),
    re.compile(r"query\s+is\s+malformed", re.IGNORECASE),
)

#: 结果数量文案，如「百度为您找到相关结果约 1,230 个」
NUMS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"百度为您找到(?:相关结果)?约?\s*([\d,，]+)\s*个"),
    re.compile(r"找到相关结果约?\s*([\d,，]+)\s*个"),
    re.compile(r"(?:约|about)\s*([\d,，]+)\s*(?:条|个)\s*结果"),
)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_MIN_PAGE_LEN = 2048


# ================================================================== 基础工具
def extract_title(html: str) -> str:
    """抽取页面 ``<title>`` 文本（用于验证码识别）。

    Args:
        html: 页面 HTML。

    Returns:
        title 文本（清洗后）。
    """
    match = _TITLE_RE.search(html or "")
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def detect_captcha(html: str) -> tuple[bool, str]:
    """检测是否命中验证码 / 风控页。

    Args:
        html: 页面 HTML。

    Returns:
        ``(是否命中, 命中特征)``。
    """
    text = html or ""
    title = extract_title(text)
    for marker in CAPTCHA_MARKERS:
        if marker.lower() in text.lower() or marker.lower() in title.lower():
            return True, marker
    return False, ""


def detect_silent_risk(html: str) -> tuple[bool, str]:
    """识别百度返回 HTTP 200、但结果区为空的隐性风控页。

    这类页面通常已经显示搜索框和 ``site:`` 查询词，却没有结果容器、
    明确空结果文案或验证码。部分慢代理还会只收到完整 ``head``，没有
    ``body``。两种情况都不能据此判断未收录，应由上层刷新/换 IP 重试。
    """
    text = html or ""
    lower = text.lower()
    title = extract_title(text).lower()
    has_site_query = "site:" in title or re.search(
        r"(?:value|content)=[\"'][^\"']*site%?3?a?[:：]?[A-Za-z0-9]",
        text,
        re.IGNORECASE,
    ) is not None
    if not has_site_query:
        return False, ""

    # 响应在 head 阶段中断：截图常见只剩搜索框，DOM 没有正文。
    if "<body" not in lower:
        return True, "incomplete_document"

    has_search_shell = (
        'id="wrapper"' in lower
        or "id='wrapper'" in lower
        or "百度一下" in text
    )
    has_result_area = (
        'id="container"' in lower
        or "id='container'" in lower
        or 'id="content_left"' in lower
        or "id='content_left'" in lower
    )
    if has_search_shell and not has_result_area:
        return True, "blank_search_shell"
    return False, ""


def detect_empty_hint(html: str) -> tuple[bool, str]:
    """检测是否存在「明确空结果」文案。

    Args:
        html: 页面 HTML。

    Returns:
        ``(是否命中, 命中文案)``。
    """
    if not html:
        return False, ""
    flat = re.sub(r"<[^>]+>", " ", html)
    flat = re.sub(r"\s+", " ", flat)
    for pattern in EMPTY_HINT_PATTERNS:
        match = pattern.search(flat)
        if match:
            return True, match.group(0)
    return False, ""


def detect_nums_count(html: str) -> int:
    """抽取「百度为您找到相关结果约 N 个」中的 N。

    Args:
        html: 页面 HTML。

    Returns:
        结果数量；未取到返回 -1。
    """
    if not html:
        return -1
    flat = re.sub(r"<[^>]+>", " ", html)
    flat = re.sub(r"\s+", " ", flat)
    for pattern in NUMS_PATTERNS:
        match = pattern.search(flat)
        if match:
            digits = match.group(1).replace(",", "").replace("，", "")
            try:
                return int(digits)
            except ValueError:
                continue
    return -1


def host_from_url(url: str) -> str:
    """从 URL 取 host（失败返回空串）。

    Args:
        url: URL 字符串。

    Returns:
        host 字符串。
    """
    if not url:
        return ""
    return strip_scheme_and_path(url)


# ================================================================== 主解析
def parse(html: str, domain: str) -> BaiduParseOutcome:
    """纯函数：解析百度结果页 HTML（**不做任何网络/文件 I/O**）。

    Args:
        html: 结果页 HTML 字符串。
        domain: 目标域名（已归一化）。

    Returns:
        :class:`BaiduParseOutcome`。
    """
    html_text = html or ""
    outcome = BaiduParseOutcome(page_len=len(html_text), raw_hash=sha1_short(html_text, 16))

    has_captcha, marker = detect_captcha(html_text)
    outcome.has_captcha = has_captcha
    outcome.captcha_marker = marker
    if has_captcha:
        return outcome

    has_silent_risk, risk_marker = detect_silent_risk(html_text)
    outcome.has_silent_risk = has_silent_risk
    outcome.silent_risk_marker = risk_marker
    if has_silent_risk:
        return outcome

    selector = _safe_selector(html_text)
    hits = _extract_hits(selector, html_text)
    outcome.hits = hits
    # 注意：必须判断**真实结果节点**是否存在，不能用 #content_left 容器是否出现——
    # 空结果页同样会渲染 #content_left（内含 .hint-write-middle 提示块），
    # 否则会把「明确未收录」误升级为「有结果但未匹配」，破坏 not_indexed 判定。
    outcome.has_result_nodes = bool(hits) or bool(_safe_css(selector, SELECTORS["result_nodes"]))

    empty_hit, empty_text = detect_empty_hint(html_text)
    outcome.has_empty_hint = empty_hit
    outcome.empty_hint_text = empty_text

    outcome.nums_count = detect_nums_count(html_text)

    target_root = registrable_domain(domain)
    for hit in hits:
        hit_root = registrable_domain(hit.get("host", ""))
        if hit_root and hit_root == target_root:
            outcome.domain_matched = True
            break
        if hit.get("url") and same_registrable_domain(host_from_url(hit["url"]), target_root):
            outcome.domain_matched = True
            break
    return outcome


def _safe_selector(html: str) -> Selector | None:
    """构造 Selector（失败返回 None，交由上层降级 unknown）。

    Args:
        html: HTML 文本。

    Returns:
        ``Selector`` 或 None。
    """
    try:
        return Selector(text=html)
    except Exception:  # noqa: BLE001
        return None


def _safe_css(selector: Selector | None, expression: str) -> list[Any]:
    """安全执行 CSS 选择器（支持 ``|`` 或 ``,`` 分隔的多个表达式，结果合并去重）。

    Args:
        selector: Selector。
        expression: CSS 表达式。

    Returns:
        节点列表。
    """
    if selector is None:
        return []
    nodes: list[Any] = []
    seen: set[int] = set()
    for part in expression.replace("|", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            found = list(selector.css(part))
        except Exception:  # noqa: BLE001
            continue
        for node in found:
            marker = id(node)
            if marker in seen:
                continue
            seen.add(marker)
            nodes.append(node)
    return nodes


def _extract_hits(selector: Selector | None, html: str) -> list[dict[str, str]]:
    """抽取结果条目结构：``[{url, host, title}, ...]``。

    Args:
        selector: Selector。
        html: 原始 HTML（用于兜底提取 mu 属性）。

    Returns:
        结果条目列表。
    """
    hits: list[dict[str, str]] = []
    nodes: list[Any] = []
    if selector is not None:
        root_nodes = _safe_css(selector, SELECTORS["content_root"])
        for root in root_nodes:
            try:
                nodes.extend(list(root.css(SELECTORS["result_nodes"])))
            except Exception:  # noqa: BLE001
                continue
        if not nodes:
            nodes = _safe_css(selector, SELECTORS["result_nodes"])

    for node in nodes[:20]:
        url = _first_text(node, SELECTORS["link"]) or _attr(node, "mu")
        title = re.sub(r"\s+", " ", _first_text(node, SELECTORS["title"])).strip()
        showurl = _first_text(node, SELECTORS["showurl"])
        host = host_from_url(showurl) or host_from_url(url)
        # 百度跳转链接（www.baidu.com/link?url=...）无法直接得到目标 host，标记为未知
        if host and ("baidu.com" in host):
            host_from_mu = host_from_url(_attr(node, "mu"))
            host = host_from_mu or ""
        hits.append({"url": url or "", "host": host or "", "title": title})

    if not hits:
        # 兜底：直接从 HTML 里提取「mu=」真实目标（百度新版常见）
        for candidate in re.findall(r'mu="(https?://[^"]+)"', html)[:20]:
            hits.append(
                {
                    "url": candidate,
                    "host": host_from_url(candidate),
                    "title": "",
                }
            )
    return [hit for hit in hits if hit.get("url") or hit.get("host")]


def _first_text(node: Any, expression: str) -> str:
    """在节点内按表达式取首个文本。

    Args:
        node: Selector 节点。
        expression: CSS/XPath 混合表达式（``|`` 分隔）。

    Returns:
        文本（可能为空串）。
    """
    for part in expression.split("|"):
        part = part.strip()
        if not part:
            continue
        try:
            value = node.css(part).get() if "::" in part else node.xpath(part).get()
        except Exception:  # noqa: BLE001
            continue
        if value:
            return str(value).strip()
    return ""


def _attr(node: Any, name: str) -> str:
    """读取节点属性值。

    Args:
        node: Selector 节点。
        name: 属性名。

    Returns:
        属性值。
    """
    try:
        return str(node.attrib.get(name, "") or "")
    except Exception:  # noqa: BLE001
        return ""


# ================================================================== 判定
def decide(outcome: BaiduParseOutcome, domain: str) -> IndexResult:
    """按规则表把解析结果映射为三态收录结论（纯函数，零 I/O）。

    Args:
        outcome: :func:`parse` 的产出。
        domain: 目标域名。

    Returns:
        :class:`IndexResult`。
    """
    result = IndexResult()

    # 规则 2：验证码 / 风控
    if outcome.has_captcha:
        result.status = IndexStatus.UNKNOWN
        result.reason = IndexReasonCode.CAPTCHA
        result.confidence = 0.0
        return result

    # 规则 3：百度返回搜索页空壳或正文加载中断，属于隐性风控/代理异常。
    if outcome.has_silent_risk:
        result.status = IndexStatus.UNKNOWN
        result.reason = IndexReasonCode.SILENT_RISK
        result.confidence = 0.0
        return result

    # 规则 4：明确空结果文案（必须无结果节点才敢判未收录）
    # 注意：此规则必须**先于**「小页面降级」判断——百度的空结果页本身就很短
    # （通常在 1KB 左右），若先按页长判 unknown 就永远无法得出 not_indexed。
    if outcome.has_empty_hint and not outcome.has_result_nodes:
        result.status = IndexStatus.NOT_INDEXED
        result.reason = IndexReasonCode.EMPTY_RESULT_PAGE
        result.confidence = 0.9
        return result

    # 规则 3：返回内容过小且无结果容器 → 疑似空白 / 降权返回
    if not outcome.has_result_nodes and outcome.page_len < _MIN_PAGE_LEN:
        result.status = IndexStatus.UNKNOWN
        result.reason = IndexReasonCode.PARSE_FAILED
        result.confidence = 0.0
        return result

    # 规则 5：命中结果且至少一个结果归目标注册根域
    if outcome.has_result_nodes and outcome.domain_matched:
        result.status = IndexStatus.INDEXED
        result.reason = IndexReasonCode.OK_RESULT_FOUND
        result.result_count = outcome.nums_count if outcome.nums_count > 0 else len(outcome.hits)
        result.top_url = _pick_top_url(outcome, domain)
        result.confidence = 0.95 if outcome.nums_count > 0 else 0.75
        return result

    # 规则 6：检索页有结果但没有目标域名 → 按未收录
    if outcome.has_result_nodes and not outcome.domain_matched:
        result.status = IndexStatus.NOT_INDEXED
        result.reason = IndexReasonCode.NO_DOMAIN_MATCH
        result.result_count = outcome.nums_count if outcome.nums_count > 0 else len(outcome.hits)
        result.confidence = 0.7
        return result

    # 规则 7：结构异常，无法归类
    result.status = IndexStatus.UNKNOWN
    result.reason = IndexReasonCode.PARSE_FAILED
    result.confidence = 0.0
    return result


def _pick_top_url(outcome: BaiduParseOutcome, domain: str) -> str:
    """从命中结果里挑与目标根域最匹配的一条 URL（报告展示用）。

    Args:
        outcome: 解析结果。
        domain: 目标域名。

    Returns:
        URL 字符串。
    """
    target_root = registrable_domain(domain)
    for hit in outcome.hits:
        if same_registrable_domain(hit.get("host", ""), target_root):
            return hit.get("url") or hit.get("host") or ""
    for hit in outcome.hits:
        if hit.get("url"):
            return hit["url"]
    return ""


def parse_and_decide(html: str, domain: str) -> IndexResult:
    """便捷函数：一次完成「解析 + 判定」（同样是纯函数）。

    Args:
        html: 结果页 HTML。
        domain: 目标域名。

    Returns:
        :class:`IndexResult`。
    """
    return decide(parse(html, domain), domain)


def outcome_to_dict(outcome: BaiduParseOutcome) -> dict[str, Any]:
    """把解析结果导出为可写日志的字典。

    Args:
        outcome: 解析结果。

    Returns:
        字典。
    """
    payload = outcome.model_dump()
    payload["checked_at"] = utcnow_iso()
    return payload
