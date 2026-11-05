"""TDK 解析单测：编码探测（utf-8 / gbk / gb18030）与字段抽取/缺失标记。"""

from __future__ import annotations

from site_group_monitor.core.tdk_parser import parse_html


def test_parse_utf8_html() -> None:
    """UTF-8 页面正常抽取 title / description / keywords。"""
    raw = (
        "<html><head>"
        "<title>示例站点标题 </title>"
        '<meta name="description" content="这是一个示例描述"/>'
        '<meta name="keywords" content="示例,关键词,SEO"/>'
        '<meta charset="utf-8"/>'
        "</head><body>ok</body></html>"
    ).encode("utf-8")
    info = parse_html(raw, "https://example.com/")
    assert info.title == "示例站点标题"
    assert info.description == "这是一个示例描述"
    assert info.keywords == "示例,关键词,SEO"
    assert info.encoding.lower() in ("utf-8", "utf_8")
    assert info.title_missing is False
    assert info.desc_missing is False
    assert info.keywords_missing is False
    assert info.title_hash and info.desc_hash


def test_parse_gbk_html_without_meta() -> None:
    """GBK 页面（无 meta charset）依赖 charset-normalizer / gb18030 兜底解码。"""
    html = (
        "<html><head><title>中文站点标题</title>"
        '<meta name="description" content="中文描述内容"/>'
        "</head><body>中文正文内容测试</body></html>"
    )
    raw = html.encode("gb18030")
    info = parse_html(raw, "https://example.com/")
    assert info.title == "中文站点标题"
    assert info.description == "中文描述内容"
    assert info.encoding.lower() in ("gb18030", "gb2312", "gbk", "big5", "cp936", "hz")
    assert info.title_missing is False


def test_parse_missing_fields_are_empty_not_error() -> None:
    """缺失字段一律填空串 + missing 标记，不抛异常。"""
    raw = b"<html><head><title></title></head><body>no meta</body></html>"
    info = parse_html(raw, "https://example.com/")
    assert info.title == ""
    assert info.description == ""
    assert info.keywords == ""
    assert info.title_missing is True
    assert info.desc_missing is True
    assert info.keywords_missing is True
    assert info.error_type == "none"


def test_description_fallback_to_og_and_twitter() -> None:
    """description 缺失时依次回落 og:description / twitter:description。"""
    og_html = (
        '<html><head><title>T</title>'
        '<meta property="og:description" content="og 描述"/></head><body>x</body></html>'
    ).encode("utf-8")
    info = parse_html(og_html, "https://example.com/")
    assert info.description == "og 描述"
    assert info.desc_source == "og"

    tw_html = (
        '<html><head><title>T</title>'
        '<meta name="twitter:description" content="twitter 描述"/></head><body>x</body></html>'
    ).encode("utf-8")
    info = parse_html(tw_html, "https://example.com/")
    assert info.description == "twitter 描述"
    assert info.desc_source == "twitter"


def test_description_fallback_disabled() -> None:
    """关闭回落开关后不使用 og/twitter 描述。"""
    og_html = (
        '<html><head><title>T</title>'
        '<meta property="og:description" content="og 描述"/></head><body>x</body></html>'
    ).encode("utf-8")
    info = parse_html(og_html, "https://example.com/", fallback_meta=False)
    assert info.description == ""
    assert info.desc_missing is True


def test_placeholder_title_falls_back_to_og() -> None:
    """WAF 壳页标题不能入库，应改用 og:title / description。"""
    raw = (
        "<html><head><title>页面加载中...</title>"
        '<meta property="og:title" content="秋法网"/>'
        '<meta name="description" content="法律咨询平台"/>'
        '<meta name="keywords" content="法律,咨询"/>'
        "</head><body><h1>秋法网</h1></body></html>"
    ).encode("utf-8")
    info = parse_html(raw, "https://qiufa.com/")
    assert info.title == "秋法网"
    assert info.title_missing is False
    assert info.description == "法律咨询平台"
    assert info.keywords == "法律,咨询"


def test_broken_html_does_not_raise() -> None:
    """破损 HTML 也不应抛异常（lxml/parsel 容错）。"""
    raw = "<html><head><title>未闭合<title><body><p>段落<div>内容".encode("utf-8")
    info = parse_html(raw, "https://example.com/")
    assert info.error_type == "none"
    assert isinstance(info.title, str)


def test_content_type_hint_is_used() -> None:
    """优先使用 HTTP Content-Type 中的 charset。"""
    raw = (
        "<html><head><title>标题</title></head><body>内容</body></html>"
    ).encode("gb18030")
    info = parse_html(raw, "https://example.com/", content_type="text/html; charset=gb18030")
    assert info.title == "标题"
    assert info.encoding.lower() in ("gb18030", "gbk", "gb2312", "cp936")
