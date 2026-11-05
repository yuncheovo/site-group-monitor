"""网络 / 域名 / 编码相关工具。

提供：
    * 域名归一化 :func:`normalize_domain`；
    * 注册根域提取 :func:`registrable_domain`（内置常见多级后缀表，避免引入 tldextract）；
    * HTML 编码探测与解码 :func:`decode_html`（四级兜底）；
    * 文本清洗、哈希、脱敏等通用辅助。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote, urlsplit

# 常见的二级 / 多级公共后缀（够落地站群场景，避免引入 tldextract 依赖）
_MULTI_LEVEL_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "net.uk", "sch.uk",
        "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn", "ac.cn",
        "bj.cn", "sh.cn", "gd.cn", "js.cn", "zj.cn", "ah.cn", "fj.cn",
        "com.hk", "net.hk", "org.hk", "gov.hk", "edu.hk",
        "com.tw", "net.tw", "org.tw", "edu.tw", "gov.tw",
        "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
        "co.kr", "or.kr", "ne.kr", "go.kr",
        "com.au", "net.au", "org.au", "edu.au",
        "co.nz", "com.br", "com.mx", "com.ar", "co.za", "com.tr",
        "com.sg", "com.my", "com.vn", "com.ua", "com.pl",
        "com.ru", "net.ru", "org.ru",
    }
)

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)+$")
_META_CHARSET_RE = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""",
    re.IGNORECASE,
)
_META_EQUIV_RE = re.compile(
    rb"""<meta[^>]+http-equiv\s*=\s*["']?content-type["']?[^>]*content\s*=\s*["'][^"']*charset\s*=\s*([a-zA-Z0-9_\-]+)""",
    re.IGNORECASE,
)
_CT_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([a-zA-Z0-9_\-]+)", re.IGNORECASE)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


def strip_scheme_and_path(raw: str) -> str:
    """去掉 URL 的 scheme / 用户信息 / 端口 / 路径 / 查询串，仅留 host 部分。

    Args:
        raw: 原始输入，可能是 URL、裸域名或带端口的地址。

    Returns:
        小写的 host 字符串（仍可能包含前导 ``www.``）。

    Example:
        >>> strip_scheme_and_path("https://WWW.Example.com:443/a/b?c=1")
        'www.example.com'
    """
    text = str(raw or "").strip().strip("`'\"<> \t\r\n")
    if not text:
        return ""
    # 制表符 / 换行等控制字符会被 urlsplit 静默吞掉，导致
    # "example.com\tfoo" 变成 "example.comfoo"（两个 token 被拼成一个假域名）。
    # 域名输入里这类字符永远非法，直接判无效，绝不猜测。
    if _CONTROL_CHARS_RE.search(text) or "\t" in text or "\n" in text or "\r" in text:
        return ""
    if "//" not in text:
        text = f"//{text}"
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    host = parts.hostname or parts.netloc.split("@")[-1]
    host = host.split(":")[0] if ":" in host else host
    try:
        host = unquote(host)
    except Exception:  # noqa: BLE001 - 百分号解码失败保持原值
        pass
    return host.strip().strip("/").lower()


def normalize_domain(raw: str) -> str:
    """把任意形式输入归一化为监控用的域名（去 scheme / 端口 / 路径 / ``www.``）。

    Args:
        raw: 用户输入，如 ``https://WWW.Example.com:443/a/``。

    Returns:
        归一化域名；非法输入返回空串。

    Example:
        >>> normalize_domain("https://WWW.Example.com:443/a/")
        'example.com'
    """
    host = strip_scheme_and_path(raw)
    if not host:
        return ""
    # 幂等剥离前导 www.（支持 www.www.example.com 这类脏数据：
    # 只剥一层会导致 normalize(normalize(x)) != normalize(x)，进而产生重复站点）
    while host.startswith("www.") and host.count(".") > 1:
        host = host[4:]
    host = host.rstrip(".")
    # IDN（含中文域名）尝试 punycode 编码，便于后续 DNS/HTTP 使用
    if any(ord(char) > 127 for char in host):
        host = _idna_encode(host)
    if not _HOST_RE.match(host):
        return ""
    return host


def _idna_encode(host: str) -> str:
    """把含非 ASCII 字符的域名转为 punycode。

    Args:
        host: 小写域名。

    Returns:
        punycode 域名；转换失败返回原值。
    """
    try:
        return ".".join(
            part.encode("idna").decode("ascii") if any(ord(c) > 127 for c in part) else part
            for part in host.split(".")
        )
    except Exception:  # noqa: BLE001 - IDNA 编码失败保持原值
        return host


def is_valid_domain(domain: str) -> bool:
    """判断字符串是否像一个可监控的域名。

    Args:
        domain: 待校验域名。

    Returns:
        合法返回 True。
    """
    return bool(domain) and bool(_HOST_RE.match(domain))


def _is_ip_literal(host: str) -> bool:
    """判断 host 是否为 IPv4 字面量。

    Args:
        host: 已小写的 host。

    Returns:
        是 IPv4 字面量返回 True。
    """
    parts = host.split(".")
    if not (2 <= len(parts) <= 4):
        return False
    return all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)


def registrable_domain(host: str) -> str:
    """提取注册根域（用于在 `site:` 结果里做 host 归属匹配）。

    Args:
        host: host 或域名，如 ``news.example.com.cn``。

    Returns:
        注册根域，如 ``example.com.cn``；无法判断时原样返回 host。

    Example:
        >>> registrable_domain("news.example.com.cn")
        'example.com.cn'
    """
    domain = host.strip().lower().rstrip(".")
    if not domain or not _HOST_RE.match(domain):
        return domain
    # IP 字面量（含 1.2.3.4 这种 <3 段都是数字的写法）没有"注册根域"概念，
    # 原样返回，否则 1.2.3.4 会被切成 "3.4"，导致 site: 结果归属匹配到别的站。
    if _is_ip_literal(domain):
        return domain
    labels = domain.split(".")
    if len(labels) <= 2:
        return domain
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LEVEL_SUFFIXES:
        return ".".join(labels[-3:]) if len(labels) >= 3 else domain
    return last_two


def same_registrable_domain(left: str, right: str) -> bool:
    """判断两个 host / 域名是否属于同一注册根域（含 ``www.`` 归一化）。

    Args:
        left: 第一个 host。
        right: 第二个 host。

    Returns:
        同一注册根域返回 True。
    """
    if not left or not right:
        return False
    left_host = strip_scheme_and_path(left).removeprefix("www.")
    right_host = strip_scheme_and_path(right).removeprefix("www.")
    left_root = registrable_domain(left_host)
    right_root = registrable_domain(right_host)
    return bool(left_root) and left_root == right_root


def root_url_of(domain: str, prefer_https: bool = True) -> str:
    """构造站点首页 URL。

    Args:
        domain: 归一化域名。
        prefer_https: 是否优先 https。

    Returns:
        首页 URL 字符串。
    """
    if not domain:
        return ""
    return f"https://{domain}/" if prefer_https else f"http://{domain}/"


def _clean_encoding(name: str) -> str:
    """标准化编码名，修正常见别名。

    Args:
        name: 原始编码名。

    Returns:
        标准化后的编码名。
    """
    alias = {
        "gb2312": "gb18030",
        "gbk": "gb18030",
        "gb_2312-80": "gb18030",
        "iso-8859-1": "latin-1",
        "utf8": "utf-8",
        "utf-8-sig": "utf-8",
        "unicode": "utf-8",
    }
    key = str(name or "").strip().lower()
    return alias.get(key, key or "utf-8")


def _try_decode(raw: bytes, encoding: str) -> str | None:
    """尝试用指定编码严格解码。

    Args:
        raw: 原始字节。
        encoding: 编码名。

    Returns:
        解码成功返回文本，失败返回 None。
    """
    try:
        return raw.decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError):
        return None


def decode_html(raw: bytes, content_type: str | None = None) -> tuple[str, str]:
    """HTML 字节流解码（四级兜底，永不抛错）。

    顺序：
        1. HTTP ``Content-Type`` 中的 charset；
        2. HTML ``<meta charset>`` / ``<meta http-equiv>``；
        3. 严格尝试 UTF-8 与 ``gb18030``（覆盖 GBK/GB2312）；
        4. ``charset-normalizer`` 探测；最后 utf-8 + replace 保底。

    Args:
        raw: 原始 HTML 字节。
        content_type: 响应头 content-type，可为 None。

    Returns:
        ``(text, encoding)`` 元组；encoding 可能为 ``unknown``。
    """
    candidates: list[str] = []

    if content_type:
        found = _CT_CHARSET_RE.search(str(content_type))
        if found:
            candidates.append(_clean_encoding(found.group(1)))

    head = raw[:4096]
    for pattern in (_META_CHARSET_RE, _META_EQUIV_RE):
        found = pattern.search(head)
        if found:
            candidates.append(_clean_encoding(found.group(1).decode("ascii", "ignore")))

    detected = ""
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw)
        if best:
            detected = _clean_encoding(best.best().encoding or "")
    except Exception:  # noqa: BLE001 - 探测库异常不影响主流程
        detected = ""
    # 短中文 GBK 页面容易被 charset-normalizer 误判成可严格解码的冷门编码；
    # 先尝试 Web 最常见的 UTF-8 / GB18030，再使用统计探测结果。
    candidates.extend(["utf-8", "gb18030"])
    if detected:
        candidates.append(detected)
    for encoding in candidates:
        text = _try_decode(raw, encoding)
        if text is not None:
            return text, encoding
    return raw.decode("utf-8", errors="replace"), "unknown"


def clean_text(text: str | None, limit: int | None = None) -> str:
    """清洗文本：去控制字符、合并空白、去除首尾空格。

    Args:
        text: 原始文本。
        limit: 可选截断长度。

    Returns:
        清洗后的文本。
    """
    if not text:
        return ""
    value = _CONTROL_CHARS_RE.sub(" ", str(text))
    value = _WS_RE.sub(" ", value).strip()
    if limit and len(value) > limit:
        value = value[:limit].rstrip() + "…"
    return value


def sha1_short(text: str | bytes, length: int = 12) -> str:
    """计算 SHA1 摘要的前 N 位，用于 TDK 变更感知与去重。

    Args:
        text: 文本或字节。
        length: 摘要长度，默认 12。

    Returns:
        十六进制摘要字符串；空输入返回空串。
    """
    if text is None:
        return ""
    raw = text.encode("utf-8", errors="ignore") if isinstance(text, str) else text
    if not raw:
        return ""
    return hashlib.sha1(raw).hexdigest()[:length]


def mask_secret(secret: str | None, keep: int = 2) -> str:
    """对代理密码等敏感信息脱敏，避免写日志 / 落报告。

    Args:
        secret: 原始密钥。
        keep: 保留的头尾字符数。

    Returns:
        脱敏字符串，明文为空时返回空串。
    """
    if not secret:
        return ""
    text = str(secret)
    if len(text) <= keep * 2:
        return "***"
    return f"{text[:keep]}***{text[-keep:]}"


def mask_url(url: str | None) -> str:
    """对含 user:pass 的 URL 做脱敏，便于安全打印。

    Args:
        url: 原始 URL。

    Returns:
        脱敏后的 URL 字符串。
    """
    if not url:
        return ""
    text = str(url)
    try:
        parts = urlsplit(text)
    except ValueError:
        return "***"
    if not parts.netloc or "@" not in parts.netloc:
        return text
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return f"{parts.scheme}://***:***@{netloc}{parts.path or ''}"


def assure_absolute(url: str, base: str) -> str:
    """把相对 URL 补全为绝对 URL。

    Args:
        url: 可能是相对路径的 URL。
        base: 基地址。

    Returns:
        绝对 URL 字符串。
    """
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    base = base.rstrip("/")
    if url.startswith("/"):
        try:
            host = urlsplit(base).netloc
            return f"https://{host}{url}"
        except ValueError:
            return url
    return f"{base}/{url.lstrip('/')}"
