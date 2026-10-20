#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_to_md.py - Web Page to Markdown Converter (Python Version)

Usage:
    python scripts/source_to_md/web_to_md.py <url>
    python scripts/source_to_md/web_to_md.py <url1> <url2> ...
    python scripts/source_to_md/web_to_md.py -f urls.txt
    python scripts/source_to_md/web_to_md.py <url> -o output.md

Dependencies:
    pip install requests beautifulsoup4

TLS fingerprint handling:
    Some sites (e.g., WeChat mp.weixin.qq.com) block Python's default 'requests'
    library based on TLS fingerprints (JA3). If 'curl_cffi' is installed, this script
    uses it to impersonate a modern Chrome fingerprint and bypass such blocks. If
    'curl_cffi' is unavailable, it silently falls back to plain 'requests' — so
    non-blocking sites still work without the extra dependency.

    Public-only requests pin validated addresses: curl_cffi uses CURLOPT_RESOLVE
    while requests uses connection-local validation and numeric socket targets.
    Both retain the URL hostname for TLS verification. Environment proxies are
    disabled in this mode because a proxy could resolve the target again;
    --allow-private-hosts restores proxy use and unrestricted address resolution.

    Install for WeChat / Chinese-portal coverage:
        pip install curl_cffi

    If curl_cffi is unavailable on your platform, the Node.js counterpart
    (scripts/source_to_md/web_to_md.cjs) remains available as a fallback.
"""

from __future__ import annotations

import argparse
import codecs
import datetime
import io
import ipaddress
import json
import os
import re
import unicodedata
import socket
import subprocess
import sys
import time
from email.message import Message
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse
from urllib.request import Request

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from console_encoding import configure_utf8_stdio  # noqa: E402
from _dispatcher import (  # noqa: E402
    DOC_SUFFIXES,
    EXCEL_SUFFIXES,
    LEGACY_EXCEL_SUFFIXES,
    PDF_SUFFIXES,
    PRESENTATION_SUFFIXES,
    build_conversion_command,
)
from _conversion_profile import (  # noqa: E402
    profile_path_for,
    record_source_url,
    write_conversion_profile_best_effort,
)

configure_utf8_stdio()

# Help must not depend on the optional conversion packages: a stdlib-only
# interpreter still gets the argparse usage (docs/rules/code-style.md §4).
_HELP_REQUESTED = __name__ == "__main__" and any(
    arg in {"-h", "--help"} for arg in sys.argv[1:]
)
if not _HELP_REQUESTED:
    try:
        import requests
        from bs4 import BeautifulSoup, Comment, NavigableString, Tag
        from urllib3.util import parse_url
    except ImportError:
        print("Error: This script requires 'requests' and 'beautifulsoup4'.", file=sys.stderr)
        print("Please run: pip install requests beautifulsoup4", file=sys.stderr)
        sys.exit(1)

# Prefer curl_cffi for TLS-fingerprint impersonation (bypasses JA3 blocking on
# sites like WeChat). Fall back to plain requests when it's not installed.
try:
    from curl_cffi import CurlOpt  # type: ignore
    from curl_cffi import requests as curl_requests  # type: ignore
    _CURL_IMPERSONATE = "chrome120"
except ImportError:
    CurlOpt = None
    curl_requests = None
    _CURL_IMPERSONATE = None


class _UnsafeUrlError(ValueError):
    """Reject a URL that cannot be verified as a public HTTP(S) target."""


_NON_PUBLIC_IPV4_NETWORKS = tuple(ipaddress.ip_network(network) for network in (
    "0.0.0.0/8", "100.64.0.0/10", "240.0.0.0/4",
))


def _check_url_characters(url: str) -> None:
    """Reject characters clients could strip or reinterpret before parsing."""
    if "\\" in url or any(char.isspace() or unicodedata.category(char) == "Cc" for char in url):
        raise _UnsafeUrlError("Refusing URL containing backslash, whitespace, or control characters")


def _prepare_url(url: str) -> str:
    """Return the client's URL only when both parsers agree on its hostname."""
    _check_url_characters(url)
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise _UnsafeUrlError("Only HTTP(S) URLs with a hostname are allowed")
        prepared = requests.PreparedRequest()
        prepared.prepare_url(url, None)
        hostname = parse_url(prepared.url).host
        # The raw hostname must survive preparation unchanged, so a host the
        # client decodes differently (percent-encoded labels) is refused. An IDN
        # host legitimately becomes punycode; compare its IDNA form. urllib3
        # keeps IPv6 brackets; urlparse does not.
        raw_host = parsed.hostname
        if not raw_host.isascii():
            try:
                raw_host = raw_host.encode("idna").decode("ascii")
            except UnicodeError as exc:
                raise _UnsafeUrlError(f"Invalid internationalized hostname: {exc}") from exc
        if not hostname or raw_host.lower() != hostname.strip("[]").lower():
            raise _UnsafeUrlError("Refusing URL with inconsistent parsed hostnames")
        return prepared.url
    except (ValueError, requests.exceptions.RequestException) as exc:
        if isinstance(exc, _UnsafeUrlError):
            raise
        raise _UnsafeUrlError(f"Invalid URL: {exc}") from exc


def _resolve_public_addresses(hostname: str) -> list:
    """Resolve once and validate every address before any socket is created."""
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(info[4][0])
                for info in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            ]
        except (OSError, ValueError) as exc:
            raise _UnsafeUrlError(f"Cannot validate URL hostname {hostname}: {exc}") from exc
    if not addresses:
        raise _UnsafeUrlError(f"Cannot resolve URL hostname {hostname}")
    if CONFIG["allow_private_hosts"]:
        return addresses
    for address in addresses:
        scoped = getattr(address, "scope_id", None)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if (address.is_loopback or address.is_link_local
                or address.is_private or address.is_unspecified
                or address.is_multicast or address.is_reserved
                or scoped
                or any(address in network for network in _NON_PUBLIC_IPV4_NETWORKS)):
            raise _UnsafeUrlError(
                f"Refusing non-public URL target: {hostname} resolves to {address} "
                "(pass --allow-private-hosts for intranet or localhost pages)"
            )
    return list(dict.fromkeys(addresses))


def _validate_public_url(url: str) -> tuple[str, list]:
    """Return the prepared URL and its validated addresses for transport use."""
    prepared = _prepare_url(url)
    hostname = parse_url(prepared).host.strip("[]")
    return prepared, _resolve_public_addresses(hostname)


class _PublicConnectionMixin:
    """Keep urllib3's hostname/TLS handling but connect only to checked IPs."""

    def _new_conn(self):
        from urllib3.exceptions import ConnectTimeoutError, NewConnectionError
        from urllib3.util import Timeout

        addresses = _resolve_public_addresses(self._dns_host)
        error = None
        for address in addresses:
            sock = None
            try:
                family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
                sock = socket.socket(family, socket.SOCK_STREAM)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.timeout is not Timeout.DEFAULT_TIMEOUT:
                    sock.settimeout(self.timeout)
                if self.source_address:
                    sock.bind(self.source_address)
                # Numeric addresses go straight to connect; no second DNS lookup.
                sock.connect((str(address), self.port))
                sys.audit("http.client.connect", self, self.host, self.port)
                return sock
            except OSError as exc:
                error = exc
                if sock is not None:
                    sock.close()
        if isinstance(error, socket.timeout):
            raise ConnectTimeoutError(self, f"Connection to {self.host} timed out") from error
        raise NewConnectionError(self, f"Failed to establish a new connection: {error}") from error


def _public_http_adapter():
    """Build isolated pools without patching requests or urllib3 globally."""
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

    class PublicHTTPConnection(_PublicConnectionMixin, HTTPConnection):
        pass

    class PublicHTTPSConnection(_PublicConnectionMixin, HTTPSConnection):
        pass

    class PublicHTTPConnectionPool(HTTPConnectionPool):
        ConnectionCls = PublicHTTPConnection

    class PublicHTTPSConnectionPool(HTTPSConnectionPool):
        ConnectionCls = PublicHTTPSConnection

    adapter = requests.adapters.HTTPAdapter()
    adapter.poolmanager.pool_classes_by_scheme = {
        "http": PublicHTTPConnectionPool,
        "https": PublicHTTPSConnectionPool,
    }
    return adapter


def _validate_response_redirect(response, **kwargs) -> None:
    """Check requests redirects before its redirect engine sends the next hop."""
    try:
        response_url, _ = _validate_public_url(response.url)
        if response.is_redirect:
            # Match requests' decoding of the HTTP Location header.
            location = response.headers["location"].encode("latin1").decode("utf8")
            _check_url_characters(location)
            next_url, _ = _validate_public_url(urljoin(response_url, location))
            response.headers["location"] = next_url
    except _UnsafeUrlError:
        response.close()
        raise


def _curl_http_get(url: str, *, headers: dict | None, timeout: int | None,
                   verify: bool, stream: bool):
    """Pin each curl hop with CURLOPT_RESOLVE, retaining Chrome impersonation."""
    cookies = requests.cookies.RequestsCookieJar()
    headers = requests.structures.CaseInsensitiveDict(headers or {})
    max_redirects = requests.models.DEFAULT_REDIRECT_LIMIT
    for redirect_count in range(max_redirects + 1):
        url, addresses = _validate_public_url(url)
        curl_options = {}
        if not CONFIG["allow_private_hosts"]:
            target = parse_url(url)
            port = target.port if target.port is not None else (443 if target.scheme == "https" else 80)
            # IPv6 addresses need brackets in libcurl's host:port:address list.
            pinned = ",".join(f"[{ip}]" if ip.version == 6 else str(ip) for ip in addresses)
            curl_options = {CurlOpt.RESOLVE: [f"{target.host}:{port}:{pinned}"], CurlOpt.PROXY: ""}
        response = curl_requests.get(
            url, headers=headers, timeout=timeout, verify=verify,
            impersonate=_CURL_IMPERSONATE, stream=stream,
            allow_redirects=False, cookies=cookies, curl_options=curl_options, quote=False,
        )
        try:
            _validate_public_url(response.url)
        except _UnsafeUrlError:
            response.close()
            raise
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        try:
            _check_url_characters(location)
            next_url, _ = _validate_public_url(urljoin(response.url, location))
            if redirect_count >= max_redirects:
                raise requests.exceptions.TooManyRedirects(
                    f"Exceeded {max_redirects} redirects.", response=response,
                )
            cookie_headers = Message()
            for value in response.headers.get_list("Set-Cookie"):
                cookie_headers.add_header("Set-Cookie", value)
            cookies.extract_cookies(
                requests.cookies.MockResponse(cookie_headers), Request(response.url),
            )
            if requests.Session().should_strip_auth(response.url, next_url):
                headers = headers.copy()
                for name in ("Authorization", "Cookie", "Host"):
                    headers.pop(name, None)
            url = next_url
        finally:
            response.close()


def _http_get(url: str, *, headers: dict | None = None, timeout: int | None = None,
              verify: bool = True, stream: bool = False):
    """HTTP GET with curl_cffi preferred, requests fallback.

    Using curl_cffi lets this script fetch sites that reject Python's default
    TLS fingerprint (notably mp.weixin.qq.com). Signature mirrors the subset of
    requests.get() this script actually uses.
    """
    if curl_requests is not None:
        return _curl_http_get(
            url, headers=headers, timeout=timeout, verify=verify, stream=stream,
        )
    url, _ = _validate_public_url(url)
    with requests.Session() as session:
        if not CONFIG["allow_private_hosts"]:
            session.trust_env = False
            adapter = _public_http_adapter()
            session.mount("http://", adapter)
            session.mount("https://", adapter)
        response = session.get(
            url, headers=headers, timeout=timeout, verify=verify, stream=stream,
            hooks={"response": _validate_response_redirect},
        )
    try:
        _validate_public_url(response.url)
    except _UnsafeUrlError:
        response.close()
        raise
    return response


def _normalize_charset(charset: str | None) -> str:
    """Return a Python codec name when the declared charset is usable."""
    if not charset:
        return ""
    charset = charset.strip().strip('"').strip("'").lower()
    if not charset:
        return ""
    try:
        return codecs.lookup(charset).name
    except LookupError:
        return ""


def _charset_from_headers(headers: dict) -> str:
    content_type = headers.get("Content-Type") or headers.get("content-type") or ""
    match = re.search(r"charset\s*=\s*([^;\s]+)", content_type, re.I)
    return _normalize_charset(match.group(1)) if match else ""


def _charset_from_html(raw: bytes) -> str:
    """Extract a charset declaration from the first chunk of HTML bytes."""
    head = raw[:8192]
    patterns = [
        rb"<meta[^>]+charset=[\"']?\s*([a-zA-Z0-9_\-]+)",
        rb"<meta[^>]+content=[\"'][^\"']*charset=\s*([a-zA-Z0-9_\-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, head, re.I)
        if match:
            return _normalize_charset(match.group(1).decode("ascii", "ignore"))
    return ""


BODY_SHORTFALL_MIN_CHARS = 200
BODY_SHORTFALL_RATIO = 0.25

# ── 绝对下限（2026-10-08 新增）──
# 上面那个 shortfall 判据是**相对**的（抽取正文 ÷ 页面可见文字），专治「页面有字、但没抽出来」。
# 它**治不了 SPA**：马蜂窝 / 携程 / 知乎这类站的正文由 JS 异步加载，服务端返回的 HTML
# **本来就没有正文** ⇒ 页面可见文字也少 ⇒ 触发 shortfall 的豁免条件 ⇒ **静默通过**，
# 只留下一个 ~100 字的壳页。实测：马蜂窝 95 字、携程社区 102 字，rc=0 且无任何警告。
# ⇒ 补一条绝对判据：正文短到这个程度，基本不可能是真内容（SPA / 验证页 / 404 壳页）。
BODY_ABSOLUTE_MIN_CHARS = 200

# ── 正文占比下限（2026-10-10 新增）──
# 上面两条判据量的是「字符数」：绝对下限抓 SPA 壳页、shortfall 抓"没抽出来"。
# 它们**都抓不住**第三种形态 —— **栏目壳页**：页面可见文字很多（导航 + 播放器外壳 +
# 相关推荐），提取出的 Markdown 字符数也很多，但**真正文只有一小段**。
# 实测（2026-10-10，茶马古道轮的真实案例）：
#   · CCTV 纪录片页：提取 14625 字符，**零警告 rc=0**，但 88.8% 是链接文字，
#     长段只有 3 个、正文占比仅 6.4% ⇒ 模型只能靠"读一眼"自己发现（本轮发现了，但是靠自觉）。
# 🔬 阈值标定（2026-10-10，**17 个真实页面**：11 篇文章页 + 11 个网站首页）：
#   被拦下的首页正文占比：0%/0%/0%/0%/0%/1%/1%/8%（**最高 8%**）
#   放行的文章页正文占比：28%/59%/62%/78%/79%/92%（**最低 28%**）
#   ⇒ 取 **0.30** 落在 8% 与 28% 之间：两边各留 ~20 个点余量，**零误伤、零漏网**。
#   ⚠️ 迭代过程（两次修正，都值得记）：
#     ① 初版用 0.25，且没有"链接列表"识别 ⇒ 某文章页（正文 1574 字、占比 28%）
#        差点被误伤 ⇒ 补 `PROSE_CHARS_MAX` 绝对量兜底。
#     ② 但只靠占比+兜底，会把「整段是 `- [标题](url) 日期` 列表」的首页
#        算成正文段（交通运输部/央视网/农业农村部三个首页因此漏判）
#        ⇒ 补**"剥完链接后剩字 < 80 就不算正文段"**（见函数内 stripped 判据）。
#   🔑 **教训：判据要同时看"比例"和"绝对量"，还要先剔除"伪正文"（链接列表）。**
PROSE_MIN_BLOCK_CHARS = 120      # 多长算"像正文的一段"
PROSE_RATIO_MIN = 0.30           # 正文占比低于此值 ⇒ 疑似栏目壳页
PROSE_MIN_BLOCKS = 3             # 长段少于这么多，必打警告（连"3 段"都凑不齐）
PROSE_MIN_TOTAL_CHARS = 800      # 太短的页面交给 BODY_ABSOLUTE_MIN_CHARS，本判据不掺和

# 🔴 绝对量兜底（2026-10-10 补）—— 防误伤「外壳重的真文章」。
# 标定时发现：占比轴在「外壳重的真文章」与「首页」之间**会重叠**
#   · 某学信网文章页：总 5586 / 正文 1574 / 占比仅 **28%** ⇒ 差点被 0.30 误伤
#   · 而它的正文有 **1574 字 / 11 个长段** —— 明显是真文章，只是导航侧栏太重。
# ⇒ 判据改为**两个条件都满足才算壳页**：正文占比低 **且** 正文绝对量也少。
#   理由：首页的"正文"多是栏目导语（几百字），真文章动辄 1500+ 字。
#   ⚠️ 代价：少数"正文区里堆了少量摘要"的首页会被放过 —— 可接受，
#   因为本判据是**提示**不是硬拦截（C 段另有"像栏目页的走 --list-links"规则兜着）。
PROSE_CHARS_MAX = 800            # 正文绝对量超过此值 ⇒ 不判壳页（真文章特征）
# 🔴 2026-10-15：1000 → 800（与 check_research_volume.py **同源，改一边必须同步另一边**）。
#    实测两篇被误杀的真文章（d1ev 净正文 942 字 / 新华网 858 字）绝对量都在 800~1000 之间；
#    栏目页正文多在几百字，仍拦得住（WAF 页另有 WAF_CJK_RATIO_MIN 兜底）。

# ── 反爬 / 脚本页判据（2026-10-13 新增）──
# 上面三条判据**全部抓不住**第四种形态 —— WAF 挑战页。实测（阿里云 WAF，bjx.com.cn）：
#   HTTP **200**、正文 **3,711 字符**、**零 WARN**，而内容是一坨混淆 JS。
#   更糟的是它的"正文占比"高达 **99%**（JS 里没有 Markdown 链接）⇒ `_prose_ratio_warning`
#   直接放行；`prose_chars = 15,601 >= PROSE_CHARS_MAX` 又触发了"真文章"豁免。
#   ⇒ 模型收到的是**全绿信号**，只能靠"读一眼"发现乱码；那一轮它困在同一个站连抓 13 篇。
# 唯一稳定的分界是**中文占比**（实测差 200 倍）：
#   WAF 页 46 / 15,834 = **0.3%** ｜ 真文章 4,508 / 6,312 = **71.4%**
# ⇒ 取 **5%** 落在两者之间（与 PROSE_RATIO_MIN 同一标定法）。
WAF_CJK_RATIO_MIN = 0.05
# 命中的特征串 —— 只用于把文案说得更准，不是判据本身（判据是中文占比）。
WAF_MARKERS = ("aliyun_waf", "acw_sc__v2", "arg1='", "_waf_", "__webpack_modules__")


def _page_visible_text(soup) -> str:
    """Return the page's rendered text with scripts, styles, and noscript removed."""
    for node in soup(["script", "style", "noscript", "template"]):
        node.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ")).strip()


def _prose_ratio_warning(markdown_text: str) -> str | None:
    """Warn when the extracted body is mostly chrome, not prose (栏目壳页).

    Counts what is left after stripping links, images and bare URLs: a real
    article keeps long paragraphs, a channel/shell page keeps navigation.
    Returns ``None`` when the page is too short for the ratio to mean anything
    (``BODY_ABSOLUTE_MIN_CHARS`` already covers that case).
    """
    body = re.sub(r"<!--.*?-->", "", markdown_text, flags=re.S).strip()
    total = len(re.sub(r"\s+", "", body))
    if total < PROSE_MIN_TOTAL_CHARS:
        return None

    prose_chars = 0
    prose_blocks = 0
    for block in (b.strip() for b in re.split(r"\n\s*\n", body)):
        if not block:
            continue
        # 🔴 先判「整段是不是链接列表」—— 列表页的长段常是
        #    `- [标题](url) 日期 - [标题](url) 日期 …`，直接剥链接会把**标题文字**
        #    当正文留下，误判成"短文"（实测：交通运输部/央视网/农业农村部三个首页
        #    就是靠这个漏过第一版判据的）。判据 = 去掉所有链接后剩下的字符是否够多。
        stripped = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", block)      # 链接（含图片）
        stripped = re.sub(r"https?://\S+", "", stripped)           # 残留裸 URL
        stripped = re.sub(r"[\s\-*|>#·\[\]()【】]+", "", stripped)  # 标记符与嵌套括号
        if len(stripped) < 80:             # 剥完几乎不剩字 ⇒ 整段都是链接列表，不算正文
            continue
        # 剥掉链接目标 / 图片 / 裸 URL，只留可见文字
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", block)
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
        text = re.sub(r"https?://\S+", "", text)
        text = re.sub(r"^[>#|\-*\s]+", "", text).strip()
        chars = len(re.sub(r"\s+", "", text))
        if chars >= PROSE_MIN_BLOCK_CHARS:
            prose_chars += chars
            prose_blocks += 1

    ratio = prose_chars / total
    # ① 长段够多 且 占比够高 ⇒ 是真文章
    if prose_blocks >= PROSE_MIN_BLOCKS and ratio >= PROSE_RATIO_MIN:
        return None
    # ② 绝对量兜底：正文本身已经很多 ⇒ 是真文章（只是外壳重），别误伤
    #    （标定时某文章页 占比仅 28% 但有 1574 字正文 —— 靠这条救回来）
    if prose_chars >= PROSE_CHARS_MAX:
        return None
    return (
        f"body is {total} chars but only {prose_chars} of them ({ratio:.0%}) "
        f"read as prose in {prose_blocks} paragraph(s) - the rest is links / "
        f"navigation chrome. Looks like a CHANNEL/SHELL page, NOT an article. "
        f"Switch to another source; do not use this file as 素材."
    )


def _waf_page_warning(markdown_text: str) -> str | None:
    """Warn when the body is a crawler challenge, not an article (反爬页).

    A WAF interstitial returns HTTP 200 and a wall of obfuscated JavaScript.
    It defeats every character-count and prose-ratio check here - the text is
    long and has no Markdown links, so it even looks *more* prose-like than a
    real article. The tell that survives translation is **how much of it is
    Chinese**: a real article is mostly Chinese, a JS challenge is not.

    Returns ``None`` below ``BODY_ABSOLUTE_MIN_CHARS`` - there the ratio is
    noise and the absolute-minimum check already handles the page.
    """
    body = re.sub(r"<!--.*?-->", "", markdown_text, flags=re.S).strip()
    total = len(re.sub(r"\s+", "", body))
    if total < BODY_ABSOLUTE_MIN_CHARS:
        return None
    cjk = len(re.findall(r"[\u4e00-\u9fff]", body))
    ratio = cjk / total
    if ratio >= WAF_CJK_RATIO_MIN:
        return None
    hint = next((m for m in WAF_MARKERS if m in body), None)
    suffix = f" (matched marker: {m!r})" if (m := hint) else ""
    return (
        f"only {cjk} of {total} chars ({ratio:.1%}) are Chinese{suffix} - this is "
        f"a CRAWLER CHALLENGE / reverse-proxy page (WAF), NOT an article. "
        f"Do NOT parse it and do NOT retry this site - every article on the same "
        f"domain returns the same wall. Switch to a DIFFERENT domain."
    )


def _body_shortfall_warning(markdown_text: str, page_text: str) -> str | None:
    """Warn when the extracted body is a sliver of the text the page shows.

    A content container that the extractor did not recognise yields a short,
    plausible-looking Markdown file; comparing it against the page's visible
    text turns that silent loss into a warning the caller can act on.
    """
    body_chars = len(re.sub(r"\s+", "", markdown_text))
    page_chars = len(re.sub(r"\s+", "", page_text))
    if body_chars >= BODY_SHORTFALL_MIN_CHARS or page_chars < BODY_SHORTFALL_MIN_CHARS * 2:
        return None
    if body_chars > page_chars * BODY_SHORTFALL_RATIO:
        return None
    return (
        f"body extraction kept {body_chars} characters while the page shows "
        f"{page_chars}; the content container was not recognised, so verify "
        "the Markdown against the page before using it as a source"
    )


def _decode_quality_score(text: str) -> int:
    """Score obvious decode artifacts; lower is better."""
    mojibake_markers = [
        "�", "锟", "Ã", "Â", "â€", "â€™", "â€œ", "â€\x9d",
        "琚", "佸", "鍦", "涓", "鏄", "寤", "骞", "鏈", "鏃", "鈥",
    ]
    marker_hits = sum(text.count(marker) for marker in mojibake_markers)
    control_hits = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r")
    return marker_hits * 20 + control_hits * 10 + text.count("\ufffd") * 50


def _decode_response_text(response) -> str:
    """Decode HTTP response bytes without letting guessed encodings override declarations."""
    raw = response.content
    declared = [
        _charset_from_headers(response.headers),
        _charset_from_html(raw),
    ]
    if raw.startswith(codecs.BOM_UTF8):
        declared.insert(0, "utf-8-sig")

    seen = set()
    declared = [enc for enc in declared if enc and not (enc in seen or seen.add(enc))]
    for enc in declared:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue

    candidates = []
    for enc in [
        getattr(response, "encoding", None),
        getattr(response, "apparent_encoding", None),
        "utf-8",
        "gb18030",
        "big5",
    ]:
        enc = _normalize_charset(enc)
        if enc and enc not in candidates:
            candidates.append(enc)

    decoded = []
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        decoded.append((_decode_quality_score(text), enc, text))

    if decoded:
        decoded.sort(key=lambda item: item[0])
        return decoded[0][2]

    # Every strict decode failed: the page carries a few bad bytes. Keep the
    # declared charset in the running instead of dropping to UTF-8, and let
    # the artifact score pick the lossy decode that damages the least text.
    lossy = []
    for enc in declared + [enc for enc in candidates if enc not in declared]:
        try:
            text = raw.decode(enc, errors="replace")
        except LookupError:
            continue
        lossy.append((_decode_quality_score(text), enc, text))
    if lossy:
        lossy.sort(key=lambda item: item[0])
        return lossy[0][2]
    return raw.decode("utf-8", errors="replace")

try:
    from PIL import Image, ImageOps
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False
    if not _HELP_REQUESTED:
        print("[WARN] Pillow not installed. WebP images will not be converted to PNG.", file=sys.stderr)
        print("       Run: pip install Pillow", file=sys.stderr)

# ============ Config ============
CONFIG = {
    "output_dir": "./projects",
    "timeout": 30,
    "insecure": False,
    "allow_private_hosts": False,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Specific content identifiers often found in Chinese CMS (Gov/News)
    "content_selectors": [
        {"class_": re.compile(r"tys-main-zt-show", re.I)},
        {"class_": re.compile(r"tys-main", re.I)},
        {"class_": "TRS_Editor"},
        {"class_": "TRS_UEDITOR"},
        {"class_": "ucontent"},
        {"class_": "article-content"},
        {"class_": "news-content"},
        {"class_": "detail-content"},
        {"class_": "content-text"},
        {"class_": "pages_content"},
        {"class_": "zwgk_content"},
        {"class_": "content_detail"},
        {"class_": "text_content"},
        {"class_": "main-content"},
        {"class_": "main_content"},
        {"class_": "view-content"},
        {"class_": "info-content"},
        {"id": "Zoom"},
        {"id": "content"},
        {"id": "article"},
        {"class_": "content"},
        {"name": "article"},  # tag name
        {"name": "main"},    # tag name
    ]
}


def fetch_response(url: str):
    """Fetch a URL with explicit headers and return the raw HTTP response."""
    headers = {
        "User-Agent": CONFIG["user_agent"],
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"
    }

    try:
        response = _http_get(url, headers=headers,
                             timeout=CONFIG["timeout"], verify=not CONFIG["insecure"])
        response.raise_for_status()
        return response
    except Exception as e:
        raise Exception(f"Failed to fetch {url}: {str(e)}")


def fetch_url(url: str) -> tuple[str, str]:
    """Fetch a web page as decoded text plus the final URL after redirects."""
    response = fetch_response(url)
    return _decode_response_text(response), response.url


def clean_title(title: str) -> str:
    """Remove common site suffixes from a title."""
    if not title:
        return ""
    # Remove site name suffixes often found in Chinese titles
    clean = re.sub(r"[-_|].*?(政府|门户|网站|委员会).*$", "", title)
    return clean.strip()


_FILENAME_TRANSLITERATIONS = str.maketrans({
    'đ': 'd', 'Đ': 'D', 'ø': 'o', 'Ø': 'O', 'ł': 'l', 'Ł': 'L',
    'ß': 'ss', 'æ': 'ae', 'Æ': 'AE', 'œ': 'oe', 'Œ': 'OE', 'ı': 'i',
})


def sanitize_filename(name: str) -> str:
    """Sanitize a string for filesystem-safe filenames.

    Accented Latin letters fold to their base letter (``Khát vọng`` ->
    ``Khat_vong``) instead of vanishing; letters of any script and digits
    stay, everything else is dropped.
    """
    folded = unicodedata.normalize('NFKD', name.translate(_FILENAME_TRANSLITERATIONS))
    folded = ''.join(ch for ch in folded if not unicodedata.combining(ch))
    # Replace whitespace with underscore first
    clean = re.sub(r'\s+', '_', folded)
    # Keep letters and digits of any script, underscore, hyphen
    clean = ''.join(ch for ch in clean if ch.isalnum() or ch in '_-')
    # Collapse repeating underscores
    clean = re.sub(r'_+', '_', clean)
    return clean[:80]  # Truncate


def derive_base_name(title: str, url: str) -> str:
    """Derive a safe, non-empty basename from a title or URL."""
    base = sanitize_filename(title or "")
    if base:
        return base

    parsed = urlparse(url)
    path = parsed.path.strip('/')
    if path:
        candidate = f"{parsed.netloc}_{path}"
    else:
        candidate = parsed.netloc or "untitled"
    base = sanitize_filename(candidate)
    if base:
        return base

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"untitled_{ts}"


def build_image_filename(abs_url: str, seq: int, content_type: str | None = None) -> str:
    """Build a safe image filename from URL metadata."""
    parsed = urlparse(abs_url)
    basename = os.path.basename(parsed.path).split('?')[0]
    stem, ext = os.path.splitext(basename)
    if not ext or len(ext) > 5 or '/' in ext:
        ext = ""
    if not ext and content_type:
        ctype = content_type.split(';')[0].lower()
        ext_map = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        ext = ext_map.get(ctype, "")
    if not ext:
        ext = ".jpg"
    stem = sanitize_filename(stem) if stem else f"image_{seq}"
    return f"{stem}{ext}"


def resolve_content_image_url(img: Tag, page_url: str) -> str | None:
    """Resolve one content image, preferring real lazy-load URLs."""
    candidates = [
        img.get("data-src"),
        img.get("data-original"),
        img.get("data-lazy-src"),
        img.get("data-actualsrc"),
        img.get("src"),
    ]
    for value in candidates:
        if not isinstance(value, str):
            continue
        src = value.strip()
        if not src or src.startswith(("data:", "javascript:", "blob:", "#")):
            continue
        _check_url_characters(src)
        resolved = urljoin(page_url, src)
        parsed = urlparse(resolved)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            img["src"] = resolved
            return resolved
    return None


def rewrite_images_to_remote_urls(content_element: Tag | None, page_url: str) -> int:
    """Retain remote image links without downloading image bytes."""
    if content_element is None:
        return 0
    return sum(
        resolve_content_image_url(img, page_url) is not None
        for img in content_element.find_all("img")
    )


def download_and_rewrite_images(
    content_element: Tag | None,
    page_url: str,
    image_dir: str,
    rel_prefix: str,
) -> int:
    """Download images under the main content node and rewrite `src` paths."""
    if content_element is None:
        return 0
    images = list(content_element.find_all("img"))
    if not images:
        return 0

    os.makedirs(image_dir, exist_ok=True)
    downloaded = {}
    manifest_by_filename: dict[str, dict[str, object]] = {}
    saved = 0

    for idx, img in enumerate(images):
        abs_url = resolve_content_image_url(img, page_url)
        if abs_url is None:
            continue
        content_type = ""
        converted_from = ""
        if abs_url in downloaded:
            saved_name = downloaded[abs_url]
        else:
            try:
                resp = _http_get(
                    abs_url,
                    headers={"User-Agent": CONFIG["user_agent"]},
                    timeout=CONFIG["timeout"],
                    verify=not CONFIG["insecure"],
                )
                resp.raise_for_status()
                filename = build_image_filename(
                    abs_url, idx, resp.headers.get("Content-Type"))

                # Check if image is webp and convert to png
                stem, ext = os.path.splitext(filename)
                content_type = resp.headers.get("Content-Type", "").lower()
                is_webp = ext.lower() == ".webp" or "webp" in content_type

                if is_webp and PILLOW_AVAILABLE:
                    # Convert webp to png (optimized)
                    try:
                        img_data = io.BytesIO(resp.content)
                        with Image.open(img_data) as source:
                            pil_image = ImageOps.exif_transpose(source)

                        # Update filename to .png
                        converted_from = filename
                        filename = f"{stem}.png"
                        local_path = os.path.join(image_dir, filename)

                        # Avoid accidental overwrites if filenames collide
                        counter = 1
                        while os.path.exists(local_path):
                            local_path = os.path.join(
                                image_dir, f"{stem}_{counter}.png")
                            filename = os.path.basename(local_path)
                            counter += 1

                        # Save as PNG directly (Pillow auto-converts, no need for explicit mode conversion)
                        pil_image.save(local_path, 'PNG', optimize=False)
                        pil_image.close()
                        print(f"   [INFO] Converted webp to png: {filename}")
                    except Exception as convert_err:
                        print(
                            f"   [WARN] Failed to convert webp: {convert_err}, saving as-is")
                        local_path = os.path.join(image_dir, filename)
                        counter = 1
                        stem, ext = os.path.splitext(filename)
                        while os.path.exists(local_path):
                            local_path = os.path.join(
                                image_dir, f"{stem}_{counter}{ext}")
                            filename = os.path.basename(local_path)
                            counter += 1
                        with open(local_path, "wb") as f:
                            f.write(resp.content)
                else:
                    local_path = os.path.join(image_dir, filename)

                    # Avoid accidental overwrites if filenames collide
                    counter = 1
                    stem, ext = os.path.splitext(filename)
                    while os.path.exists(local_path):
                        local_path = os.path.join(
                            image_dir, f"{stem}_{counter}{ext}")
                        filename = os.path.basename(local_path)
                        counter += 1

                    with open(local_path, "wb") as f:
                        f.write(resp.content)
                downloaded[abs_url] = filename
                saved_name = filename
                manifest_by_filename[saved_name] = {
                    "index": len(manifest_by_filename) + 1,
                    "filename": saved_name,
                    "original_filename": converted_from or saved_name,
                    "asset_kind": "bitmap",
                    "svg_renderable": True,
                    "pptx_native_supported": True,
                    "source_kind": "web_image",
                    "source_url": abs_url,
                    "source_page_url": page_url,
                    "content_type": content_type.split(";")[0] if content_type else "",
                    "occurrences": [],
                }
                saved += 1
            except _UnsafeUrlError:
                raise
            except Exception as e:
                print(f"   [WARN] Skip image {abs_url}: {e}")
                continue

        rel_path = os.path.join(
            rel_prefix, saved_name) if rel_prefix else saved_name
        img["src"] = rel_path
        manifest_item = manifest_by_filename.get(saved_name)
        if manifest_item is not None:
            occurrences = manifest_item.setdefault("occurrences", [])
            if isinstance(occurrences, list):
                occurrences.append({
                    "occurrence_index": idx + 1,
                    "source_url": abs_url,
                    "alt_text": img.get("alt", ""),
                })
                manifest_item["usage_count"] = len(occurrences)

    if manifest_by_filename:
        manifest_path = os.path.join(image_dir, "image_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(
                list(manifest_by_filename.values()),
                f,
                ensure_ascii=False,
                indent=2,
            )
            f.write("\n")

    return saved


def extract_metadata(soup: BeautifulSoup, url: str) -> dict[str, str]:
    """Extract page metadata such as title, date, description, and author."""

    # 1. Title
    title_tag = soup.title
    title = clean_title(title_tag.string if title_tag else "")

    # 2. Meta tags
    metas = {}
    for meta in soup.find_all("meta"):
        name = meta.get("name") or meta.get("property")
        content = meta.get("content")
        if name and content:
            metas[name.lower()] = content.strip()

    # 3. Date Extraction Strategies
    date = (
        metas.get("article:published_time") or
        metas.get("og:published_time") or
        metas.get("pubdate") or
        metas.get("publishdate") or
        metas.get("date")
    )

    if not date:
        # Try matching date patterns in the text
        text_content = soup.get_text()
        date_patterns = [
            r"发布[时日]间[：:]\s*(\d{4}[-\/年]\d{1,2}[-\/月]\d{1,2}[日]?)",
            r"日期[：:]\s*(\d{4}[-\/年]\d{1,2}[-\/月]\d{1,2}[日]?)",
            r"(\d{4}[-\/年]\d{1,2}[-\/月]\d{1,2}[日]?)\s*(?:发布|来源)",
            r"时间[：:]\s*(\d{4}[-\/]\d{1,2}[-\/]\d{1,2})"
        ]
        for pattern in date_patterns:
            match = re.search(pattern, text_content)
            if match:
                date = match.group(1).replace(
                    "年", "-").replace("月", "-").replace("日", "")
                break

    if not date:
        # Try URL matching
        # Only a plausible year and month: a handle or record number such as
        # ".../10665/379812/..." is not May 3798.
        match = re.search(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])[\/_](?:t\d+_)?", url)
        if match:
            date = f"{match.group(1)}-{match.group(2)}"
        else:
            match = re.search(r"(?<!\d)((?:19|20)\d{2})[-\/](0[1-9]|1[0-2])[-\/](\d{2})", url)
            if match:
                date = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"

    # 4. Description
    description = (
        metas.get("description") or
        metas.get("og:description") or
        metas.get("twitter:description") or
        ""
    )

    # 5. Author/Source
    author = metas.get("author") or metas.get("article:author")
    if not author:
        # Try common patterns
        source_patterns = [
            r"来源[：:]\s*([^\s<]+)",
            r"发布(?:单位|机构)[：:]\s*([^\s<]+)"
        ]
        for pattern in source_patterns:
            match = re.search(pattern, soup.get_text())
            if match:
                author = match.group(1)
                break

    return {
        "title": title or metas.get("og:title") or "Untitled",
        "date": date or "",
        "description": description,
        "author": author or "",
        "source_url": url
    }


def find_main_content(soup: BeautifulSoup) -> Tag | None:
    """Find the most likely main content container in a page."""
    # 1. Clean up first (remove known clutter)
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "noscript", "iframe"]):
        tag.decompose()

    best_element = None
    max_score = 0

    # 2. Strategy A: Check specific classes/ids
    for selector in CONFIG["content_selectors"]:
        if "name" in selector:
            # Tag name match (article, main)
            elements = soup.find_all(selector["name"])
        else:
            # Class or ID match
            elements = soup.find_all(attrs=selector)

        for el in elements:
            # Score based on text length and chinese character count
            text = el.get_text(strip=True)
            length = len(text)
            if length < 100:
                continue

            chinese_count = len(re.findall(r'[\u4e00-\u9fa5]', text))
            score = length + (chinese_count * 2)

            if score > max_score:
                max_score = score
                best_element = el

    # 3. Strategy B: If no specific container found, look for dense text areas with paragraphs
    if not best_element or max_score < 200:
        for div in soup.find_all("div"):
            p_count = len(div.find_all("p", recursive=False))
            # recursive=False ensures we don't just pick the top-level body by accident
            # but sometimes content is nested deep
            if p_count == 0:
                # Check if it has lots of text even without p tags (br tags?)
                pass

            text = div.get_text(strip=True)
            if len(text) > 200 and p_count >= 1:
                # Recalculate deep score
                chinese_count = len(re.findall(r'[\u4e00-\u9fa5]', text))
                score = len(text) + (chinese_count * 2) + (p_count * 50)
                if score > max_score:
                    max_score = score
                    best_element = div

    # Fallback to body, then to the whole document: some pages ship without a
    # <body> element at all (news.cn article pages do), and 'html.parser' leaves
    # ``soup.body`` as None there, which used to crash the Markdown traversal
    # downstream with "'NoneType' object has no attribute 'name'".
    if best_element is not None:
        return best_element
    if soup.body is not None:
        return soup.body
    return soup


# ── Listing-page link extraction (--list-links) ──
# Why: with no search API available for web pages, a converter that only accepts
# article URLs forces the caller to guess them. Walking a channel page is the
# reliable move, so this exposes "listing page in, candidate article URLs out".

# Rule of thumb for every list below: only cross-site, high-confidence patterns
# may EXCLUDE a link. Anything that is merely "usually true" may only DEMOTE it
# (sort last). A wrong exclusion silently drops a real article; a wrong demotion
# just reorders the list. Site-specific knowledge goes in as a ranking hint,
# never as a filter - the next site will not match it.

_SITE_SUFFIXES = {"com.cn", "gov.cn", "org.cn", "net.cn", "edu.cn", "ac.cn"}
_ASSET_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".bmp",
    ".css", ".js", ".json", ".xml", ".rss",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".gz", ".mp3", ".mp4", ".avi", ".apk", ".exe",
)
# Cross-site exclusions: utility pages and section names that are not an article
# on any CMS.
_NON_ARTICLE_PATH_MARKERS = (
    "/login", "/register", "/signin", "/signup", "/rss", "/sitemap",
    "/search", "/about", "/contact", "/privacy", "/terms", "/help",
    "/list_", "/list/", "/channel", "/column", "/category", "/tag/",
    "/topic/", "/special", "/subject/", "/index",
    "/footer", "/nav",
)
# Demoted only, never excluded: usually site chrome, but real articles do live
# under it - e.g. some portals publish at /home/YYYY-MM/DD/content_*.htm.
_LOW_PRIORITY_PATH_MARKERS = ("/home/",)
# Footer / boilerplate link labels, matched against anchor text: filters site
# chrome by meaning, so it still works on a site we have never seen.
_NON_ARTICLE_TITLE_MARKERS = (
    "关于我们", "关于本网", "关于本站", "关于网站", "联系我们", "联系方式",
    "网站声明", "版权声明", "免责声明", "隐私政策", "网站地图", "意见反馈",
    "用户协议", "友情链接", "广告服务", "招聘信息", "部门网站",
    # Account / login entry points (SSO links appear in many footers).
    "立即注册", "免费注册", "用户注册", "忘记密码", "找回密码",
)
# Shortest anchor label that plausibly reads as a headline. Chinese headlines
# normally run well past this; a low bar here would let nav labels like
# "国务院客户端小程序" (9) pass as articles and suppress the listing warning.
_MIN_HEADLINE_CHARS = 12
# Generic article-shaped URLs: any site with dated permalinks matches these.
_GENERIC_ARTICLE_RE = re.compile(
    r"(/\d{6,8}/|/\d{4}-\d{1,2}/|/article/|/detail/|/news/|"
    r"/content/|/post/|/\d{5,}\.html)",
    re.IGNORECASE,
)
# Shapes seen on specific sites (news.cn `c_*.htm`; thepaper.cn
# `newsDetail_forward_*`). Ranking bonus only - never a filter, and never the
# trigger for the "no articles here" warning, or an unseen site would misfire.
_SITE_ARTICLE_HINTS = re.compile(
    r"(c_\d+|/c\.html|newsdetail|forward_\d+)", re.IGNORECASE,
)


def site_root(url: str) -> str:
    """Return the root used to keep a listing page's own links.

    ``www.news.cn`` and ``city.news.cn`` share ``news.cn``. Two-part public
    suffixes keep one more label, so ``www.mct.gov.cn`` -> ``mct.gov.cn`` rather
    than collapsing every government site into ``gov.cn``.
    """
    host = urlparse(url).netloc.lower().split(":")[0]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    if ".".join(parts[-2:]) in _SITE_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _is_listing_asset(url: str) -> bool:
    """Return whether a link points at an asset or a non-article utility page."""
    path = urlparse(url).path.lower()
    if path.endswith(_ASSET_SUFFIXES):
        return True
    return any(marker in path for marker in _NON_ARTICLE_PATH_MARKERS)


def _is_boilerplate_title(title: str) -> bool:
    """Return whether an anchor label is site chrome (about / contact / legal)."""
    if not title:
        return False
    return any(marker in title for marker in _NON_ARTICLE_TITLE_MARKERS)


def _looks_like_article_list(links: list[tuple[str, str]]) -> bool:
    """Return whether a listing's links look like articles rather than chrome.

    Deliberately generous: a false "yes" only skips a warning, while a false
    "no" would nag the caller about a page that is actually fine. Generic URL
    shapes carry the judgment, with headline length as a cross-site fallback,
    so a site we have never seen still passes.
    """
    for title, link in links:
        if any(marker in urlparse(link).path.lower()
               for marker in _LOW_PRIORITY_PATH_MARKERS):
            continue  # a demoted link is not evidence of an article list
        if _GENERIC_ARTICLE_RE.search(link) or _SITE_ARTICLE_HINTS.search(link):
            return True
        if len(title) >= _MIN_HEADLINE_CHARS:
            return True
    return False


def extract_article_links(
    soup: BeautifulSoup | Tag, base_url: str, limit: int = 40,
) -> list[tuple[str, str]]:
    """Return ``(title, url)`` candidates linked from a listing page.

    Same-site links only, de-duplicated, with article-shaped paths sorted first
    so a caller can take the top N instead of re-reading the whole page.
    """
    root = site_root(base_url)
    titles: dict[str, str] = {}
    order: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = (anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        url = parsed._replace(fragment="").geturl()
        if site_root(url) != root or _is_listing_asset(url):
            continue
        if url.rstrip("/") == base_url.rstrip("/"):
            continue
        if urlparse(url).path in ("", "/"):
            continue  # a bare host is a homepage link, never an article
        if url not in titles:
            titles[url] = ""
            order.append(url)
        # A listing often links the same article twice (thumbnail + headline);
        # keeping the longest label stops an image-only anchor blanking the title.
        label = anchor.get_text(" ", strip=True)
        if not label:
            label = anchor.get("title") or ""
        if not label:
            image = anchor.find("img")
            if image is not None:
                label = image.get("alt") or ""
        label = re.sub(r"\s+", " ", label)
        if len(label) > len(titles[url]):
            titles[url] = label
    links = [
        (titles[url], url) for url in order
        if not _is_boilerplate_title(titles[url])
    ]
    def rank(item: tuple[str, str]) -> int:
        link = item[1]
        # Checked first: a demoted path must not be re-promoted by a URL shape it
        # happens to share with articles (e.g. /home/2023-03/29/content_*.htm).
        if any(marker in urlparse(link).path.lower()
               for marker in _LOW_PRIORITY_PATH_MARKERS):
            return 3
        if _GENERIC_ARTICLE_RE.search(link):
            return 0
        if _SITE_ARTICLE_HINTS.search(link):
            return 1
        return 2

    links.sort(key=rank)
    return links[:limit]


# ── Keyword search (--search) ──
# Why: a sandbox has no search tool, and scraping a search engine's HTML results
# page does not work (Baidu blocks it; Bing's HTML is low quality; 360/Sogou
# return their own sites). Bing still serves its web results as an RSS feed when
# asked, and that is a structured endpoint - no HTML scraping, no JS, no
# per-engine parsing. So keyword search works from the sandbox after all.
BING_SEARCH_ENDPOINT = "https://cn.bing.com/search"


# Search-hit source tiers. Same rule as everywhere else: only DEMOTE, never
# exclude - a wrong exclusion silently loses a real source. They are not shared
# with the image pipeline because the two have different failure modes (there,
# low-value hosts; here, hosts that block fetching outright).
_SEARCH_AUTHORITY_SUFFIXES = (".gov.cn", ".edu.cn", ".org.cn", ".ac.cn")
_SEARCH_MEDIA_HOSTS = (
    "people.com.cn", "xinhuanet.com", "news.cn", "chinanews.com.cn",
    "thepaper.cn", "cctv.com", "chinadaily.com.cn", "cnr.cn", "stdaily.com",
    "ce.cn", "china.com.cn", "gmw.cn", "youth.cn", "comac.cc",
)
# 「查询词被切碎」的指纹 —— 词典 / 字词典 / 小游戏站。这批站**只在查询词被
# 分词切碎时才成堆出现**（实测 `综合管廊 分类` ⇒ 10 条里 9 条是词典页，全是
# 「综合」一个词条；而单给 `综合管廊` 时一条都没有）。
# ⇒ 既让它们沉底，也用它触发一条**可执行告警**（见 _run_search）。
_SEARCH_SHRED_MARKERS = (
    "hanyuguoxue", "hgcha", "hancibao", "chagushici", "kmcha",
    "zdic", "zidian", "cihai", "iciba", "youdao", "dictionary", "dict.",
    "omgchinese", "moe.edu.tw", "4399", "7k7k", "xiaoyouxi",
)

# 通用词 —— 不含专有信息的修饰语（主题副标题里几乎全是这类）。它们一旦被
# 单独切出来就会主导整个结果（`综合管廊 分类` ⇒ 全是「综合」的词典页）。
#
# 🔴 这是 references/step_0_extract.md §0f「取词铁律」禁词表的**运行期副本**
#    （脚本不能依赖 md，只能各存一份）⇒ **改任意一边必须同步改另一边**。
#    用途：查询词被切碎时，剥掉它们给出一个更干净的候选（见 _suggest_query）。
_QUERY_COMMON_WORDS = (
    "分类", "系统", "构成", "施工", "工法", "应用", "技术", "发展",
    "建设", "管理", "概述", "意义", "现状", "措施", "要点",
)

# Sink order: encyclopaedia/user-content hosts that either block fetching
# (baike.baidu.com returns 403) or are low-value as a PPT source.
_SEARCH_LOW_HOSTS = (
    "baike.baidu.com", "zhidao.baidu.com", "wenku.baidu.com", "jingyan.baidu.com",
    "baijiahao", "zhihu.com", "sohu.com", "toutiao.com", "csdn.net",
    "docin.com", "doc88.com", "book118.com", "360doc.com", "renrendoc.com",
    "mianfeiwendang",
    "wenku.so.com",        # 360 文库：JS 壳页，实测 0 字符（2026-10-09）
    "customs.gov.cn",      # 🔴 SSL 证书主机名不匹配，抓取必然失败（2026-10-10 实测）
) + _SEARCH_SHRED_MARKERS


def _shredded_hits(results: list[tuple[str, str]]) -> int:
    """Count hits that look like dictionary/game pages (query got shredded)."""
    return sum(
        1 for _, link in results
        if any(marker in urlparse(link).netloc.lower() for marker in _SEARCH_SHRED_MARKERS)
    )


def _suggest_query(keyword: str) -> str | None:
    """Guess a cleaner query after the original came back shredded.

    Two shapes, both seen in the field:

    - multi-word  (``综合管廊 分类``)   -> keep the longest non-common word;
    - single glued (``管廊施工``)       -> strip trailing common words.

    Returns ``None`` when nothing can be guessed - never invent a query.
    """
    tokens = [tok for tok in keyword.split() if tok]
    if len(tokens) >= 2:
        proper = [tok for tok in tokens if tok not in _QUERY_COMMON_WORDS]
        pool = proper or tokens
        # 最长者通常就是核心名词（并列时取最靠前的那个）
        return max(pool, key=lambda tok: (len(tok), -tokens.index(tok)))
    if not tokens:
        return None
    stem = tokens[0]
    while True:  # 剥掉可能叠了好几层的中文后缀（管廊施工工法 -> 管廊施工 -> 管廊）
        for word in sorted(_QUERY_COMMON_WORDS, key=len, reverse=True):
            if len(stem) > len(word) and stem.endswith(word):
                stem = stem[: -len(word)]
                break
        else:
            break
    return stem if stem != tokens[0] else None


def _search_tier(url: str) -> int:
    """Rank a search hit: 0 authority/media, 1 other, 2 low-value sink."""
    host = urlparse(url).netloc.lower()
    if any(marker in host for marker in _SEARCH_LOW_HOSTS):
        return 2
    if host.endswith(_SEARCH_AUTHORITY_SUFFIXES):
        return 0
    if any(marker in host for marker in _SEARCH_MEDIA_HOSTS):
        return 0
    return 1


def search_web(keyword: str, limit: int = 10) -> list[tuple[str, str]]:
    """Return ``(title, url)`` results for a keyword, via Bing's RSS output.

    Hits come back in Bing's relevance order, which routinely puts
    encyclopaedia entries first - and those cannot even be fetched. Sort by
    source tier instead, keeping Bing's order inside a tier (stable sort).
    """
    url = f"{BING_SEARCH_ENDPOINT}?q={quote_plus(keyword)}&format=rss"
    response = fetch_response(url)
    feed = BeautifulSoup(_decode_response_text(response), "xml")

    results: list[tuple[str, str]] = []
    for item in feed.find_all("item"):
        link_el = item.find("link")
        link = (link_el.text if link_el else "").strip()
        if not link.startswith("http"):
            continue
        title_el = item.find("title")
        title = re.sub(r"\s+", " ", title_el.text if title_el else "").strip()
        results.append((title, link))

    results.sort(key=lambda item: _search_tier(item[1]))
    return results[:limit]


def element_to_markdown(element: Tag | NavigableString | None) -> str:
    """Recursively convert a BeautifulSoup node to Markdown."""
    if element is None:
        return ""

    if isinstance(element, NavigableString):
        text = str(element).strip()
        return text if text else ""

    tag_name = element.name.lower()

    # Skip hidden/unwanted tags
    if tag_name in ['script', 'style', 'meta', 'link', 'input', 'button', 'select']:
        return ""

    content = ""
    for child in element.children:
        content += element_to_markdown(child)
        # Add spacing logic here if needed, but usually block elements handle it

    # Block handlers
    if tag_name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
        level = int(tag_name[1])
        return f"\n{'#' * level} {content}\n\n"

    elif tag_name == 'p':
        # Clean up internal whitespace
        content = re.sub(r'\s+', ' ', content).strip()
        return f"\n{content}\n\n" if content else ""

    elif tag_name == 'br':
        return "  \n"

    elif tag_name == 'hr':
        return "\n---\n"

    elif tag_name == 'div':
        return f"\n{content}\n"

    elif tag_name == 'blockquote':
        lines = content.strip().split('\n')
        quoted = '\n'.join([f"> {line}" for line in lines if line.strip()])
        return f"\n{quoted}\n\n"

    elif tag_name in ['ul', 'ol']:
        # This is tricky without "state" (knowing we are in a list)
        # For simplicity in this recursive version, we rely on LI handling
        return f"\n{content}\n"

    elif tag_name == 'li':
        # Simple list handling
        clean_content = content.strip()
        return f"- {clean_content}\n"

    elif tag_name == 'pre':
        return f"\n```\n{content}\n```\n\n"

    elif tag_name == 'code':
        # If parent is pre, handle in pre. If inline:
        parent = element.parent
        if parent and parent.name == 'pre':
            return content
        return f"`{content}`"

    elif tag_name == 'a':
        href = element.get('href', '')
        if href and not href.startswith('javascript:'):
            return f"[{content}]({href})"
        return content

    elif tag_name == 'img':
        src = element.get('src', '')
        alt = element.get('alt', '')
        if src:
            return f"![{alt}]({src})"
        return ""

    elif tag_name == 'table':
        # Basic table text extraction, full markdown table support is complex
        # Leaving as raw text or simplistic conversion for now
        # Ideally, we'd parse TRs and TDs
        return f"\n{content}\n"

    elif tag_name == 'tr':
        return f"{content}|\n"

    elif tag_name in ['td', 'th']:
        return f"| {content.strip()} "

    # Style formatting
    elif tag_name in ['strong', 'b']:
        return f"**{content}**"
    elif tag_name in ['em', 'i']:
        return f"*{content}*"
    elif tag_name in ['del', 's', 'strike']:
        return f"~~{content}~~"

    # Default for span, section, etc.
    return f"{content} "


def simple_html_to_markdown_traversal(
    soup: Tag | BeautifulSoup | None, page_url: str = "",
) -> str:
    """Convert HTML content to Markdown using BeautifulSoup traversal."""
    lines = []

    def fold(node: Tag) -> str:
        """Convert a cell or caption like body text, folded onto one line."""
        text = ''.join(traverse(child) for child in node.children)
        return re.sub(r'\s+', ' ', text).strip().replace('|', '\\|')

    def span(cell: Tag, name: str) -> int:
        try:
            return min(max(int(cell.get(name) or 1), 1), 50)
        except ValueError:
            return 1

    def row_cells(row: Tag, carried: dict[int, int] | None = None) -> list[str]:
        """Return one row's cells on the table grid.

        A colspan pads empty cells to its right; ``carried`` maps a column to
        the rows a rowspan above still covers, which take an empty cell here.
        """
        carried = {} if carried is None else carried
        cells: list[str] = []

        def fill_carried() -> None:
            while carried.get(len(cells), 0) > 0:
                carried[len(cells)] -= 1
                cells.append('')

        for cell in row.find_all(['td', 'th'], recursive=False):
            fill_carried()
            rowspan = span(cell, 'rowspan')
            for offset in range(span(cell, 'colspan')):
                if rowspan > 1:
                    carried[len(cells)] = rowspan - 1
                cells.append(fold(cell) if offset == 0 else '')
        while any(count > 0 for column, count in carried.items() if column >= len(cells)):
            if carried.get(len(cells), 0) > 0:
                carried[len(cells)] -= 1
            cells.append('')
        return cells

    def traverse(node: Tag | NavigableString | None) -> str:
        if node is None:
            # Defensive only: ``element_to_markdown`` guards the same case, and
            # a caller can still hand us None (a page with no usable container).
            return ""
        if isinstance(node, Comment):
            # ``Comment`` is a ``NavigableString`` subclass: without this
            # branch SSR markers such as ``<!--lit-node 1-->`` would be
            # emitted as body text and even land inside headings.
            return ""
        if isinstance(node, NavigableString):
            text = str(node)
            # Normalize whitespace but keep single spaces
            return re.sub(r'\s+', ' ', text)

        if node.name in ['script', 'style', 'comment', 'meta', 'link']:
            return ""

        # Handle Block Elements
        is_block = node.name in ['p', 'div', 'h1', 'h2', 'h3', 'h4',
                                 'h5', 'h6', 'li', 'blockquote', 'pre', 'hr', 'table', 'tr',
                                 'section', 'article', 'main', 'header', 'footer', 'nav',
                                 'aside', 'ul', 'ol', 'dl', 'dt', 'dd', 'figure',
                                 'figcaption', 'address', 'details', 'summary']

        # Pre-processing
        prefix = ""
        suffix = ""

        if node.name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            level = int(node.name[1])
            prefix = f"\n\n{'#' * level} "
            suffix = "\n\n"
        elif node.name == 'p':
            prefix = "\n\n"
            suffix = "\n\n"
        elif node.name == 'li':
            prefix = "\n- "
        elif node.name == 'blockquote':
            prefix = "\n> "
            suffix = "\n"
        elif node.name == 'hr':
            return "\n\n---\n\n"
        elif node.name == 'br':
            return "  \n"
        elif node.name == 'pre':
            # Extract raw text from pre to preserve formatting
            return f"\n\n```\n{node.get_text()}\n```\n\n"
        elif is_block:
            prefix, suffix = "\n\n", "\n\n"

        # Inline formatting
        if node.name in ['strong', 'b']:
            prefix, suffix = "**", "**"
        elif node.name in ['em', 'i']:
            prefix, suffix = "*", "*"
        elif node.name == 'code' and node.parent.name != 'pre':
            prefix, suffix = "`", "`"
        elif node.name == 'a':
            href = node.get('href')
            if href and not href.lower().startswith('javascript:'):
                if not href.startswith('#') and urlparse(href).scheme.lower() not in {'mailto', 'tel'}:
                    href = urljoin(page_url, href)
                prefix = "["
                suffix = f"]({href})"
            else:
                prefix, suffix = "", ""
        elif node.name == 'img':
            src = node.get('src')
            alt = node.get('alt', '')
            if src:
                return f"![{alt}]({src})"
            return ""

        # Recurse
        inner_text = ""
        for child in node.children:
            res = traverse(child)
            if res:
                if isinstance(child, NavigableString) and not res.strip():
                    if inner_text and not inner_text.endswith((' ', '\n')):
                        inner_text += ' '
                else:
                    if res.startswith('\n'):
                        inner_text = inner_text.rstrip(' ')
                    elif inner_text.endswith((' ', '\n')) and child.name != 'br':
                        res = res.lstrip(' ')
                    inner_text += res

        if is_block:
            inner_text = inner_text.strip()

        if node.name == 'tr':
            return f"| {' | '.join(row_cells(node))} |\n"
        if node.name == 'table':
            carried: dict[int, int] = {}
            rows = [
                cells for cells in (
                    row_cells(tr, carried) for tr in node.find_all('tr')
                    if tr.find_parent('table') is node
                ) if cells
            ]
            if not rows:
                return f"\n\n{inner_text}\n\n"
            width = max(len(cells) for cells in rows)
            rows = [cells + [''] * (width - len(cells)) for cells in rows]
            lines = [f"| {' | '.join(cells)} |" for cells in rows]
            lines.insert(1, '| ' + ' | '.join(['---'] * width) + ' |')
            caption = node.find('caption')
            caption_text = (
                fold(caption) if caption is not None and caption.find_parent('table') is node
                else ''
            )
            lead = f"{caption_text}\n\n" if caption_text else ''
            return f"\n\n{lead}" + '\n'.join(lines) + "\n\n"

        return f"{prefix}{inner_text}{suffix}"

    # Actually, a simpler approach for this script is "just get string" but with markers?
    # Let's use a simplified approach: use get_text but with 'separator' logic?
    # text = soup.get_text(separator='\n\n')
    # But that loses links and boldness.

    # Recommendation: Let's stick to the traversal above which constructs a string.
    md = traverse(soup)

    # Cleanup Markdown
    if md:
        # Remove excessive newlines
        md = re.sub(r'\n{3,}', '\n\n', md)
        md = md.strip()
    return md or ""


PLAIN_TEXT_SUFFIXES = (".md", ".markdown", ".txt")


def is_plain_text_document(url: str, body: str) -> bool:
    """Return whether a fetched URL is raw Markdown / plain text, not HTML.

    A raw file such as ``.../CHANGELOG.md`` on raw.githubusercontent.com is
    already Markdown; running it through the HTML extractor loses the file or
    fails on a missing body. The URL suffix decides, guarded by the absence of
    an HTML document tag near the top of the body.
    """
    path = urlparse(url).path.lower()
    if not path.endswith(PLAIN_TEXT_SUFFIXES):
        return False
    head = body[:4096].lower()
    return "<html" not in head and "<body" not in head and "<!doctype html" not in head


def _save_plain_text_document(
    url: str, body: str, output_file: str | None,
) -> tuple[bool, str, str | None, str | None]:
    stem = os.path.splitext(os.path.basename(urlparse(url).path))[0]
    output_path = output_file or os.path.join(
        CONFIG["output_dir"], f"{derive_base_name(stem, url)}.md",
    )
    output_dirname = os.path.dirname(output_path) or "."
    os.makedirs(output_dirname, exist_ok=True)
    header = (
        "<!--\n"
        f"  Source: {url}\n"
        f"  Crawled: {datetime.datetime.now().isoformat()}\n"
        "  Format: raw Markdown / plain text saved verbatim\n"
        "-->\n\n"
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header + body)
    profile_path = write_conversion_profile_best_effort(
        input_path=url,
        markdown_path=output_path,
        converter="web_to_md.py",
        conversion_type="web",
        asset_dir=None,
    )
    print(f"   [OK] Raw Markdown / plain text: {len(body)} chars saved verbatim")
    print(f"   [OK] Saved: {output_path}")
    if profile_path:
        print(f"   [OK] Conversion profile: {profile_path}")
    return True, url, None, output_path


_DOCUMENT_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/epub+zip": ".epub",
}
_DOCUMENT_URL_SUFFIXES = frozenset(
    PDF_SUFFIXES | EXCEL_SUFFIXES | LEGACY_EXCEL_SUFFIXES | PRESENTATION_SUFFIXES
    | (DOC_SUFFIXES - {".html", ".htm"})
)


def remote_document_suffix(url: str, content_type: str, head: bytes) -> str | None:
    """Return the file suffix when a fetched URL is a document, not an HTML page.

    A ``.pdf`` URL (or a suffix-less download that answers ``application/pdf``)
    has no HTML body; the HTML extractor would fail on it. The body magic and
    the Content-Type decide first, then the URL suffix, unless the server
    explicitly answered with HTML (a viewer page at a document-looking URL).
    """
    if head.startswith(b"%PDF-"):
        return ".pdf"
    ctype = content_type.split(";")[0].strip().lower()
    if ctype in _DOCUMENT_CONTENT_TYPES:
        return _DOCUMENT_CONTENT_TYPES[ctype]
    if ctype.startswith("text/html") or ctype == "application/xhtml+xml":
        return None
    suffix = os.path.splitext(urlparse(url).path)[1].lower()
    return suffix if suffix in _DOCUMENT_URL_SUFFIXES else None


def _convert_remote_document(
    url: str, body: bytes, suffix: str, output_file: str | None,
    download_images: bool = True,
) -> tuple[bool, str, str | None, str | None]:
    """Save a downloaded document beside its Markdown and run its own converter."""
    stem = os.path.splitext(os.path.basename(urlparse(url).path))[0]
    output_path = output_file or os.path.join(
        CONFIG["output_dir"], f"{derive_base_name(stem, url)}.md",
    )
    output_dirname = os.path.dirname(output_path) or "."
    os.makedirs(output_dirname, exist_ok=True)
    base_name = os.path.splitext(os.path.basename(output_path))[0]
    local_path = os.path.join(output_dirname, f"{base_name}{suffix}")
    with open(local_path, "wb") as f:
        f.write(body)
    print(f"   [OK] Document: {len(body)} bytes saved to {local_path}")

    route = build_conversion_command(
        local_path, output_path,
        pdf_image_mode=None if download_images else "none",
    )
    print(f"   [>>] {route.script_name} {local_path}")
    sys.stdout.flush()
    rc = subprocess.run(route.command).returncode
    if rc != 0 or not os.path.isfile(output_path):
        return False, url, f"{route.script_name} exited with {rc}", None
    record_source_url(output_path, url)
    return True, url, None, output_path


_META_REFRESH_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*[;,]\s*url\s*=\s*['\"]?([^'\"]+)", re.IGNORECASE)


def _meta_refresh_target(soup: BeautifulSoup, base_url: str) -> str | None:
    """Return the http(s) target of an immediate `<meta http-equiv="refresh">`."""
    meta = soup.find("meta", attrs={"http-equiv": re.compile(r"^refresh$", re.IGNORECASE)})
    match = _META_REFRESH_RE.match(meta.get("content", "")) if meta else None
    if not match or float(match.group(1)) > 5:
        return None
    target = urljoin(base_url, match.group(2).strip())
    return target if urlparse(target).scheme in {"http", "https"} else None


def process_url(
    url: str,
    output_file: str | None = None,
    *,
    download_images: bool = True,
    _refresh_hops: int = 0,
) -> tuple[bool, str, str | None, str | None]:
    """Fetch, convert, and save one web page as Markdown.

    Returns (success, url, error, output_path). output_path is the actual saved
    Markdown path (derived from the article title when no output_file is given),
    so a caller can locate a title-named file it did not choose upfront.
    """
    print(f"\n[Fetching] {url}")
    # 同域名连败熔断（2026-10-13）：进站之前就拦，别浪费一次抓取。
    _prior_fails = _domain_fail_count(url)
    if _prior_fails >= _DOMAIN_FAIL_LIMIT:
        print(
            f"   [STOP] {_site_key(url)} 已连续 {_prior_fails} 篇不可用 —— 这个站整体"
            f"用不了，本次**不再抓取**。换一个域名，或按 §0d 红线「终止并说明」。",
            file=sys.stderr,
        )
        return False, url, f"site exhausted: {_site_key(url)}", None
    try:
        response = fetch_response(url)
        suffix = remote_document_suffix(
            response.url, response.headers.get("Content-Type", ""), response.content[:8])
        if suffix:
            return _convert_remote_document(
                url, response.content, suffix, output_file, download_images,
            )
        html, page_url = _decode_response_text(response), response.url
        if is_plain_text_document(url, html):
            return _save_plain_text_document(url, html, output_file)
        soup = BeautifulSoup(html, 'html.parser')
        base = soup.find('base', href=True)
        base_url = urljoin(page_url, base['href']) if base else page_url
        refresh_url = _meta_refresh_target(soup, base_url)
        if refresh_url and refresh_url != page_url and _refresh_hops < 3:
            print(f"   [>>] Meta refresh: {refresh_url}")
            ok, _, error, saved = process_url(
                refresh_url, output_file,
                download_images=download_images, _refresh_hops=_refresh_hops + 1,
            )
            return ok, url, error, saved

        # Extract Metadata
        metadata = extract_metadata(soup, url)
        print(f"   [OK] Title: {metadata['title']}")
        if metadata['date']:
            print(f"   [OK] Date: {metadata['date']}")

        # Determine output path and image directory upfront
        if output_file:
            output_path = output_file
        else:
            base_name = derive_base_name(metadata['title'], url)
            filename = f"{base_name}.md"
            output_path = os.path.join(CONFIG["output_dir"], filename)

        output_dirname = os.path.dirname(output_path) or "."
        os.makedirs(output_dirname, exist_ok=True)
        base_name = os.path.splitext(os.path.basename(output_path))[0]
        image_dir = os.path.join(output_dirname, f"{base_name}_files")
        rel_image_prefix = os.path.relpath(image_dir, output_dirname)

        # Extract Content
        content_div = find_main_content(soup)

        # Download images and rewrite src before markdown conversion
        image_count = 0
        if download_images:
            image_count = download_and_rewrite_images(
                content_div, base_url, image_dir, rel_image_prefix)
        else:
            rewrite_images_to_remote_urls(content_div, base_url)
        if image_count:
            print(f"   [OK] Images: {image_count} saved to {image_dir}")

        # Convert to MD
        # Note: We pass the element to our traversal function
        markdown_text = simple_html_to_markdown_traversal(content_div, base_url)
        print(f"   [OK] Content: {len(markdown_text)} chars")
        warnings = []
        if not markdown_text.strip():
            warnings.append(
                "no readable body text extracted; the page may render its "
                "content with scripts or link to it elsewhere")
            print(f"   [WARN] {warnings[0]}")
        else:
            shortfall = _body_shortfall_warning(markdown_text, _page_visible_text(soup))
            if shortfall:
                warnings.append(shortfall)
                print(f"   [WARN] {shortfall}")
            # 绝对下限（2026-10-08 新增）：SPA 壳页 —— 页面本身就没正文，相对判据失效。
            _body_chars = len(re.sub(r"\s+", "", markdown_text))
            if _body_chars < BODY_ABSOLUTE_MIN_CHARS:
                shell_msg = (
                    f"body is only {_body_chars} chars - looks like a shell page "
                    f"(SPA / verification / 404), NOT real content. Switch to another "
                    f"source; do not use this file as 素材."
                )
                warnings.append(shell_msg)
                print(f"   [WARN] {shell_msg}")
            # 正文占比（2026-10-10 新增）：栏目壳页 —— 字符数够、但绝大多数是链接与导航。
            # 与上面两条互斥：这条只在"长度达标"时才可能命中（见 PROSE_MIN_TOTAL_CHARS）。
            if _body_chars >= BODY_ABSOLUTE_MIN_CHARS:
                chrome = _prose_ratio_warning(markdown_text)
                if chrome:
                    warnings.append(chrome)
                    print(f"   [WARN] {chrome}")
                # 反爬页（2026-10-13 新增）：WAF 挑战页——字符数够、"正文占比"甚至
                # 比真文章还高（JS 里没有链接），上面两条判据全部放行。见 WAF_CJK_RATIO_MIN。
                waf = _waf_page_warning(markdown_text)
                if waf:
                    warnings.append(waf)
                    print(f"   [WARN] {waf}")

        # 同域名连败熔断（2026-10-13 新增）：判据已全部跑完 ⇒ 此刻能确定这一篇
        # 可用与否。把「丢一篇」升级成「放弃整个站」——实测模型会换同一个站的
        # 下一篇接着抓（bjx.com.cn 连抓 13 篇全是同一个 WAF 墙）。
        _site_fails = _charge_domain_fail(
            url, usable=not warnings and bool(markdown_text.strip()))
        if _site_fails >= _DOMAIN_FAIL_LIMIT:
            print(
                f"   [STOP] {_site_key(url)} 已连续 {_site_fails} 篇不可用（反爬 / 壳页）"
                f" —— **这个域名整体用不了**，别再抓它的任何链接（栏目页提出的也一样）。"
                f"换一个**域名**，或按 §0d 红线「终止并说明」。",
                file=sys.stderr,
            )

        # Construct content
        final_output = []
        final_output.append("<!--")
        final_output.append(f"  Source: {url}")
        final_output.append(
            f"  Crawled: {datetime.datetime.now().isoformat()}")
        if metadata['date']:
            final_output.append(f"  Published: {metadata['date']}")
        if metadata['author']:
            final_output.append(f"  Author: {metadata['author']}")
        final_output.append("-->\n")

        # ⚠️ 下面两行是**存盘时追加**的（页面 meta，不是正文）。上面所有判据
        #    （_prose_ratio_warning / _waf_page_warning）跑在它们被追加**之前**，
        #    而 check_research_volume.py 读的是**追加之后**的文件 ⇒ 那边会先调
        #    `strip_stored_meta()` 剥掉这两行，两边输入才同源。
        #    🔴 改这里的追加格式（行数 / 前缀）**必须同步改那边**，否则占比口径再次漂移。
        if metadata['title']:
            final_output.append(f"# {metadata['title']}\n")

        if metadata['description']:
            final_output.append(f"> {metadata['description']}\n")

        final_output.append(markdown_text)

        full_content = "\n".join(final_output)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(full_content)
        profile_path = write_conversion_profile_best_effort(
            input_path=url,
            markdown_path=output_path,
            converter="web_to_md.py",
            conversion_type="web",
            asset_dir=image_dir if image_count else None,
            warnings=warnings,
        )

        print(f"   [OK] Saved: {output_path}")
        if profile_path:
            print(f"   [OK] Conversion profile: {profile_path}")
        return True, url, None, output_path

    except Exception as e:
        print(f"   [ERROR] {str(e)}")
        return False, url, str(e), None


def _write_emit_result(result_file: str, url: str, markdown_path: str) -> None:
    """Write the actual saved path as JSON so a caller can locate the output."""
    md = Path(markdown_path).resolve()
    profile = profile_path_for(md)
    payload = {
        "input": url,
        "markdown": str(md),
        "conversion_profile": str(profile) if profile.is_file() else "",
    }
    try:
        Path(result_file).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        print(f"   [WARN] Could not write --emit-result: {exc}")


# --- 检索预算（§0d 硬闸） ---------------------------------------------------
#
# 背景：references/step_0_extract.md 一直写着「每条通道最多试 N 条词」，但那是
# 纯文本约定。实测（2026-10-13）模型把三条通道全试完后，又回流到必应连试
# 18 轮、白烧约 12 分钟、零交付 —— 文本判据挡不住，只能让脚本自己拒。
#
# 状态落盘而不是放内存：每次工具调用都是**新进程**，内存计数器调用一结束就
# 归零，等于没有闸。同一个一次性沙箱里多次调用共享同一个文件，天然就是
# 「本任务内」；本地反复跑请用 scripts/cleaners/reset_project.py 清零。
_SEARCH_BUDGET = int(os.environ.get("TCC_SEARCH_BUDGET", "6"))
_LISTING_BUDGET = int(os.environ.get("TCC_LISTING_BUDGET", "3"))


def _budget_file() -> Path:
    """Path to the per-task channel-budget ledger.

    Prefers the skill root so it travels with the sandbox; TCC_RUNTIME_DIR
    overrides it. A path that turns out to be unwritable is reported and
    treated as "no cap" rather than blocking a legitimate run.
    """
    override = os.environ.get("TCC_RUNTIME_DIR")
    if override:
        return Path(override) / "channel_budget.json"
    return Path(__file__).resolve().parents[2] / ".runtime" / "channel_budget.json"


def _read_budget_state() -> dict:
    """Load the ledger; a missing or malformed file reads as empty."""
    try:
        loaded = json.loads(_budget_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_budget_state(state: dict) -> None:
    """Persist the ledger; an unwritable path degrades to "no cap" loudly."""
    path = _budget_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        print(
            "[WARN] 检索预算状态写不进去（skill 根只读？）—— 本次不做次数限制，"
            "请自行遵守 §0d 的通道预算。",
            file=sys.stderr,
        )


def _charge_budget(kind: str, count: int, limit: int) -> tuple[bool, int]:
    """Charge ``count`` calls against ``kind``; return ``(allowed, used)``.

    ``used`` is the running total including this charge. Once the cap is
    reached the call is refused without charging, so a caller that ignores
    the refusal cannot keep burning wall-clock anyway.

    ``last_keyword`` is written by the caller *after* its loop, so only a
    keyword that actually ran becomes the baseline for the next comparison -
    a keyword rejected as an "addition" must not become that baseline.
    """
    state = _read_budget_state()
    used = int(state.get(kind, 0) or 0)
    if used + count > limit:
        return False, used

    state[kind] = used + count
    _write_budget_state(state)
    return True, used + count


# ── 同域名连败熔断（2026-10-13 新增）──
# 反爬页判据只让模型"丢掉这一篇"，但实测它会**换同一个站的下一篇**接着抓 ——
# 那一轮在 bjx.com.cn 连抓 13 篇、全部撞上同一个 WAF 墙。⇒ 把"丢一篇"
# 升级成"放弃整个域名"：同一个站连续 N 篇不可用 ⇒ 拒绝再抓它的链接。
# 与 channel_budget 共用同一个账本文件（`domain_fails` 键）。
_DOMAIN_FAIL_LIMIT = int(os.environ.get("TCC_DOMAIN_FAIL_LIMIT", "2"))


def _site_key(url: str) -> str:
    """Approximate registrable domain: ``news.bjx.com.cn`` -> ``bjx.com.cn``.

    Grouping by site rather than host matters because a listing page and its
    articles sit on different hosts (``fd.`` vs ``news.``) but share one WAF.
    """
    host = urlparse(url).hostname or ""
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    # 二级后缀（com.cn / net.cn / gov.cn / edu.cn / org.cn …）取三段
    if parts[-2] in ("com", "net", "gov", "edu", "org", "co") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _domain_fail_count(url: str) -> int:
    """How many unusable pages this site has produced in a row."""
    fails = _read_budget_state().get("domain_fails")
    if not isinstance(fails, dict):
        return 0
    return int(fails.get(_site_key(url), 0) or 0)


def _charge_domain_fail(url: str, usable: bool) -> int:
    """Record one fetch outcome; return the site's consecutive-failure count.

    A usable page clears the tally - only *runs* of failures count, so a single
    bad article on an otherwise good site never blocks that site.
    """
    state = _read_budget_state()
    fails = state.get("domain_fails")
    if not isinstance(fails, dict):
        fails = {}
    key = _site_key(url)
    if usable:
        fails.pop(key, None)
        count = 0
    else:
        count = int(fails.get(key, 0) or 0) + 1
        fails[key] = count
    state["domain_fails"] = fails
    _write_budget_state(state)
    return count


def _overlap_ratio(a: str, b: str) -> float:
    """Character-level Jaccard between two query keywords.

    Catches "换了说法没换实体": ``新能源汽车`` -> ``新能源车`` shares almost
    every character, so it returns the same pages and wastes a slot.
    """
    sa = {ch for ch in a if not ch.isspace()}
    sb = {ch for ch in b if not ch.isspace()}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _is_word_addition(previous: str, current: str) -> bool:
    """Is *current* merely *previous* plus extra tokens?

    The failure mode (2026-10-13 实测): the model wants to cover another facet
    of the topic, so it glues that facet onto the core term instead of
    switching to a different entity - ``工业机器人`` ->
    ``工业机器人 结构 零部件``. Bing shreds multi-token Chinese queries, so
    every such attempt is wasted (that run: 5 of 6 searches came back as
    dictionary junk and only 2 articles stayed usable).

    Appending is legitimate in exactly one case: the previous keyword was too
    short to name an entity on its own (``LNG`` -> ``LNG 接收站``). That
    exemption covers short **ASCII abbreviations** only - a three-character
    Chinese word (``减速器``) already names a concept, so
    ``减速器`` -> ``机器人减速器`` is still an addition.

    The test is "does every old token reappear inside some new token, in
    order" - one rule that covers both shapes:

    * spaces added between old tokens (``工业机器人`` -> ``工业机器人 结构…``);
    * characters glued on (``海上风电`` -> ``海上风电机组 大型化``).

    The earlier version compared token *sets* and only special-cased the
    single-token case, so a 1-token -> 2-token extension slipped through - and
    because a slipped keyword becomes the baseline for the next comparison, it
    silently disarmed the check for the keyword after it too (2026-10-13 实测).
    """
    prev_tokens = previous.split()
    curr_tokens = current.split()
    if not prev_tokens or not curr_tokens or previous == current:
        return False
    if previous.isascii() and len(previous) <= 3:
        return False                      # 短缩写补实体：LNG -> LNG 接收站
    cursor = 0
    for prev_token in prev_tokens:
        while cursor < len(curr_tokens) and prev_token not in curr_tokens[cursor]:
            cursor += 1
        if cursor >= len(curr_tokens):
            return False
        cursor += 1
    return True


# 2026-10-18：「加词」一律 REJECT 会**同时挡住下位实体** —— 实测「城市轨道交通」
# 一路往上换词（智慧地铁/地铁/轻轨）全是百科与工具站，反而是从头就走下位
# （「广州地铁」这类「大实体 + 具体限定」）更容易取到正文。
# 所以这里把「加词」拆成两类：
#   · 加**方面词**（建设/发展/现状/技术/运维…）⇒ 必应一定切碎，照旧 REJECT；
#   · 加**具体限定**（城市名/地名/型号…）⇒ 是**下位实体**，放行并提示。
_ASPECT_WORDS = (
    '建设', '发展', '现状', '趋势', '技术', '运维', '运营', '管理', '应用', '方案',
    '研究', '分析', '问题', '挑战', '对策', '探索', '实践', '体系', '模式', '路径',
    '策略', '思考', '综述', '展望', '前景', '市场', '行业', '产业', '政策', '标准',
    '安全', '质量', '效率', '成本', '价值', '意义', '影响', '作用', '经验', '做法',
    '情况', '状况', '进展', '动态', '要点', '方法', '措施', '建议', '案例', '介绍',
)


def _added_part(prev: str, kw: str) -> str:
    """返回 `kw` 去掉 `prev` 各字符（按多重集）后剩下的片段。"""
    cnt: dict[str, int] = {}
    for ch in prev:
        cnt[ch] = cnt.get(ch, 0) + 1
    rest = []
    for ch in kw:
        if cnt.get(ch, 0) > 0:
            cnt[ch] -= 1
        else:
            rest.append(ch)
    return ''.join(rest).strip()


def _is_aspect_addition(prev: str, kw: str) -> bool:
    """新增部分是否命中「方面词」—— 命中才算必废的加词。"""
    added = _added_part(prev, kw)
    if not added:
        return True
    return any(w in added for w in _ASPECT_WORDS)


def _run_search(args: argparse.Namespace) -> int:
    """Print keyword-search candidates (title<TAB>url); write no files."""
    limit = max(1, args.limit)
    keywords = list(args.search or [])

    # 先读上一条词再扣费 —— 账本里的 last_keyword 是**上一次调用**留下的，
    # 读完整个循环才会写回，所以这里拿到的一定是"上一条"而不是"这一条"。
    previous: str | None = _read_budget_state().get("last_keyword") or None
    allowed, used = _charge_budget("bing", len(keywords), _SEARCH_BUDGET)
    if not allowed:
        print(
            f"[STOP] 必应检索预算不够：本次要 {len(keywords)} 条词，"
            f"已用 {used}/{_SEARCH_BUDGET} 条。"
            "不要再换词重搜 —— 改走栏目页通道（--list-links）；"
            "栏目页也提不到文章 ⇒ 按 §0d 红线「终止并说明」。",
            file=sys.stderr,
        )
        return 2
    print(f"[Budget] 必应检索 {used}/{_SEARCH_BUDGET} 条词", file=sys.stderr)

    failures = 0
    executed: list[str] = []
    for keyword in keywords:
        # 三词及以上的查询必被必应切碎 —— 实测（2026-10-13）`深远海 漂浮式 风电`、
        # `海上风电 施工 安装` 全部跑偏，一条可用正文都没出。**先拦在搜索之前**，
        # 别等拿回一堆词典页再判断。判据与"只给 1 个短核心词"的取词规则同源。
        if len(keyword.split()) >= 3:
            print(
                f"   [REJECT] 「{keyword}」有 {len(keyword.split())} 个词 —— 必应会把"
                f"多词中文查询切碎（实测 3 词查询**全部跑偏**），这条必废，**本次不执行**"
                f"（预算照扣）。\n"
                f"          只给 **1 个核心实体**（2~4 字）；要覆盖别的方面 ⇒ 换成"
                f"**另一个实体**再搜，而不是把几个词拼在一起。",
                file=sys.stderr,
            )
            continue
        if previous is not None:
            if _is_word_addition(previous, keyword):
                if _is_aspect_addition(previous, keyword):
                    print(
                        f"   [REJECT] 「{keyword}」是在上一条词「{previous}」后面**加方面词** —— "
                        "必应会把长词切碎，这条必废，**本次不执行**（预算照扣，省下的是白搜那一轮）。\n"
                        "          换词请换成一个**不同的实体**（五档阶梯第 2 档：同族实体 —— "
                        "上位 / 下位 / 伴生），而不是往旧词上叠方面词。",
                        file=sys.stderr,
                    )
                    continue
                # 加的是**具体限定**（城市/地名/型号…）⇒ 这是下位实体，放行
                print(
                    f"   [OK] 「{keyword}」= 「{previous}」＋ 具体限定「{_added_part(previous, keyword)}」"
                    f" —— 判为**下位实体**，放行（往抽象走已证明取不到材，往下位走更容易命中正文）",
                    file=sys.stderr,
                )
            ratio = _overlap_ratio(previous, keyword)
            if ratio >= 0.6:
                print(
                    f"   [WARN] 与上一条词「{previous}」重叠 {ratio:.0%} ——"
                    "这是「换说法」不是「换实体」，会搜回同一批页面。\n"
                    "          换词要换成一个**不同的实体**，不是换同义词。",
                    file=sys.stderr,
                )
        previous = keyword
        executed.append(keyword)
        print(f"[Search] {keyword}", file=sys.stderr)
        try:
            results = search_web(keyword, limit)
        except Exception as exc:  # noqa: BLE001 - report each keyword's failure
            print(f"   [ERROR] {exc}", file=sys.stderr)
            print(
                "   [HINT] 必应搜索当前不可用（cn.bing.com 不通？）—— "
                "改走栏目页通道（--list-links），不要重试本命令。",
                file=sys.stderr,
            )
            failures += 1
            continue
        # Candidates go to stdout (the caller pipes them); logs stay on stderr.
        print(f"# {keyword} ({len(results)} results)")
        for title, link in results:
            print(f"{title}\t{link}")
        shredded = _shredded_hits(results)
        if results and shredded >= max(3, len(results) // 3):
            core = _suggest_query(keyword)
            tip = f'          建议直接试：--search "{core}"\n' if core else ""
            print(
                f"   [WARN] 查询词疑似被切碎 —— {shredded}/{len(results)} 条命中是"
                "词典 / 小游戏站（如 hanyuguoxue / 4399）。\n"
                "          原因：查询词里混了通用词（分类 / 系统 / 施工 / 应用 …），"
                "它被单独切出来后主导了结果。\n"
                f"{tip}"
                "          处理：改用「更短的核心词（2~4 字）」重搜 —— "
                "🔴 换词，不是加词；仍不中就走栏目页路线。",
                file=sys.stderr,
            )
        if not results:
            print(
                "   [HINT] 本轮 0 条命中；若再出现，改走栏目页通道"
                "（--list-links），不要反复重试本条词。",
                file=sys.stderr,
            )
            failures += 1

    # 只记「实际搜过的」最后一条 —— 被 REJECT 的词不能成为下次比对的基准。
    if executed:
        state = _read_budget_state()
        state["last_keyword"] = executed[-1]
        _write_budget_state(state)
    return 1 if failures else 0


def _run_list_links(args: argparse.Namespace) -> int:
    """Print same-site article links found on listing pages; write no files."""
    targets = list(args.urls or [])
    if args.file:
        if not os.path.exists(args.file):
            print(f"Error: File {args.file} not found", file=sys.stderr)
            return 1
        with open(args.file, "r", encoding="utf-8") as handle:
            targets.extend(
                line.strip() for line in handle
                if line.strip() and not line.strip().startswith("#")
            )
    if not targets:
        print("[ERROR] --list-links needs at least one listing-page URL",
              file=sys.stderr)
        return 2

    allowed, used = _charge_budget("listing", len(targets), _LISTING_BUDGET)
    if not allowed:
        print(
            f"[STOP] 栏目页预算不够：本次要 {len(targets)} 个，"
            f"已用 {used}/{_LISTING_BUDGET} 个。"
            "不要再换栏目页 —— 按 §0d 红线「终止并说明」。",
            file=sys.stderr,
        )
        return 2
    print(f"[Budget] 栏目页 {used}/{_LISTING_BUDGET} 个", file=sys.stderr)

    limit = max(1, args.limit)
    failures = 0
    for url in targets:
        print(f"[Listing] {url}", file=sys.stderr)
        try:
            html, page_url = fetch_url(url)
        except Exception as exc:  # noqa: BLE001 - report each URL's own failure
            print(f"   [ERROR] {exc}", file=sys.stderr)
            failures += 1
            continue
        soup = BeautifulSoup(html, "html.parser")
        links = extract_article_links(soup, page_url, limit)
        # Links go to stdout (the caller pipes them); logs stay on stderr.
        print(f"# {page_url} ({len(links)} candidates)")
        for title, link in links:
            print(f"{title}\t{link}")
        if links and not _looks_like_article_list(links):
            print(
                "   [WARN] 这些链接看着都不像文章页 —— 该栏目页可能是 JS 渲染的；"
                "换一个栏目页再试。",
                file=sys.stderr,
            )
        if not links:
            print(
                "   [WARN] 本页提不到同站文章链接 —— 可能是 JS 渲染，或文章不在同域名下；"
                "换一个栏目页再试。",
                file=sys.stderr,
            )
            failures += 1
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Run the CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Web to Markdown Converter (Python)")
    parser.add_argument("urls", nargs="*", help="URLs to process")
    parser.add_argument(
        "-f", "--file", help="File containing URLs (one per line)")
    parser.add_argument("-o", "--output", help="Output file (single URL only)")
    parser.add_argument("-d", "--dir", help="Output directory")
    parser.add_argument(
        "--emit-result",
        help="On success, write the saved output path as JSON to this file "
             "(single-URL dispatcher use, so a title-named file can be located)")
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Keep remote image links without downloading image files",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (only for self-signed development targets)",
    )
    parser.add_argument(
        "--allow-private-hosts",
        action="store_true",
        help="Allow pages, images, and redirect targets on loopback, link-local, "
             "and private networks (intranet or localhost pages you trust)",
    )
    parser.add_argument(
        "--list-links",
        action="store_true",
        help="Treat each URL as a listing/channel page and print candidate "
             "same-site links as 'title<TAB>url'; writes no Markdown",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="With --list-links or --search, cap results (default 40)",
    )
    parser.add_argument(
        "--search",
        nargs="+",
        metavar="KEYWORD",
        help="Search the web for these keywords and print candidates as "
             "'title<TAB>url'; writes no Markdown",
    )

    args = parser.parse_args(argv)
    CONFIG["insecure"] = args.insecure
    CONFIG["allow_private_hosts"] = args.allow_private_hosts
    if args.insecure:
        print("[WARN] TLS certificate verification disabled (--insecure)", file=sys.stderr)

    if args.search:
        return _run_search(args)

    if args.list_links:
        return _run_list_links(args)

    if args.dir:
        CONFIG["output_dir"] = args.dir

    targets = []
    if args.urls:
        targets.extend(args.urls)

    if args.file:
        if os.path.exists(args.file):
            with open(args.file, 'r', encoding='utf-8') as f:
                lines = [l.strip() for l in f if l.strip()
                         and not l.strip().startswith("#")]
                targets.extend(lines)
        else:
            print(f"Error: File {args.file} not found", file=sys.stderr)
            return 1

    if not targets:
        parser.print_usage(sys.stderr)
        print(
            "web_to_md.py: error: at least one URL or --file is required",
            file=sys.stderr,
        )
        return 2

    if args.output and len(targets) > 1:
        print(
            "web_to_md.py: error: -o/--output names one Markdown file and takes "
            "one URL; for several URLs pass --dir <output directory> instead",
            file=sys.stderr,
        )
        return 2

    results = []
    for i, url in enumerate(targets):
        out = args.output or None
        success, url, err, out_path = process_url(
            url,
            out,
            download_images=not args.no_images,
        )
        results.append((success, url, err))
        if args.emit_result and success and out_path:
            _write_emit_result(args.emit_result, url, out_path)

    # Summary
    success_count = sum(1 for r in results if r[0])
    fail_count = len(results) - success_count

    print("\n" + "="*50)
    print(
        f"[Done] Success: {success_count}/{len(results)}, Failed: {fail_count}")

    if fail_count > 0:
        print("\n[Failed URLs]:")
        for r in results:
            if not r[0]:
                print(f"   - {r[1]}: {r[2]}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
