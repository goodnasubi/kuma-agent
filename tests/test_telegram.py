"""Telegram 接続（telegram.py）のテスト。

ネットワークには出ない: Bot API は差し替え可能な形にしてあり、
偽の API（get_updates / send_message の記録）が応答する。モデルは
スタブ。通常メッセージへの応答、許可外チャットの案内、4096 字の
分割、オフセットの保存と再開、テキスト以外への返答を確認する。
"""
import json
import threading
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.telegram import (
    MESSAGE_LIMIT,
    TelegramPoller,
    load_offset,
    parse_allowed_chats,
    session_id_for_chat,
    split_message,
)


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


class _FakeApi:
    """Bot API の偽物。バッチ単位で更新を返し、送信を記録する。"""

    def __init__(self, batches: list[list[dict]] | None = None) -> None:
        self.batches = list(batches or [])
        self.sent: list[tuple[int, str]] = []
        self.offsets: list[int | None] = []

    def get_updates(self, offset, timeout):
        self.offsets.append(offset)
        if self.batches:
            return self.batches.pop(0)
        return []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def _update(update_id: int, chat_id: int, **message_extra) -> dict:
    message = {"message_id": update_id, "chat": {"id": chat_id}}
    message.update(message_extra)
    return {"update_id": update_id, "message": message}


def _cfg(tmp_path: Path, **kwargs) -> Config:
    kwargs.setdefault("telegram_bot_token", "dummy-token")
    return Config(data_dir=tmp_path, **kwargs)


def _records(user_dir: Path, session_id: str = "main") -> list[dict]:
    path = user_dir / "conversations" / f"{session_id}.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- 通常の会話

def test_message_gets_stub_model_reply(tmp_path: Path):
    api = _FakeApi([[_update(10, 123, text="こんにちは")]])
    poller = TelegramPoller(
        _cfg(tmp_path, telegram_allowed_chats="123"), api,
        model=_EchoModel())
    assert poller.poll_once() == 1
    assert api.sent == [(123, "返答: こんにちは")]
    # 会話は相棒モードの main に記録される（Web UI と共有する履歴）
    records = _records(tmp_path)
    assert [r["content"] for r in records] == ["こんにちは", "返答: こんにちは"]
    assert [r["role"] for r in records] == ["user", "assistant"]


def test_wall_mode_uses_telegram_session(tmp_path: Path):
    cfg = _cfg(tmp_path, telegram_allowed_chats="456", mode="wall")
    poller = TelegramPoller(cfg, _FakeApi(), model=_EchoModel())
    assert session_id_for_chat(cfg, 456) == "telegram-456"
    poller.handle_update(_update(1, 456, text="やあ"))
    assert poller.api.sent == [(456, "返答: やあ")]
    # main ではなく telegram-<chat id> の会話に記録される
    assert _records(tmp_path) == []
    records = _records(tmp_path, "telegram-456")
    assert [r["content"] for r in records] == ["やあ", "返答: やあ"]


def test_scoped_to_configured_tenant_and_user(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KUMA_TELEGRAM_TENANT", "acme")
    monkeypatch.setenv("KUMA_TELEGRAM_USER", "bob")
    cfg = Config(data_dir=tmp_path, telegram_bot_token="dummy",
                 telegram_allowed_chats="7")
    api = _FakeApi([[_update(1, 7, text="hi")]])
    poller = TelegramPoller(cfg, api, model=_EchoModel())
    poller.poll_once()
    assert api.sent == [(7, "返答: hi")]
    user_dir = tmp_path / "tenants" / "acme" / "users" / "bob"
    assert [r["content"] for r in _records(user_dir)] == ["hi", "返答: hi"]


def test_telegram_session_has_no_approver(tmp_path: Path):
    # Telegram 経路には承認者を付けない（承認が必要な道具は安全側で
    # 却下される前提）。既定の factory が作る Session の approver は
    # None のままであること
    poller = TelegramPoller(
        _cfg(tmp_path, telegram_allowed_chats="1"), _FakeApi(),
        model=_EchoModel())
    session = poller._session_factory("main")
    assert session.approver is None


# ---------------------------------------------------------------- 許可リスト

def test_unallowed_chat_gets_registration_notice(tmp_path: Path):
    api = _FakeApi()
    poller = TelegramPoller(
        _cfg(tmp_path, telegram_allowed_chats="999"), api,
        model=_EchoModel())
    poller.handle_update(_update(1, 123, text="こんにちは"))
    # 案内だけが返り、chat id が含まれる。会話は処理されない
    assert len(api.sent) == 1
    chat_id, text = api.sent[0]
    assert chat_id == 123
    assert "まだ登録されていません" in text
    assert "123" in text
    assert _records(tmp_path) == []


def test_empty_allowlist_registers_nobody(tmp_path: Path):
    # 許可リストが未設定・空なら、誰の会話も処理しない
    for raw in ("", None):
        api = _FakeApi()
        cfg = _cfg(tmp_path)
        if raw is not None:
            cfg.telegram_allowed_chats = raw
        else:
            cfg.telegram_allowed_chats = ""
        poller = TelegramPoller(cfg, api, model=_EchoModel())
        poller.handle_update(_update(1, 555, text="やあ"))
        assert len(api.sent) == 1
        assert "chat id は 555 です" in api.sent[0][1]
        assert _records(tmp_path) == []


def test_parse_allowed_chats():
    assert parse_allowed_chats("123, -456 , 789") == {123, -456, 789}
    assert parse_allowed_chats("") == set()
    assert parse_allowed_chats(None) == set()
    assert parse_allowed_chats("abc, 12x, 42") == {42}


# ---------------------------------------------------------------- 分割

def test_split_message_by_limit():
    assert split_message("短い文") == ["短い文"]
    chunks = split_message("あ" * (MESSAGE_LIMIT * 2 + 100))
    assert [len(c) for c in chunks] == [MESSAGE_LIMIT, MESSAGE_LIMIT, 100]
    assert "".join(chunks) == "あ" * (MESSAGE_LIMIT * 2 + 100)


def test_split_message_prefers_newlines():
    text = "行1\n" + "あ" * 100 + "\n" + "い" * (MESSAGE_LIMIT + 10)
    chunks = split_message(text)
    assert all(len(c) <= MESSAGE_LIMIT for c in chunks)
    assert chunks[0].startswith("行1\n")


def test_long_reply_is_sent_in_chunks(tmp_path: Path):
    api = _FakeApi([[_update(1, 123, text="長くして")]])
    poller = TelegramPoller(
        _cfg(tmp_path, telegram_allowed_chats="123"), api,
        model=_LongModel("あ" * (MESSAGE_LIMIT + 500)))
    poller.poll_once()
    assert [chat for chat, _text in api.sent] == [123, 123]
    assert len(api.sent[0][1]) == MESSAGE_LIMIT
    assert len(api.sent[1][1]) == 500


# ---------------------------------------------------------------- オフセット

def test_offset_saved_and_resumed(tmp_path: Path):
    api = _FakeApi([[_update(10, 123, text="a"), _update(11, 123, text="b")]])
    cfg = _cfg(tmp_path, telegram_allowed_chats="123")
    poller = TelegramPoller(cfg, api, model=_EchoModel())
    assert poller.offset is None
    poller.poll_once()
    # 最後の update_id の次が保存され、ファイルにも残る
    assert poller.offset == 12
    state = json.loads(
        (tmp_path / "telegram_state.json").read_text(encoding="utf-8"))
    assert state == {"offset": 12}
    assert load_offset(tmp_path) == 12

    # 再起動（新しい Poller）では保存済み位置から取りにいき、
    # 処理済みの更新は再処理しない
    api2 = _FakeApi([[]])
    poller2 = TelegramPoller(cfg, api2, model=_EchoModel())
    assert poller2.offset == 12
    poller2.poll_once()
    assert api2.offsets == [12]
    assert api2.sent == []
    assert len(_records(tmp_path)) == 4  # a/b の往復 2 組のまま


def test_offset_advances_for_non_message_updates(tmp_path: Path):
    api = _FakeApi([[{"update_id": 42, "edited_message": {"text": "x"}}]])
    poller = TelegramPoller(
        _cfg(tmp_path, telegram_allowed_chats="123"), api,
        model=_EchoModel())
    assert poller.poll_once() == 1
    assert api.sent == []
    assert poller.offset == 43


# ---------------------------------------------------------------- テキスト以外

def test_non_text_message_gets_text_only_notice(tmp_path: Path):
    api = _FakeApi()
    poller = TelegramPoller(
        _cfg(tmp_path, telegram_allowed_chats="123"), api,
        model=_EchoModel())
    poller.handle_update(_update(1, 123, photo=[{"file_id": "x"}]))
    assert api.sent == [(123, "今はテキストだけ対応しています")]
    assert _records(tmp_path) == []  # 会話には入らない


# ---------------------------------------------------------------- 再試行

def test_run_forever_retries_after_failure(tmp_path: Path):
    attempts = {"count": 0}
    stopped = threading.Event()

    class _FlakyApi(_FakeApi):
        def get_updates(self, offset, timeout):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise OSError("network down")
            stopped.set()
            return []

    poller = TelegramPoller(
        _cfg(tmp_path, telegram_allowed_chats="1"),
        _FlakyApi(), model=_EchoModel())
    # 失敗で終わらず再試行し、stop_event が立ったら戻ること
    poller.run_forever(stop_event=stopped, retry_delay=0.01,
                       poll_timeout=1)
    assert attempts["count"] == 2


# ---------------------------------------------------------------- Bot API 実装

def test_httpx_api_get_updates_and_send_message():
    import httpx

    from kuma_agent.telegram import HttpxTelegramApi

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 5, "message": {"chat": {"id": 1},
                                             "text": "hi"}}]})
        if request.url.path.endswith("/sendMessage"):
            assert json.loads(request.content) == {"chat_id": 1, "text": "やあ"}
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(404, json={"ok": False})

    api = HttpxTelegramApi("dummy-token",
                           transport=httpx.MockTransport(handler))
    updates = api.get_updates(3, 30)
    assert updates[0]["update_id"] == 5
    # オフセットとポーリングの待ち時間がクエリに載る
    assert seen[0].url.params["offset"] == "3"
    assert seen[0].url.params["timeout"] == "30"
    api.send_message(1, "やあ")

    # ok でない応答は例外にする（文面にトークンを含めない）
    def ng_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "bad"})

    ng = HttpxTelegramApi("dummy-token",
                          transport=httpx.MockTransport(ng_handler))
    try:
        ng.send_message(1, "x")
    except RuntimeError as exc:
        assert "dummy-token" not in str(exc)
    else:
        raise AssertionError("例外になるはず")


# ---------------------------------------------------------------- 設定

def test_config_reads_telegram_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KUMA_TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("KUMA_TELEGRAM_TENANT", "acme")
    monkeypatch.setenv("KUMA_TELEGRAM_USER", "bob")
    monkeypatch.setenv("KUMA_TELEGRAM_ALLOWED_CHATS", "1,2")
    cfg = Config(data_dir=tmp_path)
    assert cfg.telegram_bot_token == "tok123"
    assert cfg.telegram_tenant == "acme"
    assert cfg.telegram_user == "bob"
    assert cfg.telegram_allowed_chats == "1,2"


def test_config_telegram_defaults(monkeypatch, tmp_path: Path):
    for name in ("KUMA_TELEGRAM_BOT_TOKEN", "KUMA_TELEGRAM_TENANT",
                 "KUMA_TELEGRAM_USER", "KUMA_TELEGRAM_ALLOWED_CHATS"):
        monkeypatch.delenv(name, raising=False)
    cfg = Config(data_dir=tmp_path)
    assert cfg.telegram_bot_token == ""
    assert cfg.telegram_tenant == "default"
    assert cfg.telegram_user == "default"
    assert cfg.telegram_allowed_chats == ""


def test_start_polling_thread_requires_token(tmp_path: Path):
    from kuma_agent.telegram import start_polling_thread

    # トークン未設定ならスレッドは始まらない（機能は無効）
    assert start_polling_thread(_cfg(tmp_path, telegram_bot_token=""),
                                _EchoModel()) is None
