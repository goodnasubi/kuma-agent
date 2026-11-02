"""能動発信の LINE 配信（line_api.build_proactive_notifier）のテスト。

ネットワークには出ない: 送信 API は偽物（Push の記録、失敗の再現）
に差し替える。巡回（proactive）の投稿が、LINE の行き先のテナント
／ユーザーについてだけ許可リストの全ユーザーへ Push されること、
無効化・未設定では送られないこと、送信失敗でも会話への投稿は
残ること、Telegram の通知とまとめられる（combine_notifiers）
ことを確認する。Push は月間の通数に数えるため、無効化スイッチ
（KUMA_LINE_PROACTIVE=0）の確認も含む。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.line_api import MESSAGE_LIMIT, build_proactive_notifier
from kuma_agent.llm import Message
from kuma_agent.proactive import check_and_post, combine_notifiers, run_sweep
from kuma_agent.session import Session

NOW = datetime(2026, 11, 2, 9, 0, 0)


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


class _FakeLineApi:
    """送信 API の偽物。Push を記録し、fail=True なら例外を出す。"""

    def __init__(self, fail: bool = False) -> None:
        self.pushes: list[tuple[str, list[str]]] = []
        self.replies: list[tuple[str, list[str]]] = []
        self.fail = fail

    def reply_message(self, reply_token, texts):
        self.replies.append((reply_token, list(texts)))

    def push_message(self, user_id, texts):
        if self.fail:
            raise RuntimeError("送信に失敗しました（偽物）")
        self.pushes.append((user_id, list(texts)))


def _cfg(tmp_path: Path, **kwargs) -> Config:
    kwargs.setdefault("line_channel_secret", "dummy-secret")
    kwargs.setdefault("line_channel_access_token", "dummy-token")
    kwargs.setdefault("line_allowed_users", "U123,U456")
    return Config(data_dir=tmp_path, **kwargs)


def _write_tasks(user_dir: Path, tasks: list[dict]) -> Path:
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / "tasks.json"
    path.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
    return path


def _task(task_id: str, title: str, created: datetime,
          remind_at: str = "") -> dict:
    stamp = created.isoformat(timespec="seconds")
    return {"id": task_id, "title": title, "status": "open", "memo": "",
            "created_at": stamp, "updated_at": stamp,
            "remind_at": remind_at, "reminded_at": ""}


def _records(user_dir: Path) -> list[dict]:
    path = user_dir / "conversations" / "main.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- 日報・リマインダー

def test_daily_report_pushed_to_all_allowed_users(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = _cfg(tmp_path)
    api = _FakeLineApi()
    notify = build_proactive_notifier(cfg, api=api)
    assert notify is not None

    text = check_and_post(cfg, now=NOW, notify=notify)
    assert text is not None and "買い物" in text
    # 許可リストの全ユーザーへ、会話へ投稿したのと同じ文面が
    # Push で届く（Reply は Webhook の返信用なので使わない）
    assert api.pushes == [("U123", [text]), ("U456", [text])]
    assert api.replies == []
    # 会話への投稿も従来どおり残る
    assert [r["content"] for r in _records(tmp_path)] == [text]


def test_reminder_pushed(tmp_path: Path):
    due = (NOW - timedelta(minutes=30)).isoformat(timespec="seconds")
    _write_tasks(tmp_path, [_task("1", "薬を飲む", NOW, remind_at=due)])
    (tmp_path / "proactive_state.json").write_text(
        json.dumps({"last_posted_on": "2026-11-02"}), encoding="utf-8")
    cfg = _cfg(tmp_path)
    api = _FakeLineApi()
    notify = build_proactive_notifier(cfg, api=api)

    text = check_and_post(cfg, now=NOW, notify=notify)
    assert text is not None and "薬を飲む" in text
    assert api.pushes == [("U123", [text]), ("U456", [text])]


def test_no_post_no_delivery(tmp_path: Path):
    cfg = _cfg(tmp_path)
    api = _FakeLineApi()
    notify = build_proactive_notifier(cfg, api=api)
    assert check_and_post(cfg, now=NOW, notify=notify) is None
    assert api.pushes == []


# ---------------------------------------------------------------- 届け先の絞り込み

def test_other_user_not_delivered(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "自分の用事", NOW)])
    _write_tasks(tmp_path / "users" / "bob",
                 [_task("1", "bobの用事", NOW)])
    cfg = _cfg(tmp_path)  # line_user は既定の default
    api = _FakeLineApi()
    notify = build_proactive_notifier(cfg, api=api)

    posted = run_sweep(cfg, now=NOW, notify=notify)
    assert {(t, u) for t, u, _text in posted} == {
        ("default", "default"), ("default", "bob")}
    assert len(_records(tmp_path / "users" / "bob")) == 1
    own = [text for _t, u, text in posted if u == "default"][0]
    assert api.pushes == [("U123", [own]), ("U456", [own])]


def test_notifier_ignores_other_tenant_and_user(tmp_path: Path):
    cfg = _cfg(tmp_path, line_tenant="acme", line_user="alice")
    api = _FakeLineApi()
    notify = build_proactive_notifier(cfg, api=api)
    assert notify is not None
    notify("default", "alice", "違うテナント")
    notify("acme", "bob", "違うユーザー")
    assert api.pushes == []
    notify("acme", "alice", "対象の投稿")
    assert api.pushes == [("U123", ["対象の投稿"]), ("U456", ["対象の投稿"])]


# ---------------------------------------------------------------- 無効化・未設定

def test_proactive_disabled_by_config(tmp_path: Path):
    cfg = _cfg(tmp_path, line_proactive=False)
    assert build_proactive_notifier(cfg, api=_FakeLineApi()) is None


def test_proactive_disabled_by_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KUMA_LINE_PROACTIVE", "0")
    cfg = Config(data_dir=tmp_path)
    assert cfg.line_proactive is False
    monkeypatch.setenv("KUMA_LINE_PROACTIVE", "1")
    assert Config(data_dir=tmp_path).line_proactive is True
    monkeypatch.delenv("KUMA_LINE_PROACTIVE")
    assert Config(data_dir=tmp_path).line_proactive is True


def test_no_token_no_notifier(tmp_path: Path):
    cfg = _cfg(tmp_path, line_channel_access_token="")
    assert build_proactive_notifier(cfg, api=_FakeLineApi()) is None


def test_empty_allowed_users_no_notifier(tmp_path: Path):
    cfg = _cfg(tmp_path, line_allowed_users="")
    assert build_proactive_notifier(cfg, api=_FakeLineApi()) is None
    cfg = _cfg(tmp_path, line_allowed_users="  , ,")
    assert build_proactive_notifier(cfg, api=_FakeLineApi()) is None


def test_disabled_means_no_delivery(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = _cfg(tmp_path, line_proactive=False)
    notify = build_proactive_notifier(cfg, api=_FakeLineApi())
    assert notify is None
    text = check_and_post(cfg, now=NOW, notify=notify)
    assert text is not None
    assert [r["content"] for r in _records(tmp_path)] == [text]


# ---------------------------------------------------------------- 失敗時の扱い

def test_send_failure_still_posts_to_conversation(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = _cfg(tmp_path)
    api = _FakeLineApi(fail=True)
    notify = build_proactive_notifier(cfg, api=api)

    # 送信が全て失敗しても例外にならず、投稿は会話記録に残る
    text = check_and_post(cfg, now=NOW, notify=notify)
    assert text is not None
    assert [r["content"] for r in _records(tmp_path)] == [text]
    assert api.pushes == []  # 失敗したので記録も残らない（再送もしない）


# ---------------------------------------------------------------- 分割

def test_long_message_split_into_pushes(tmp_path: Path):
    cfg = _cfg(tmp_path, line_allowed_users="U123")
    api = _FakeLineApi()
    notify = build_proactive_notifier(cfg, api=api)
    assert notify is not None
    long_text = "あ" * (MESSAGE_LIMIT * 5 + 100)  # 6 通分
    notify("default", "default", long_text)
    # Push は 1 回最大 5 通: 5 通 + 1 通の 2 回に分かれる
    assert [len(texts) for _uid, texts in api.pushes] == [5, 1]
    assert all(len(t) <= MESSAGE_LIMIT
               for _uid, texts in api.pushes for t in texts)
    joined = "".join(t for _uid, texts in api.pushes for t in texts)
    assert joined == long_text


# ---------------------------------------------------------------- 通知の組み合わせ

def test_combine_notifiers_calls_all():
    calls: list[tuple[str, str]] = []

    def first(tenant, user, text):
        calls.append(("first", text))

    def second(tenant, user, text):
        calls.append(("second", text))

    combined = combine_notifiers(first, None, second)
    assert combined is not None
    combined("default", "default", "お知らせ")
    assert calls == [("first", "お知らせ"), ("second", "お知らせ")]
    # 1 つだけならそれ自体、無ければ None
    assert combine_notifiers(None, first) is first
    assert combine_notifiers() is None
    assert combine_notifiers(None, None) is None


def test_telegram_and_line_notifiers_combined(tmp_path: Path):
    # Web / CLI の巡回と同じ結線: Telegram と LINE の両方の通知を
    # まとめると、1 回の投稿で両方へ届く
    from kuma_agent.telegram import (
        build_proactive_notifier as build_telegram_notifier,
    )

    class _FakeTelegramApi:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        def get_updates(self, offset, timeout):
            return []

        def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))

    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = _cfg(tmp_path, telegram_bot_token="dummy-token",
               telegram_allowed_chats="123")
    line_api = _FakeLineApi()
    telegram_api = _FakeTelegramApi()
    notify = combine_notifiers(
        build_telegram_notifier(cfg, api=telegram_api),
        build_proactive_notifier(cfg, api=line_api))
    assert notify is not None

    text = check_and_post(cfg, now=NOW, notify=notify,
                          session_provider=lambda: Session(cfg, _EchoModel()))
    assert text is not None
    assert telegram_api.sent == [(123, text)]
    assert line_api.pushes == [("U123", [text]), ("U456", [text])]
