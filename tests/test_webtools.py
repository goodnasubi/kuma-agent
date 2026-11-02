"""Web 検索とページ取得の道具（web_search / fetch_page）のテスト。

ネットワークには出ない: HTTP 取得（fetcher）とホスト名解決
（resolver）は差し替え可能にしてあり、DuckDuckGo 形式の HTML と
本文ページの HTML を偽の応答として差し込む。検索結果の抽出
（uddg の戻し含む）と 0 件、本文抽出（script 除去・上限で切り）、
プライベート宛先の拒否と許可、道具としての配線を確認する。
"""
from pathlib import Path

import kuma_agent.tools as tools_module
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.tools import activity_label
from kuma_agent.webtools import FetchedResponse, fetch_page, web_search

PUBLIC_IPS = ["93.184.216.34"]


def _public_resolver(host: str) -> list[str]:
    return list(PUBLIC_IPS)


def _fetcher_returning(resp: FetchedResponse, calls: list | None = None):
    def fetch(url: str, timeout: float) -> FetchedResponse:
        if calls is not None:
            calls.append(url)
        return resp
    return fetch


# DuckDuckGo HTML 版の結果ページを模した HTML。1 件目は uddg 形式の
# リダイレクト URL、2 件目は直接の URL
DDG_HTML = """\
<html><body>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage1%3Fa%3D1&amp;rut=deadbeef">Example Page 1</a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">最初の<b>要約</b>です</a>
</div>
<div class="result results_links results_links_deep web-result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="https://example.org/direct">Direct Page</a>
  </h2>
  <a class="result__snippet">2 件目の要約</a>
</div>
</body></html>
"""

PAGE_HTML = """\
<html>
<head><title>ページの題名</title><style>body { color: red; }</style></head>
<body>
<script>var secret = "SCRIPT_MARKER";</script>
<h1>見出しです</h1>
<p>1 つ目の段落です。</p>
<p>2 つ目の段落です。<a href="https://example.com/">リンク</a>を含みます。</p>
<style>.x { display: none; }</style>
</body>
</html>
"""


# ---------------------------------------------------------------- 検索

def test_search_extracts_results_and_unwraps_uddg():
    calls: list[str] = []
    fetcher = _fetcher_returning(
        FetchedResponse(200, {"content-type": "text/html"}, DDG_HTML),
        calls)
    result = web_search("kuma agent", fetcher=fetcher)
    # 既定の検索先（DuckDuckGo HTML 版）に、符号化したクエリで聞く
    assert calls and "html.duckduckgo.com/html/" in calls[0]
    assert "q=kuma+agent" in calls[0]
    assert "1. Example Page 1" in result
    # uddg は実際の URL に戻され、クエリ文字列まで復元される
    assert "https://example.com/page1?a=1" in result
    assert "最初の要約です" in result
    assert "2. Direct Page" in result
    assert "https://example.org/direct" in result
    assert "2 件目の要約" in result


def test_search_limit_and_template_override(monkeypatch):
    fetcher = _fetcher_returning(
        FetchedResponse(200, {"content-type": "text/html"}, DDG_HTML))
    result = web_search("x", limit=1, fetcher=fetcher)
    assert "Example Page 1" in result
    assert "Direct Page" not in result

    calls: list[str] = []
    monkeypatch.setenv(
        "KUMA_SEARCH_URL", "https://search.example.jp/find?q={query}")
    web_search("日本語 テスト", fetcher=_fetcher_returning(
        FetchedResponse(200, {}, DDG_HTML), calls))
    assert calls[0].startswith("https://search.example.jp/find?q=")
    assert "%E6%97%A5%E6%9C%AC%E8%AA%9E" in calls[0]


def test_search_no_results():
    fetcher = _fetcher_returning(
        FetchedResponse(200, {}, "<html><body>なし</body></html>"))
    assert web_search("zzz", fetcher=fetcher) == "検索結果が見つかりませんでした"


def test_search_failures_become_errors():
    fetcher = _fetcher_returning(FetchedResponse(503, {}, ""))
    assert web_search("x", fetcher=fetcher).startswith("error:")

    def boom(url: str, timeout: float) -> FetchedResponse:
        raise OSError("network unreachable")
    assert web_search("x", fetcher=boom).startswith("error:")
    assert web_search("  ", fetcher=fetcher).startswith("error:")


# ---------------------------------------------------------------- ページ取得

def test_fetch_extracts_body_without_script_or_style():
    fetcher = _fetcher_returning(FetchedResponse(
        200, {"content-type": "text/html; charset=utf-8"}, PAGE_HTML))
    body = fetch_page("https://example.com/page",
                      fetcher=fetcher, resolver=_public_resolver)
    assert "見出しです" in body
    assert "1 つ目の段落です。" in body
    assert "2 つ目の段落です。リンクを含みます。" in body
    # script / style の中身と head の題名は本文に入らない
    assert "SCRIPT_MARKER" not in body
    assert "color: red" not in body
    assert "ページの題名" not in body


def test_fetch_plain_text_is_used_as_is():
    fetcher = _fetcher_returning(FetchedResponse(
        200, {"content-type": "text/plain"}, "ただのテキスト\n2 行目"))
    body = fetch_page("https://example.com/note.txt",
                      fetcher=fetcher, resolver=_public_resolver)
    assert body == "ただのテキスト\n2 行目"


def test_fetch_truncates_long_body():
    long_html = "<html><body><p>" + "あ" * 9000 + "</p></body></html>"
    fetcher = _fetcher_returning(FetchedResponse(
        200, {"content-type": "text/html"}, long_html))
    body = fetch_page("https://example.com/long",
                      fetcher=fetcher, resolver=_public_resolver)
    assert "先頭 8000 文字で切りました" in body
    assert len(body) < 9000


def test_fetch_rejects_private_destinations():
    calls: list[str] = []
    fetcher = _fetcher_returning(
        FetchedResponse(200, {"content-type": "text/plain"}, "中身"), calls)
    # 名前解決の先がプライベートなら、取得自体をしない
    result = fetch_page("https://internal.example.jp/",
                        fetcher=fetcher,
                        resolver=lambda host: ["192.168.1.10"])
    assert result.startswith("error:")
    assert calls == []
    # IP リテラル（ループバック・プライベート）も拒否する
    for url in ("http://127.0.0.1:8765/", "http://10.0.0.5/",
                "http://[::1]/"):
        result = fetch_page(url, fetcher=fetcher,
                            resolver=_public_resolver)
        assert result.startswith("error:"), url
    assert calls == []


def test_fetch_allows_private_when_env_set(monkeypatch):
    monkeypatch.setenv("KUMA_FETCH_ALLOW_PRIVATE", "1")
    fetcher = _fetcher_returning(FetchedResponse(
        200, {"content-type": "text/plain"}, "ローカルの中身"))
    body = fetch_page("http://127.0.0.1:8765/",
                      fetcher=fetcher, resolver=_public_resolver)
    assert body == "ローカルの中身"


def test_fetch_rejects_non_http_and_bad_responses():
    fetcher = _fetcher_returning(
        FetchedResponse(200, {"content-type": "text/plain"}, "x"))
    for url in ("file:///etc/passwd", "ftp://example.com/x", ""):
        result = fetch_page(url, fetcher=fetcher,
                            resolver=_public_resolver)
        assert result.startswith("error:"), url
    # HTTP エラーと、解決できないホスト
    fetcher404 = _fetcher_returning(FetchedResponse(404, {}, "not found"))
    assert fetch_page("https://example.com/missing", fetcher=fetcher404,
                      resolver=_public_resolver).startswith("error:")

    def bad_resolver(host: str) -> list[str]:
        raise OSError("dns failed")
    assert fetch_page("https://nope.example.jp/", fetcher=fetcher,
                      resolver=bad_resolver).startswith("error:")
    # 非対応の形式
    fetcher_json = _fetcher_returning(FetchedResponse(
        200, {"content-type": "application/json"}, "{}"))
    assert fetch_page("https://example.com/api", fetcher=fetcher_json,
                      resolver=_public_resolver).startswith("error:")


def test_fetch_follows_redirects_but_rechecks_destination():
    pages = {
        "https://example.com/start": FetchedResponse(
            302, {"location": "/next"}, ""),
        "https://example.com/next": FetchedResponse(
            200, {"content-type": "text/plain"}, "飛んだ先の中身"),
    }

    def fetcher(url: str, timeout: float) -> FetchedResponse:
        return pages[url]

    body = fetch_page("https://example.com/start",
                      fetcher=fetcher, resolver=_public_resolver)
    assert body == "飛んだ先の中身"

    # プライベート宛先へのリダイレクトは追わない
    evil = {
        "https://example.com/start": FetchedResponse(
            302, {"location": "http://127.0.0.1:8765/admin"}, ""),
    }
    result = fetch_page(
        "https://example.com/start",
        fetcher=lambda url, timeout: evil[url],
        resolver=_public_resolver)
    assert result.startswith("error:")


# ---------------------------------------------------------------- 道具の配線

def test_activity_labels_for_web_tools():
    assert activity_label("web_search") == "Webを検索中"
    assert activity_label("fetch_page") == "ページを読んでいます"


def _patch_webtools(monkeypatch) -> dict:
    seen: dict = {}

    def fake_search(query: str, limit: int = 5) -> str:
        seen["query"] = query
        return "1. 例のページ\nhttps://example.com/\n要約です"

    def fake_fetch(url: str) -> str:
        seen["url"] = url
        return "取得した本文です"

    monkeypatch.setattr(tools_module, "web_search", fake_search)
    monkeypatch.setattr(tools_module, "fetch_page", fake_fetch)
    return seen


def test_tools_call_through(tmp_path: Path, monkeypatch):
    seen = _patch_webtools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    box = Config(data_dir=tmp_path)
    session = Session(box, _PlainModel())
    names = [t["name"] for t in session.tools.schemas()]
    assert "web_search" in names
    assert "fetch_page" in names
    result = session.tools.call("web_search", {"query": "調べ物"})
    assert "例のページ" in result
    assert seen["query"] == "調べ物"
    result = session.tools.call(
        "fetch_page", {"url": "https://example.com/"})
    assert result == "取得した本文です"
    assert seen["url"] == "https://example.com/"


def test_web_tools_offered_in_wall_mode_too(tmp_path: Path, monkeypatch):
    _patch_webtools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path, mode="wall")
    session = Session(cfg, _PlainModel())
    names = [t["name"] for t in session.tools.schemas()]
    # Web 道具は基本道具の仲間なのでモードを問わず出る
    assert "web_search" in names
    assert "fetch_page" in names


class _PlainModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "はい")


class _FetchModel:
    """1 回目で fetch_page を呼び、2 回目で最終応答を返すスタブ。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": "fetch_page",
                 "args": {"url": "https://example.com/"}}])
        return Message("assistant", "読みました")


def test_session_model_can_call_fetch_page(tmp_path: Path, monkeypatch):
    _patch_webtools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _FetchModel())
    assert session.say("このページを読んで") == "読みました"
    tool_results = [m.content for m in session.history if m.role == "tool"]
    assert tool_results == ["取得した本文です"]
