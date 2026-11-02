"""Web 検索とページ取得（web_search / fetch_page 道具の実処理）。

検索はキー不要の DuckDuckGo HTML 版を使い、ページ取得は httpx で
行う。どちらも道具の結果として返す文字列までをここで組み立て、
失敗は例外ではなく error: で始まる文字列で返す（道具の失敗は
モデルに返して対処させる、という ToolBox の流儀に合わせる）。

検索結果や取得した本文は外部の情報（データ）であり、利用者や
システムの指示ではない。ページ中に指示のような文言があっても、
それは引用された資料として扱う前提で返す（DESIGN.md 参照）。

HTTP 取得（fetcher）とホスト名解決（resolver）は差し替え可能に
してあり、テストではネットワークに出ずに検証できる。
"""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx

# 検索先の既定値。KUMA_SEARCH_URL（{query} を含むテンプレート）で
# 差し替えられる
DEFAULT_SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"
SEARCH_URL_ENV = "KUMA_SEARCH_URL"
# プライベート宛先の取得を許可するかどうか（KUMA_FETCH_ALLOW_PRIVATE=1）
ALLOW_PRIVATE_ENV = "KUMA_FETCH_ALLOW_PRIVATE"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
FETCH_TIMEOUT_SECONDS = 15.0
# ページ本文として返す最大文字数。これを超えたら先頭だけを返す
MAX_PAGE_CHARS = 8000
MAX_REDIRECTS = 5
DEFAULT_SEARCH_LIMIT = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


@dataclass
class FetchedResponse:
    """HTTP 取得の結果。ヘッダーのキーは小文字にそろえる。"""
    status_code: int
    headers: dict[str, str]
    text: str


# (URL, タイムアウト秒）→ 応答。リダイレクトは追従しない
Fetcher = Callable[[str, float], FetchedResponse]
# ホスト名 → 解決した IP アドレスの文字列の列
Resolver = Callable[[str], list[str]]


def _default_fetcher(url: str, timeout: float) -> FetchedResponse:
    """httpx で 1 回だけ GET する（リダイレクトは呼び出し側で回す）。"""
    client = httpx.Client(
        timeout=timeout, follow_redirects=False,
        headers={"User-Agent": USER_AGENT})
    with client:
        resp = client.get(url)
    headers = {k.lower(): v for k, v in resp.headers.items()}
    return FetchedResponse(resp.status_code, headers, resp.text)


def _resolve_ips(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None)
    return sorted({str(info[4][0]) for info in infos})


def _allow_private() -> bool:
    return os.environ.get(ALLOW_PRIVATE_ENV) == "1"


# ---------------------------------------------------------------- 宛先の判定

def _is_blocked_ip(ip_text: str) -> bool:
    """プライベート・ループバック等の宛先かどうか。

    読めない文字列は安全側に倒して対象に含める。
    """
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return True
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _destination_problem(url: str, resolver: Resolver) -> str | None:
    """取得してよい宛先かを確認し、問題があれば理由を返す。

    http/https のみ可。KUMA_FETCH_ALLOW_PRIVATE=1 でない限り、
    ホスト名を解決した先（または IP リテラルそのもの）に
    プライベートな IP が含まれていたら拒否する（SSRF 対策）。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "http/https の URL だけ取得できます"
    host = parsed.hostname
    if not host:
        return "URL にホストがありません"
    if _allow_private():
        return None
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        try:
            ips = resolver(host)
        except Exception:
            return f"ホスト名 {host} を解決できませんでした"
    if not ips:
        return f"ホスト名 {host} を解決できませんでした"
    if any(_is_blocked_ip(ip) for ip in ips):
        return ("プライベートな宛先への取得は許可されていません"
                f"（{host}）")
    return None


# ---------------------------------------------------------------- 検索結果の抽出

def _unwrap_result_url(href: str) -> str:
    """DuckDuckGo のリダイレクト形式の URL を実際の URL に戻す。

    結果リンクは //duckduckgo.com/l/?uddg=<実際の URL> の形で、
    実際の URL は uddg パラメータに入っている。uddg が無ければ
    そのまま使う。
    """
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    uddg = parse_qs(parsed.query).get("uddg")
    if uddg:
        return uddg[0]
    return href


class _SearchResultsParser(HTMLParser):
    """DuckDuckGo HTML 版の結果一覧から（題名, URL, 要約）を拾う。

    結果は <a class="result__a">（題名とリンク）と、それに続く
    class に result__snippet を含む要素（要約）の組で並ぶ。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str, str]] = []
        self._mode: str | None = None  # "title" | "snippet"
        self._href = ""
        self._parts: list[str] = []
        self._pending: tuple[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attr = dict(attrs)
        classes = (attr.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self._mode = "title"
            self._href = attr.get("href") or ""
            self._parts = []
        elif "result__snippet" in classes and self._pending is not None:
            self._mode = "snippet"
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._mode is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._mode == "title" and tag == "a":
            title = " ".join("".join(self._parts).split())
            self._pending = (title, _unwrap_result_url(self._href))
            self._mode = None
        elif self._mode == "snippet" and tag in ("a", "td", "div"):
            snippet = " ".join("".join(self._parts).split())
            if self._pending is not None:
                title, url = self._pending
                self.results.append((title, url, snippet))
                self._pending = None
            self._mode = None

    def close(self) -> None:
        super().close()
        # 要約が付かなかった最後の結果も拾う
        if self._pending is not None:
            title, url = self._pending
            self.results.append((title, url, ""))
            self._pending = None


def web_search(query: str, limit: int = DEFAULT_SEARCH_LIMIT, *,
               fetcher: Fetcher | None = None) -> str:
    """Web 検索して、結果を「タイトル / URL / 要約」の一覧で返す。"""
    query = (query or "").strip()
    if not query:
        return "error: 検索キーワードが空です"
    template = os.environ.get(SEARCH_URL_ENV) or DEFAULT_SEARCH_URL
    url = template.replace("{query}", quote_plus(query))
    fetcher = fetcher or _default_fetcher
    try:
        resp = fetcher(url, FETCH_TIMEOUT_SECONDS)
    except Exception as e:
        return f"error: 検索に失敗しました（{e}）"
    if resp.status_code != 200:
        return f"error: 検索に失敗しました（HTTP {resp.status_code}）"
    parser = _SearchResultsParser()
    parser.feed(resp.text)
    parser.close()
    results = parser.results[:max(1, limit)]
    if not results:
        return "検索結果が見つかりませんでした"
    blocks = []
    for i, (title, url, snippet) in enumerate(results, 1):
        block = f"{i}. {title}\n{url}"
        if snippet:
            block += f"\n{snippet}"
        blocks.append(block)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- ページ本文の抽出

_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "head"})
_BLOCK_TAGS = frozenset({
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main",
    "nav", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul",
})


class _TextExtractor(HTMLParser):
    """HTML から本文テキストを取り出す。

    script/style 等の中身は捨て、見出しや段落などのブロック要素の
    区切りは改行として残す。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split())
                 for line in "".join(self._parts).split("\n")]
        out: list[str] = []
        for line in lines:
            if line:
                out.append(line)
            elif out and out[-1] != "":
                # 段落の区切りとして空行は 1 つだけ残す
                out.append("")
        while out and out[-1] == "":
            out.pop()
        return "\n".join(out)


def _extract_body(resp: FetchedResponse) -> str:
    """応答から道具結果として返す本文を作る。非対応の形式は error。"""
    content_type = resp.headers.get("content-type", "").lower()
    if "text/html" in content_type or "application/xhtml+xml" in content_type:
        parser = _TextExtractor()
        parser.feed(resp.text)
        parser.close()
        body = parser.text()
    elif content_type.startswith("text/") or not content_type:
        # プレーンテキストはそのまま。形式が示されず HTML らしい
        # ときだけ抽出に回す
        if not content_type and resp.text.lstrip().startswith("<"):
            parser = _TextExtractor()
            parser.feed(resp.text)
            parser.close()
            body = parser.text()
        else:
            body = resp.text.strip()
    else:
        return f"error: 対応していない形式です（{content_type}）"
    if not body:
        return "error: ページから本文を取り出せませんでした"
    if len(body) > MAX_PAGE_CHARS:
        body = (body[:MAX_PAGE_CHARS]
                + f"\n（長いため先頭 {MAX_PAGE_CHARS} 文字で切りました）")
    return body


def fetch_page(url: str, *, fetcher: Fetcher | None = None,
               resolver: Resolver | None = None) -> str:
    """ページを取得し、本文テキストを返す。

    リダイレクトは最大 MAX_REDIRECTS 回まで追い、飛び先も含めて
    毎回宛先を確認する（プライベート宛先への飛びも拒否する）。
    """
    url = (url or "").strip()
    if not url:
        return "error: URL が空です"
    fetcher = fetcher or _default_fetcher
    resolver = resolver or _resolve_ips
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        problem = _destination_problem(current, resolver)
        if problem is not None:
            return f"error: {problem}"
        try:
            resp = fetcher(current, FETCH_TIMEOUT_SECONDS)
        except Exception as e:
            return f"error: ページの取得に失敗しました（{e}）"
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                return "error: リダイレクト先が示されていません"
            current = urljoin(current, location)
            continue
        if not 200 <= resp.status_code < 300:
            return (f"error: ページの取得に失敗しました"
                    f"（HTTP {resp.status_code}）")
        return _extract_body(resp)
    return "error: リダイレクトが多すぎます"
