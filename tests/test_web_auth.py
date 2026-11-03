"""Web UI のログイン（アクセス制御）のテスト。

パスワード未設定では従来どおり認証なしで開くこと、設定時は
未ログインで画面がリダイレクト・API が 401 になること、正しい
パスワードで Cookie が発行されて使えること、ログアウトで
トークンが無効になること、Cookie の属性、連続失敗の制限を
確認する。リダイレクトやヘッダーを正確に見るため、通信は
http.client で行う（urllib はリダイレクトを自動で追うため）。
"""
import http.client
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.web import AuthGate, build_server, render_login_page, render_page

PASSWORD = "secret-pw"


class _EchoModel:
    """受け取った発話をそのまま返す偽モデル。"""

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


@contextmanager
def _running(tmp_path: Path, **cfg_kwargs):
    """偽モデルで Web サーバーを起動し、(host, port) を渡す。"""
    cfg = Config(data_dir=tmp_path, **cfg_kwargs)
    session = Session(cfg, _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def open_server(tmp_path: Path):
    """パスワード未設定（認証なし）のサーバー。"""
    with _running(tmp_path) as addr:
        yield addr


@pytest.fixture()
def auth_server(tmp_path: Path):
    """パスワード設定済み（ログイン有効）のサーバー。"""
    with _running(tmp_path, web_password=PASSWORD) as addr:
        yield addr


def _request(addr, method: str, path: str, body=None,
             headers: dict | None = None) -> tuple[int, dict, str]:
    host, port = addr
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8")
    finally:
        conn.close()


def _get(addr, path: str, cookie: str | None = None):
    headers = {"Cookie": cookie} if cookie else {}
    return _request(addr, "GET", path, headers=headers)


def _post_login(addr, password: str):
    return _request(
        addr, "POST", "/login",
        body=urlencode({"password": password}),
        headers={"Content-Type": "application/x-www-form-urlencoded"})


def _cookie_from(headers: dict) -> str:
    """Set-Cookie から Cookie ヘッダーに使える形（名前=値）を取り出す。"""
    return headers["Set-Cookie"].split(";")[0]


def _login_cookie(addr) -> str:
    status, headers, _ = _post_login(addr, PASSWORD)
    assert status == 302
    return _cookie_from(headers)


# ---------------------------------------------------------- 認証なし

def test_without_password_everything_is_open(open_server):
    status, _, body = _get(open_server, "/")
    assert status == 200
    assert "クマ" in body
    assert "logoutBtn" not in body  # ログアウトボタンは出ない
    status, _, body = _get(open_server, "/api/meta")
    assert status == 200
    assert json.loads(body)["mode"]


def test_login_page_redirects_to_index_when_disabled(open_server):
    status, headers, _ = _get(open_server, "/login")
    assert status == 302
    assert headers["Location"] == "/"


def test_render_page_logout_button_only_when_web_auth():
    assert "logoutBtn" not in render_page("muse")
    page = render_page("muse", web_auth=True)
    assert "logoutBtn" in page and "ログアウト" in page


# ---------------------------------------------------------- ログイン画面

def test_unauthenticated_redirect_and_401(auth_server):
    status, headers, _ = _get(auth_server, "/")
    assert status == 302
    assert headers["Location"] == "/login"
    # API はリダイレクトせず 401
    for path in ("/api/meta", "/api/history", "/api/tenants",
                 "/api/outputs/download?name=x"):
        status, _, body = _get(auth_server, path)
        assert status == 401, path
        assert "ログインが必要です" in body
    # POST の API も 401（会話・ログアウト含む）
    status, _, _ = _request(
        auth_server, "POST", "/api/chat",
        body=json.dumps({"message": "やあ"}),
        headers={"Content-Type": "application/json"})
    assert status == 401
    status, _, _ = _request(auth_server, "POST", "/api/logout")
    assert status == 401


def test_login_page_is_served_with_brand(auth_server):
    status, _, body = _get(auth_server, "/login")
    assert status == 200
    assert 'type="password"' in body
    assert "パスワード" in body
    assert "#E79A3C" in body  # ブランド色


def test_render_login_page():
    page = render_login_page()
    assert 'action="/login"' in page
    assert "パスワードが違います" in render_login_page(
        error="パスワードが違います")


def test_wrong_password_is_rejected(auth_server):
    status, headers, body = _post_login(auth_server, "wrong-pw")
    assert status == 401
    assert "パスワードが違います" in body
    assert "Set-Cookie" not in headers  # Cookie は発行されない


def test_tampered_cookie_is_rejected(auth_server):
    status, _, _ = _get(auth_server, "/api/meta",
                        cookie="kuma_session=not-a-real-token")
    assert status == 401


# ---------------------------------------------------------- ログイン成功後

def test_correct_password_issues_cookie_and_unlocks(auth_server):
    status, headers, _ = _post_login(auth_server, PASSWORD)
    assert status == 302
    assert headers["Location"] == "/"
    set_cookie = headers["Set-Cookie"]
    assert set_cookie.startswith("kuma_session=")
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie
    assert "Path=/" in set_cookie
    assert f"Max-Age={AuthGate.TOKEN_TTL_SECONDS}" in set_cookie
    assert "Secure" not in set_cookie  # 既定では付けない

    cookie = _cookie_from(headers)
    status, _, body = _get(auth_server, "/", cookie=cookie)
    assert status == 200
    assert "logoutBtn" in body  # ヘッダーにログアウトボタンが出る
    status, _, _ = _get(auth_server, "/api/meta", cookie=cookie)
    assert status == 200
    # ログインページは画面へ戻される
    status, headers, _ = _get(auth_server, "/login", cookie=cookie)
    assert status == 302 and headers["Location"] == "/"
    # 会話もできる
    status, _, body = _request(
        auth_server, "POST", "/api/chat",
        body=json.dumps({"message": "こんにちは"}),
        headers={"Content-Type": "application/json", "Cookie": cookie})
    assert status == 200
    assert json.loads(body) == {
        "reply": "返答: こんにちは", "user_rid": 0, "rid": 1}


def test_logout_invalidates_token(auth_server):
    cookie = _login_cookie(auth_server)
    status, headers, body = _request(
        auth_server, "POST", "/api/logout", headers={"Cookie": cookie})
    assert status == 200
    assert json.loads(body) == {"logged_out": True}
    assert "Max-Age=0" in headers["Set-Cookie"]  # Cookie を消す
    # 破棄したトークンはもう使えない
    status, _, _ = _get(auth_server, "/api/meta", cookie=cookie)
    assert status == 401
    status, headers, _ = _get(auth_server, "/", cookie=cookie)
    assert status == 302 and headers["Location"] == "/login"


def test_secure_cookie_only_when_configured(tmp_path: Path):
    with _running(tmp_path, web_password=PASSWORD,
                  web_secure_cookie=True) as addr:
        status, headers, _ = _post_login(addr, PASSWORD)
        assert status == 302
        assert "Secure" in headers["Set-Cookie"]


# ---------------------------------------------------------- 連続失敗の制限

def test_repeated_failures_lock_out(auth_server):
    # 4 回までは間違えても、正しいパスワードで入れる
    for _ in range(AuthGate.MAX_FAILURES - 1):
        status, _, _ = _post_login(auth_server, "wrong-pw")
        assert status == 401
    status, _, _ = _post_login(auth_server, PASSWORD)
    assert status == 302
    # 上限まで連続で間違えると、正しくても受け付けない
    for _ in range(AuthGate.MAX_FAILURES):
        status, _, _ = _post_login(auth_server, "wrong-pw")
        assert status == 401
    status, headers, _ = _post_login(auth_server, PASSWORD)
    assert status == 401
    assert "Set-Cookie" not in headers


# ---------------------------------------------------------- AuthGate 単体

def test_gate_disabled_never_logs_in():
    gate = AuthGate("")
    assert not gate.enabled
    assert gate.try_login("127.0.0.1", "") is None


def test_gate_token_lifecycle():
    gate = AuthGate("pw")
    assert gate.enabled
    assert gate.try_login("127.0.0.1", "bad") is None
    token = gate.try_login("127.0.0.1", "pw")
    assert token
    assert gate.validate(token)
    assert not gate.validate("unknown")
    assert not gate.validate(None)
    gate.revoke(token)
    assert not gate.validate(token)


def test_gate_token_expires(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(AuthGate, "TOKEN_TTL_SECONDS", -1)
    gate = AuthGate("pw")
    token = gate.try_login("127.0.0.1", "pw")
    assert token
    assert not gate.validate(token)  # 発行時点で失効している


def test_gate_lockout_expires():
    gate = AuthGate("pw")
    for _ in range(AuthGate.MAX_FAILURES):
        assert gate.try_login("10.0.0.1", "bad") is None
    assert gate.try_login("10.0.0.1", "pw") is None  # 制限中は通さない
    gate._lockouts["10.0.0.1"] = time.time() - 1  # 制限時間を過ぎたことにする
    assert gate.try_login("10.0.0.1", "pw")


def test_gate_cookie_attributes():
    gate = AuthGate("pw")
    cookie = gate.session_cookie("tok")
    assert cookie.startswith("kuma_session=tok")
    for attr in ("HttpOnly", "SameSite=Lax", "Path=/",
                 f"Max-Age={AuthGate.TOKEN_TTL_SECONDS}"):
        assert attr in cookie
    assert "Secure" not in cookie
    assert "Secure" in AuthGate("pw", secure_cookie=True).session_cookie("t")
    assert "Max-Age=0" in gate.cleared_cookie()


# ---------------------------------------------------------- 設定

def test_config_reads_web_auth_from_env(monkeypatch: pytest.MonkeyPatch,
                                        tmp_path: Path):
    monkeypatch.setenv("KUMA_WEB_PASSWORD", "from-env")
    assert Config(data_dir=tmp_path).web_password == "from-env"
    assert Config(data_dir=tmp_path).web_secure_cookie is False
    monkeypatch.setenv("KUMA_WEB_SECURE_COOKIE", "1")
    assert Config(data_dir=tmp_path).web_secure_cookie is True
    monkeypatch.delenv("KUMA_WEB_PASSWORD")
    assert Config(data_dir=tmp_path).web_password == ""
