"""百度结果页解析单测（离线 fixture 驱动，不联网）。

覆盖架构文档 §1.5(b) 规则表：
    * 命中 → indexed / ok_result_found；
    * 明确空结果文案 → not_indexed / empty_result_page；
    * 验证码/风控 → unknown / captcha（**绝不误判未收录**）；
    * 结构异常 → unknown / parse_failed；
    * 有结果但无一 host 匹配 → unknown / no_domain_match（触发窄化复核）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from site_group_monitor.core.baidu_parser import (
    decide,
    detect_captcha,
    detect_empty_hint,
    detect_nums_count,
    parse,
    parse_and_decide,
)
from site_group_monitor.models.enums import IndexReasonCode, IndexStatus

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load(name: str) -> str:
    """读取 fixture HTML。

    Args:
        name: 文件名。

    Returns:
        HTML 文本。
    """
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_indexed_hit() -> None:
    """fixture：明确命中 —— 有结果节点 + nums > 0 + 结果 host 匹配目标根域。"""
    outcome = parse(load("baidu_indexed.html"), "example.com")
    assert outcome.has_result_nodes is True
    assert outcome.domain_matched is True
    assert outcome.nums_count == 12300
    assert not outcome.has_captcha

    result = decide(outcome, "example.com")
    assert result.status == IndexStatus.INDEXED
    assert result.reason == IndexReasonCode.OK_RESULT_FOUND
    assert result.confidence >= 0.9
    assert result.top_url.startswith("https://www.example.com")


def test_parse_indexed_with_www_domain() -> None:
    """目标域名带 www 时也应正确匹配（展示用去重场景）。"""
    result = parse_and_decide(load("baidu_indexed.html"), "www.example.com")
    assert result.status == IndexStatus.INDEXED


def test_parse_empty_result_page() -> None:
    """fixture：明确空结果文案 —— 判 un_indexed（唯一敢判未收录的场景）。"""
    outcome = parse(load("baidu_empty.html"), "notfound-example.com")
    assert outcome.has_empty_hint is True
    assert outcome.has_result_nodes is False

    result = decide(outcome, "notfound-example.com")
    assert result.status == IndexStatus.NOT_INDEXED
    assert result.reason == IndexReasonCode.EMPTY_RESULT_PAGE
    assert result.confidence >= 0.8


def test_parse_captcha_degrades_to_unknown() -> None:
    """fixture：验证码页 —— 必须降级 unknown/captcha，绝不能判未收录。"""
    outcome = parse(load("baidu_captcha.html"), "example.com")
    assert outcome.has_captcha is True

    result = decide(outcome, "example.com")
    assert result.status == IndexStatus.UNKNOWN
    assert result.reason == IndexReasonCode.CAPTCHA
    assert result.status != IndexStatus.NOT_INDEXED


def test_parse_broken_structure_degrades_to_unknown() -> None:
    """fixture：结构异常 —— 无结果也无空文案，降级 unknown/parse_failed。"""
    outcome = parse(load("baidu_broken.html"), "example.com")
    result = decide(outcome, "example.com")
    assert result.status == IndexStatus.UNKNOWN
    assert result.reason == IndexReasonCode.PARSE_FAILED
    assert result.status != IndexStatus.NOT_INDEXED


def test_tiny_page_degrades_to_unknown() -> None:
    """返回内容过小且无结果容器 —— 降级 unknown/parse_failed（疑似白页/降权）。"""
    result = parse_and_decide("<html><body>hi</body></html>", "example.com")
    assert result.status == IndexStatus.UNKNOWN
    assert result.reason == IndexReasonCode.PARSE_FAILED


def test_no_domain_match_is_not_indexed() -> None:
    """有结果但无一 host 匹配目标域 —— 判未收录。"""
    html = """
    <html><body>
      <div id="content_left">
        <div class="result c-container" mu="https://www.other-site.com/">
          <h3 class="t"><a href="https://www.other-site.com/">其他网站标题</a></h3>
          <span class="c-showurl">www.other-site.com/</span>
        </div>
      </div>
      <div class="nums_text">百度为您找到相关结果约 3,000,000 个</div>
    </body></html>
    """
    outcome = parse(html, "example.com")
    assert outcome.has_result_nodes is True
    assert outcome.domain_matched is False

    result = decide(outcome, "example.com")
    assert result.status == IndexStatus.NOT_INDEXED
    assert result.reason == IndexReasonCode.NO_DOMAIN_MATCH


def test_captcha_markers_detection() -> None:
    """验证码关键词逐个验证（标题与正文都要能命中）。"""
    for marker in ("百度安全验证", "请输入验证码", "wappass.baidu.com", "SeccodeInput",
                   "antispider", "异常流量", "/su/", "请将图片旋转", "请拖动滑块"):
        html = f"<html><head><title>t</title></head><body>{marker}</body></html>"
        hit, found = detect_captcha(html)
        assert hit is True, f"未识别验证码特征: {marker}"
        assert found
    title_html = "<html><head><title>百度安全验证</title></head><body>x</body></html>"
    assert detect_captcha(title_html)[0] is True


def test_empty_hint_detection() -> None:
    """空结果文案正则验证（多种常见写法）。"""
    for text in (
        "抱歉，没有找到与 site:abc.com 相关的网页。",
        "很抱歉，没有找到相关的网页",
        "未找到相关结果",
        "没有找到该URL",
    ):
        hit, matched = detect_empty_hint(f"<html><body><div>{text}</div></body></html>")
        assert hit is True, f"未识别空结果文案: {text}"
        assert matched


def test_nums_count_detection() -> None:
    """结果数量抽取验证（含千分位逗号）。"""
    assert detect_nums_count("<div>百度为您找到相关结果约 1,230 个</div>") == 1230
    assert detect_nums_count("<div>百度为您找到相关结果约5个</div>") == 5
    assert detect_nums_count("<div>no numbers here</div>") == -1


def test_domain_matching_is_root_based() -> None:
    """Host 归属判定走注册根域，避免二级域名误匹配。"""
    html = """
    <html><body>
      <div id="content_left">
        <div class="result c-container" mu="https://news.example.com.cn/index.html">
          <h3 class="t"><a href="https://news.example.com.cn/index.html">标题</a></h3>
          <span class="c-showurl">news.example.com.cn/</span>
        </div>
      </div>
      <div class="nums_text">百度为您找到相关结果约 10 个</div>
    </body></html>
    """
    outcome = parse(html, "example.com.cn")
    assert outcome.domain_matched is True
    result = decide(outcome, "example.com.cn")
    assert result.status == IndexStatus.INDEXED


@pytest.mark.parametrize(
    "fixture,domain,expected_status",
    [
        ("baidu_indexed.html", "example.com", IndexStatus.INDEXED),
        ("baidu_empty.html", "notfound-example.com", IndexStatus.NOT_INDEXED),
        ("baidu_captcha.html", "example.com", IndexStatus.UNKNOWN),
        ("baidu_broken.html", "example.com", IndexStatus.UNKNOWN),
    ],
)
def test_fixture_matrix(fixture: str, domain: str, expected_status: IndexStatus) -> None:
    """四类 fixture 的整体矩阵校验（快速回归入口）。

    Args:
        fixture: fixture 文件名。
        domain: 目标域名。
        expected_status: 期望的三态结果。
    """
    assert parse_and_decide(load(fixture), domain).status == expected_status
