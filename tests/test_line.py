"""LINE 接続（line_api.py と Web の /line/webhook 受け口）のテスト。

ネットワークには出ない: 送信 API は差し替え可能な形にしてあり、
偽の API（Reply / Push の記録）が応答する。モデルはスタブ。
署名検証の正否、Webhook の受付（未設定 404・署名不正 400・
正常 200）、許可外ユーザーへの案内、行き先、分割と Reply 失敗時の
Push 切り替え、グループイベントの無視、ログイン必須でも Webhook が
署名で通ることを確認する。
"""
import base64
import hashlib
import hmac
import http.client
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from kuma_agent import web
from kuma_agent.config import Config
from kuma_agent.line_api import (
    MESSAGE_LIMIT,
    LineBot,
    parse_allowed_users,
    session_id_for_user,
    verify_signature,
)
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.web import build_server

SECRET = "test-channel-secret"
ACCESS_TOKEN = "dummy-access-token"


class _EchoModel:
    """受け取った発話をそのまま返す偽モデル。"""

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


class _LongModel:
    """長い応答を返す偽モデル（分割の確認用）。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", self.text)


class _FakeLineApi:
    """送信 API の偽物。Reply / Push を記録し、失敗も再現できる。"""

    def __init__(self, fail_reply: bool = False,
                 fail_push: bool = False) -> None:
        self.replies: list[tuple[str, list[str]]] = []
        self.pushes: list[tuple[str, list[str]]] = []
        self.fail_reply = fail_reply
        self.fail_push = fail_push

    def reply_message(self, reply_token, texts):
        if self.fail_reply:
            raise RuntimeError("Reply に失敗しました（偽物）")
        self.replies.append((reply_token, list(texts)))

    def push_message(self, user_id, texts):
        if self.fail_push:
            raise RuntimeError("Push に失敗しました（偽物）")
        self.pushes.append((user_id, list(texts)))


def _sign(secret: str, body: bytes) -> str:
    """Webhook の署名（X-Line-Signature）を組み立てる。"""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _message_event(user_id: str = "U123", text: str = "こんにちは",
                   reply_token: str = "token-1",
                   source_type: str = "user",
                   message_type: str = "text") -> dict:
    message: dict = {"type": message_type, "id": "1"}
    if message_type == "text":
        message["text"] = text
    source: dict = {"type": source_type}
    if source_type == "user":
        source["userId"] = user_id
    elif source_type == "group":
        source["groupId"] = "G999"
        source["userId"] = user_id
    return {"type": "message", "replyToken": reply_token,
            "source": source, "message": message}


def _body(*events: dict) -> bytes:
    return json.dumps({"destination": "Ubot", "events": list(events)}
                      ).encode("utf-8")


def _cfg(tmp_path: Path, **kwargs) -> Config:
    kwargs.setdefault("line_channel_secret", SECRET)
    kwargs.setdefault("line_channel_access_token", ACCESS_TOKEN)
    return Config(data_dir=tmp_path, **kwargs)


def _records(user_dir: Path, session_id: str = "main") -> list[dict]:
    path = user_dir / "conversations" / f"{session_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- 署名検証

def test_verify_signature_accepts_valid():
    body = _body(_message_event())
    assert verify_signature(SECRET, body, _sign(SECRET, body))


def test_verify_signature_rejects_invalid():
    body = _body(_message_event())
    good = _sign(SECRET, body)
    assert not verify_signature(SECRET, body, _sign("other-secret", body))
    assert not verify_signature(SECRET, body + b" ", good)  # 本文が違う
    assert not verify_signature(SECRET, body, "not-a-signature")
    assert not verify_signature(SECRET, body, "")
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature("", body, good)  # シークレット未設定


# ---------------------------------------------------------------- 通常の会話

def test_message_gets_reply_via_reply_api(tmp_path: Path):
    api = _FakeLineApi()
    bot = LineBot(_cfg(tmp_path, line_allowed_users="U123"), api,
                  model=_EchoModel())
    assert bot.process_body(_body(_message_event())) == 1
    # 返信は replyToken を使う Reply API で届き、Push は使わない
    assert api.replies == [("token-1", ["返答: こんにちは"])]
    assert api.pushes == []
    # 会話は相棒モードの main に記録される（Web UI と共有する履歴）
    records = _records(tmp_path)
    assert [r["content"] for r in records] == ["こんにちは", "返答: こんにちは"]
    assert [r["role"] for r in records] == ["user", "assistant"]


def test_wall_mode_uses_line_session(tmp_path: Path):
    cfg = _cfg(tmp_path, line_allowed_users="U456", mode="wall")
    bot = LineBot(cfg, _FakeLineApi(), model=_EchoModel())
    assert session_id_for_user(cfg, "U456") == "line-U456"
    bot.handle_event(_message_event(user_id="U456", text="やあ"))
    assert bot.api.replies == [("token-1", ["返答: やあ"])]
    # main ではなく line-<user id> の会話に記録される
    assert _records(tmp_path) == []
    records = _records(tmp_path, "line-U456")
    assert [r["content"] for r in records] == ["やあ", "返答: やあ"]


def test_scoped_to_configured_tenant_and_user(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KUMA_LINE_TENANT", "acme")
    monkeypatch.setenv("KUMA_LINE_USER", "bob")
    cfg = Config(data_dir=tmp_path, line_channel_secret=SECRET,
                 line_channel_access_token=ACCESS_TOKEN,
                 line_allowed_users="U7")
    api = _FakeLineApi()
    bot = LineBot(cfg, api, model=_EchoModel())
    bot.process_body(_body(_message_event(user_id="U7", text="hi")))
    assert api.replies == [("token-1", ["返答: hi"])]
    user_dir = tmp_path / "tenants" / "acme" / "users" / "bob"
    assert [r["content"] for r in _records(user_dir)] == ["hi", "返答: hi"]


def test_line_session_has_no_approver(tmp_path: Path):
    # LINE 経路には承認者を付けない（承認が必要な道具は安全側で
    # 却下される前提）。既定の factory が作る Session の approver は
    # None のままであること
    bot = LineBot(_cfg(tmp_path, line_allowed_users="U1"), _FakeLineApi(),
                  model=_EchoModel())
    session = bot._session_factory("main")
    assert session.approver is None


# ---------------------------------------------------------------- 許可リスト

def test_unallowed_user_gets_registration_notice(tmp_path: Path):
    api = _FakeLineApi()
    bot = LineBot(_cfg(tmp_path, line_allowed_users="U999"), api,
                  model=_EchoModel())
    bot.handle_event(_message_event(user_id="U123"))
    # 案内だけが返り、user id が含まれる。会話は処理されない
    assert len(api.replies) == 1
    _token, texts = api.replies[0]
    assert "まだ登録されていません" in texts[0]
    assert "U123" in texts[0]
    assert _records(tmp_path) == []


def test_empty_allowlist_registers_nobody(tmp_path: Path):
    # 許可リストが未設定・空なら、誰の会話も処理しない
    api = _FakeLineApi()
    bot = LineBot(_cfg(tmp_path), api, model=_EchoModel())
    bot.handle_event(_message_event(user_id="U555"))
    assert len(api.replies) == 1
    assert "user id は U555 です" in api.replies[0][1][0]
    assert _records(tmp_path) == []


def test_parse_allowed_users():
    assert parse_allowed_users("U1, U2 ,U3") == {"U1", "U2", "U3"}
    assert parse_allowed_users("") == set()
    assert parse_allowed_users(None) == set()
    assert parse_allowed_users(" , ,") == set()


# ---------------------------------------------------------------- 対象外イベント

def test_group_and_room_events_are_ignored(tmp_path: Path):
    api = _FakeLineApi()
    bot = LineBot(_cfg(tmp_path, line_allowed_users="U123"), api,
                  model=_EchoModel())
    bot.handle_event(_message_event(source_type="group"))
    room = _message_event()
    room["source"] = {"type": "room", "roomId": "R1", "userId": "U123"}
    bot.handle_event(room)
    assert api.replies == [] and api.pushes == []
    assert _records(tmp_path) == []


def test_non_message_events_are_ignored(tmp_path: Path):
    api = _FakeLineApi()
    bot = LineBot(_cfg(tmp_path, line_allowed_users="U123"), api,
                  model=_EchoModel())
    follow = {"type": "follow", "replyToken": "token-9",
              "source": {"type": "user", "userId": "U123"}}
    assert bot.process_body(_body(follow)) == 1
    assert api.replies == [] and api.pushes == []
    assert _records(tmp_path) == []


def test_non_text_message_gets_text_only_notice(tmp_path: Path):
    api = _FakeLineApi()
    bot = LineBot(_cfg(tmp_path, line_allowed_users="U123"), api,
                  model=_EchoModel())
    bot.handle_event(_message_event(message_type="image"))
    bot.handle_event(_message_event(message_type="sticker"))
    assert api.replies == [("token-1", ["今はテキストだけ対応しています"])] * 2
    assert _records(tmp_path) == []  # 会話には入らない


def test_broken_body_processes_nothing(tmp_path: Path):
    bot = LineBot(_cfg(tmp_path, line_allowed_users="U123"),
                  _FakeLineApi(), model=_EchoModel())
    assert bot.process_body(b"not json") == 0
    assert bot.process_body(b"{}") == 0


# ---------------------------------------------------------------- 分割と Push 切り替え

def test_long_reply_first_five_via_reply_rest_via_push(tmp_path: Path):
    api = _FakeLineApi()
    long_text = "あ" * (MESSAGE_LIMIT * 5 + 100)  # 6 通に分かれる長さ
    bot = LineBot(_cfg(tmp_path, line_allowed_users="U123"), api,
                  model=_LongModel(long_text))
    bot.handle_event(_message_event(text="長くして"))
    # 先頭 5 通は Reply、残り 1 通は Push で届く
    assert len(api.replies) == 1
    token, texts = api.replies[0]
    assert token == "token-1"
    assert [len(t) for t in texts] == [MESSAGE_LIMIT] * 5
    assert api.pushes == [("U123", ["あ" * 100])]


def test_reply_failure_falls_back_to_push(tmp_path: Path):
    api = _FakeLineApi(fail_reply=True)
    bot = LineBot(_cfg(tmp_path, line_allowed_users="U123"), api,
                  model=_EchoModel())
    bot.handle_event(_message_event())
    # Reply が失敗したら、同じ文面が Push で届く
    assert api.replies == []
    assert api.pushes == [("U123", ["返答: こんにちは"])]
    # 会話自体は記録されている
    assert len(_records(tmp_path)) == 2


def test_push_failure_does_not_break_processing(tmp_path: Path):
    api = _FakeLineApi(fail_reply=True, fail_push=True)
    bot = LineBot(_cfg(tmp_path, line_allowed_users="U123"), api,
                  model=_EchoModel())
    # 送信が全て失敗しても、process_body は例外にせず件数を返す
    assert bot.process_body(_body(_message_event())) == 1


# ---------------------------------------------------------------- 送信 API 実装

def test_httpx_line_api_reply_and_push():
    import httpx

    from kuma_agent.line_api import HttpxLineApi

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == \
            f"Bearer {ACCESS_TOKEN}"
        payload = json.loads(request.content)
        if request.url.path.endswith("/message/reply"):
            assert payload == {
                "replyToken": "token-1",
                "messages": [{"type": "text", "text": "やあ"}]}
        elif request.url.path.endswith("/message/push"):
            assert payload == {
                "to": "U123",
                "messages": [{"type": "text", "text": "お知らせ"}]}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={})

    api = HttpxLineApi(ACCESS_TOKEN,
                       transport=httpx.MockTransport(handler))
    api.reply_message("token-1", ["やあ"])
    api.push_message("U123", ["お知らせ"])
    assert len(seen) == 2

    # エラー応答は例外にする（文面にトークンを含めない）
    def ng_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad request"})

    ng = HttpxLineApi(ACCESS_TOKEN,
                      transport=httpx.MockTransport(ng_handler))
    try:
        ng.push_message("U123", "x")
    except RuntimeError as exc:
        assert ACCESS_TOKEN not in str(exc)
    else:
        raise AssertionError("例外になるはず")


# ---------------------------------------------------------------- 設定

def test_config_reads_line_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KUMA_LINE_CHANNEL_SECRET", "sec123")
    monkeypatch.setenv("KUMA_LINE_CHANNEL_ACCESS_TOKEN", "tok123")
    monkeypatch.setenv("KUMA_LINE_TENANT", "acme")
    monkeypatch.setenv("KUMA_LINE_USER", "bob")
    monkeypatch.setenv("KUMA_LINE_ALLOWED_USERS", "U1,U2")
    cfg = Config(data_dir=tmp_path)
    assert cfg.line_channel_secret == "sec123"
    assert cfg.line_channel_access_token == "tok123"
    assert cfg.line_tenant == "acme"
    assert cfg.line_user == "bob"
    assert cfg.line_allowed_users == "U1,U2"
    assert cfg.line_proactive is True


def test_config_line_defaults(monkeypatch, tmp_path: Path):
    for name in ("KUMA_LINE_CHANNEL_SECRET",
                 "KUMA_LINE_CHANNEL_ACCESS_TOKEN", "KUMA_LINE_TENANT",
                 "KUMA_LINE_USER", "KUMA_LINE_ALLOWED_USERS",
                 "KUMA_LINE_PROACTIVE"):
        monkeypatch.delenv(name, raising=False)
    cfg = Config(data_dir=tmp_path)
    assert cfg.line_channel_secret == ""
    assert cfg.line_channel_access_token == ""
    assert cfg.line_tenant == "default"
    assert cfg.line_user == "default"
    assert cfg.line_allowed_users == ""
    assert cfg.line_proactive is True
    monkeypatch.setenv("KUMA_LINE_PROACTIVE", "0")
    assert Config(data_dir=tmp_path).line_proactive is False


# ---------------------------------------------------------------- Web の受け口

@contextmanager
def _running(tmp_path: Path, monkeypatch, fake_api=None, **cfg_kwargs):
    """偽モデル（と偽の LINE API）で Web サーバーを起動する。"""
    if fake_api is not None:
        monkeypatch.setattr(web, "HttpxLineApi",
                            lambda token, **kw: fake_api)
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


def _post_webhook(addr, body: bytes, signature: str | None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Line-Signature"] = signature
    return _request(addr, "POST", "/line/webhook", body=body,
                    headers=headers)


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _line_cfg_kwargs(**extra) -> dict:
    kwargs = {"line_channel_secret": SECRET,
              "line_channel_access_token": ACCESS_TOKEN,
              "line_allowed_users": "U123"}
    kwargs.update(extra)
    return kwargs


def test_webhook_not_configured_is_404(tmp_path: Path, monkeypatch):
    # LINE の設定が無ければ受け口自体が無い
    with _running(tmp_path, monkeypatch) as addr:
        body = _body(_message_event())
        status, _, _ = _post_webhook(addr, body, _sign(SECRET, body))
        assert status == 404
    # 片方だけの設定でも有効にならない
    with _running(tmp_path, monkeypatch,
                  line_channel_secret=SECRET) as addr:
        body = _body(_message_event())
        status, _, _ = _post_webhook(addr, body, _sign(SECRET, body))
        assert status == 404


def test_webhook_bad_signature_is_400(tmp_path: Path, monkeypatch):
    api = _FakeLineApi()
    with _running(tmp_path, monkeypatch, fake_api=api,
                  **_line_cfg_kwargs()) as addr:
        body = _body(_message_event())
        status, _, _ = _post_webhook(addr, body, _sign("wrong", body))
        assert status == 400
        status, _, _ = _post_webhook(addr, body, None)  # 署名なし
        assert status == 400
    # 署名が合わなければ処理も返信もされない
    assert api.replies == [] and api.pushes == []
    assert _records(tmp_path) == []


def test_webhook_processes_in_background(tmp_path: Path, monkeypatch):
    api = _FakeLineApi()
    with _running(tmp_path, monkeypatch, fake_api=api,
                  **_line_cfg_kwargs()) as addr:
        body = _body(_message_event())
        status, _, resp_body = _post_webhook(addr, body, _sign(SECRET, body))
        assert status == 200
        assert json.loads(resp_body) == {}
        # 処理は裏側のスレッドで行われ、返信と記録が付く
        assert _wait_for(lambda: len(api.replies) == 1)
        assert api.replies == [("token-1", ["返答: こんにちは"])]
        assert _wait_for(lambda: len(_records(tmp_path)) == 2)


def test_webhook_bypasses_login_gate(tmp_path: Path, monkeypatch):
    # ログイン必須の設定でも、Webhook は Cookie なしで署名だけで
    # 通る（LINE のサーバーは Cookie を持たないため）。一方で通常の
    # API は従来どおり 401 のまま
    api = _FakeLineApi()
    with _running(tmp_path, monkeypatch, fake_api=api,
                  web_password="secret-pw",
                  **_line_cfg_kwargs()) as addr:
        status, _, _ = _request(addr, "GET", "/api/meta")
        assert status == 401
        body = _body(_message_event())
        status, _, _ = _post_webhook(addr, body, _sign(SECRET, body))
        assert status == 200
        assert _wait_for(lambda: len(api.replies) == 1)
        # 署名が合わなければ、ログイン状態にかかわらず 400
        status, _, _ = _post_webhook(addr, body, _sign("wrong", body))
        assert status == 400
