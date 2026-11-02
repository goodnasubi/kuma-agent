"""能動発信の Telegram 配信（telegram.build_proactive_notifier）のテスト。

ネットワークには出ない: Bot API は偽物（送信の記録、失敗の再現）に
差し替える。巡回（proactive）の投稿（日報・リマインダー・定期
ジョブの結果）が、Telegram の行き先のテナント／ユーザーについて
だけ許可リストの全チャットへ送られること、無効化・未設定では
送られないこと、送信失敗でも会話への投稿は残ることを確認する。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.proactive import check_and_post, run_sweep
from kuma_agent.session import Session
from kuma_agent.telegram import MESSAGE_LIMIT, build_proactive_notifier

NOW = datetime(2026, 11, 2, 9, 0, 0)


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


class _FakeApi:
    """Bot API の偽物。送信を記録し、fail=True なら送信で例外を出す。"""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    def get_updates(self, offset, timeout):
        return []

    def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("送信に失敗しました（偽物）")
        self.sent.append((chat_id, text))


def _cfg(tmp_path: Path, **kwargs) -> Config:
    kwargs.setdefault("telegram_bot_token", "dummy-token")
    kwargs.setdefault("telegram_allowed_chats", "123,456")
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


def _sent_texts(api: _FakeApi) -> list[str]:
    return [text for _chat_id, text in api.sent]


# ---------------------------------------------------------------- 日報

def test_daily_report_delivered_to_all_allowed_chats(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = _cfg(tmp_path)
    api = _FakeApi()
    notify = build_proactive_notifier(cfg, api=api)
    assert notify is not None

    text = check_and_post(cfg, now=NOW, notify=notify)
    assert text is not None and "買い物" in text
    # 許可リストの全チャットへ、会話へ投稿したのと同じ文面が届く
    assert api.sent == [(123, text), (456, text)]
    # 会話への投稿も従来どおり残る
    assert [r["content"] for r in _records(tmp_path)] == [text]


def test_no_post_no_delivery(tmp_path: Path):
    cfg = _cfg(tmp_path)
    api = _FakeApi()
    notify = build_proactive_notifier(cfg, api=api)
    # 用事が無ければ投稿も配信も無い
    assert check_and_post(cfg, now=NOW, notify=notify) is None
    assert api.sent == []


# ---------------------------------------------------------------- リマインダー

def test_reminder_delivered(tmp_path: Path):
    due = (NOW - timedelta(minutes=30)).isoformat(timespec="seconds")
    _write_tasks(tmp_path, [_task("1", "薬を飲む", NOW, remind_at=due)])
    # 日報は本日投稿済みにしておき、リマインダーだけが立つようにする
    (tmp_path / "proactive_state.json").write_text(
        json.dumps({"last_posted_on": "2026-11-02"}), encoding="utf-8")
    cfg = _cfg(tmp_path)
    api = _FakeApi()
    notify = build_proactive_notifier(cfg, api=api)

    text = check_and_post(cfg, now=NOW, notify=notify)
    assert text is not None and "薬を飲む" in text
    assert api.sent == [(123, text), (456, text)]
    assert [r["content"] for r in _records(tmp_path)] == [text]


# ---------------------------------------------------------------- 定期ジョブ

def _write_due_job(user_dir: Path) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    record = {"id": "1", "title": "朝の確認", "prompt": "ニュースを確認して",
              "schedule": {"kind": "daily", "time": "08:00"},
              "enabled": True, "last_run_at": "",
              "created_at": "2026-11-01T08:00:00"}
    (user_dir / "jobs.json").write_text(
        json.dumps([record], ensure_ascii=False), encoding="utf-8")


def test_job_result_delivered(tmp_path: Path):
    _write_due_job(tmp_path)
    cfg = _cfg(tmp_path)
    api = _FakeApi()
    notify = build_proactive_notifier(cfg, api=api)
    session = Session(cfg, _EchoModel())

    text = check_and_post(
        cfg, now=NOW, session_provider=lambda: session,
        job_executor=lambda parent, prompt: "ジョブの実行結果",
        notify=notify)
    assert text is not None
    assert "定期ジョブ「朝の確認」の結果です。" in text
    assert api.sent == [(123, text), (456, text)]
    assert [r["content"] for r in _records(tmp_path)] == [text]


def test_job_failure_delivered(tmp_path: Path):
    _write_due_job(tmp_path)
    cfg = _cfg(tmp_path)
    api = _FakeApi()
    notify = build_proactive_notifier(cfg, api=api)
    session = Session(cfg, _EchoModel())

    text = check_and_post(
        cfg, now=NOW, session_provider=lambda: session,
        job_executor=lambda parent, prompt: "error: 何か壊れた",
        notify=notify)
    assert text is not None and "失敗しました" in text
    assert api.sent == [(123, text), (456, text)]


# ---------------------------------------------------------------- 届け先の絞り込み

def test_other_user_not_delivered(tmp_path: Path):
    # 既定ユーザー（Telegram の行き先）と、別ユーザー bob の両方に
    # 用事があり、巡回では両方とも会話へ投稿されるが、Telegram へ
    # 届くのは行き先のユーザーの分だけ
    _write_tasks(tmp_path, [_task("1", "自分の用事", NOW)])
    _write_tasks(tmp_path / "users" / "bob",
                 [_task("1", "bobの用事", NOW)])
    cfg = _cfg(tmp_path)  # telegram_user は既定の default
    api = _FakeApi()
    notify = build_proactive_notifier(cfg, api=api)

    posted = run_sweep(cfg, now=NOW, notify=notify)
    assert {(t, u) for t, u, _text in posted} == {
        ("default", "default"), ("default", "bob")}
    # bob の会話にも投稿はされている
    assert len(_records(tmp_path / "users" / "bob")) == 1
    # Telegram へは行き先ユーザーの文面だけが全チャットへ届く
    own = [text for _t, u, text in posted if u == "default"][0]
    assert "自分の用事" in own
    assert api.sent == [(123, own), (456, own)]


def test_notifier_ignores_other_tenant_and_user(tmp_path: Path):
    cfg = _cfg(tmp_path, telegram_tenant="acme", telegram_user="alice")
    api = _FakeApi()
    notify = build_proactive_notifier(cfg, api=api)
    assert notify is not None
    notify("default", "alice", "違うテナント")
    notify("acme", "bob", "違うユーザー")
    assert api.sent == []
    notify("acme", "alice", "対象の投稿")
    assert api.sent == [(123, "対象の投稿"), (456, "対象の投稿")]


# ---------------------------------------------------------------- 無効化・未設定

def test_proactive_disabled_by_config(tmp_path: Path):
    cfg = _cfg(tmp_path, telegram_proactive=False)
    assert build_proactive_notifier(cfg, api=_FakeApi()) is None


def test_proactive_disabled_by_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KUMA_TELEGRAM_PROACTIVE", "0")
    cfg = Config(data_dir=tmp_path)
    assert cfg.telegram_proactive is False
    monkeypatch.setenv("KUMA_TELEGRAM_PROACTIVE", "1")
    assert Config(data_dir=tmp_path).telegram_proactive is True
    monkeypatch.delenv("KUMA_TELEGRAM_PROACTIVE")
    assert Config(data_dir=tmp_path).telegram_proactive is True


def test_no_token_no_notifier(tmp_path: Path):
    cfg = _cfg(tmp_path, telegram_bot_token="")
    assert build_proactive_notifier(cfg, api=_FakeApi()) is None


def test_empty_allowed_chats_no_notifier(tmp_path: Path):
    cfg = _cfg(tmp_path, telegram_allowed_chats="")
    assert build_proactive_notifier(cfg, api=_FakeApi()) is None
    cfg = _cfg(tmp_path, telegram_allowed_chats="  , 読めない")
    assert build_proactive_notifier(cfg, api=_FakeApi()) is None


def test_disabled_means_no_delivery(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = _cfg(tmp_path, telegram_proactive=False)
    notify = build_proactive_notifier(cfg, api=_FakeApi())
    assert notify is None
    # 通知が無くても会話への投稿自体は従来どおり行われる
    text = check_and_post(cfg, now=NOW, notify=notify)
    assert text is not None
    assert [r["content"] for r in _records(tmp_path)] == [text]


# ---------------------------------------------------------------- 失敗時の扱い

def test_send_failure_still_posts_to_conversation(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = _cfg(tmp_path)
    api = _FakeApi(fail=True)
    notify = build_proactive_notifier(cfg, api=api)

    # 送信が全て失敗しても例外にならず、投稿は会話記録に残る
    text = check_and_post(cfg, now=NOW, notify=notify)
    assert text is not None
    assert [r["content"] for r in _records(tmp_path)] == [text]
    assert api.sent == []  # 失敗したので記録も残らない（再送もしない）


def test_broken_notify_callback_does_not_break_posting(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = _cfg(tmp_path)

    def broken(tenant, user, text):
        raise RuntimeError("通知側の故障（偽物）")

    text = check_and_post(cfg, now=NOW, notify=broken)
    assert text is not None
    assert [r["content"] for r in _records(tmp_path)] == [text]


# ---------------------------------------------------------------- 分割

def test_long_message_split_like_replies(tmp_path: Path):
    cfg = _cfg(tmp_path, telegram_allowed_chats="123")
    api = _FakeApi()
    notify = build_proactive_notifier(cfg, api=api)
    assert notify is not None
    long_text = "\n".join(f"行 {i}" for i in range(2000))
    assert len(long_text) > MESSAGE_LIMIT
    notify("default", "default", long_text)
    assert len(api.sent) > 1
    assert all(len(text) <= MESSAGE_LIMIT for _id, text in api.sent)
    # 断片をつなげると（区切りに使った改行を除き）元の文面に戻る
    joined = "".join(_sent_texts(api))
    assert joined.replace("\n", "") == long_text.replace("\n", "")
