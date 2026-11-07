"""活動の記録（activity）のテスト。

記録モジュール（追記・読み取り・保持期限の掃除・1 日 1 回の
自動掃除）、Session 経由の記録（道具の呼び出し・承認・CLI の
起動）、外部送信（Telegram）の記録、Web API と管理タブの
パネルを確認する。
"""
import json
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kuma_agent.activity import (
    ACTIVITY_FILE,
    auto_cleanup_activity,
    cleanup_expired_activity,
    load_activity_records,
    record_activity,
    record_approval,
    record_cli_launch,
    record_external_send,
    record_tool_call,
)
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.telegram import build_proactive_notifier
from kuma_agent.web import build_server

NOW = datetime(2026, 11, 7, 12, 0, 0)


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------- 記録と読み取り

def test_record_and_load_newest_first(tmp_path):
    record_tool_call(tmp_path, "main", "ledger", "read_file",
                     '{"path": "a.txt"}', "中身", now=NOW)
    record_approval(tmp_path, "main", "ledger", "run_shell",
                    "echo hi", True,
                    now=NOW + timedelta(seconds=1))
    record_external_send(tmp_path, "telegram", "123", "知らせの文面",
                         now=NOW + timedelta(seconds=2))
    record_cli_launch(tmp_path, "main", "ledger", "codex", "gpt-x",
                      now=NOW + timedelta(seconds=3))
    records = load_activity_records(tmp_path)
    assert [r["kind"] for r in records] == [
        "cli_launch", "external_send", "approval", "tool_call"]
    tool = records[3]
    assert tool["tool"] == "read_file"
    assert tool["summary"] == '{"path": "a.txt"}'
    assert tool["result"] == "中身"
    assert tool["ok"] is True
    assert tool["session_id"] == "main" and tool["mode"] == "ledger"
    approval = records[2]
    assert approval["result"] == "approved" and approval["ok"] is True
    send = records[1]
    assert send["tool"] == "telegram" and send["target"] == "123"
    assert send["summary"] == "知らせの文面"
    launch = records[0]
    assert launch["tool"] == "codex" and launch["summary"] == "gpt-x"


def test_tool_call_error_result_marks_not_ok(tmp_path):
    record_tool_call(tmp_path, "main", "ledger", "run_shell", "ls",
                     "error: 失敗", now=NOW)
    (record,) = load_activity_records(tmp_path)
    assert record["ok"] is False
    assert record["result"] == "error: 失敗"


def test_load_limit_is_normalized(tmp_path):
    for i in range(5):
        record_activity(tmp_path, "tool_call", tool=f"t{i}", now=NOW)
    assert len(load_activity_records(tmp_path)) == 5
    assert len(load_activity_records(tmp_path, limit=2)) == 2
    assert len(load_activity_records(tmp_path, limit="3")) == 3
    # 不正な指定は既定（100）に倒す
    assert len(load_activity_records(tmp_path, limit="x")) == 5


def test_load_skips_broken_lines(tmp_path):
    path = tmp_path / ACTIVITY_FILE
    path.write_text(
        '{"ts": "2026-11-07T12:00:00", "kind": "tool_call"}\n'
        "壊れた行\n\n"
        '{"ts": "2026-11-07T12:01:00", "kind": "approval"}\n',
        encoding="utf-8")
    records = load_activity_records(tmp_path)
    assert [r["kind"] for r in records] == ["approval", "tool_call"]


def test_record_failure_does_not_raise(tmp_path):
    # 置き場の親がファイルだと mkdir に失敗するが、例外は出ない
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    record_tool_call(blocker / "sub", "main", "ledger", "now", "", "ok")
    record_activity(blocker / "sub", "tool_call")


# ------------------------------------------------------- 保持期限の掃除

def _write_old_and_new(tmp_path: Path) -> None:
    old = NOW - timedelta(days=100)
    record_tool_call(tmp_path, "main", "ledger", "now", "", "古い",
                     now=old)
    record_tool_call(tmp_path, "main", "ledger", "now", "", "新しい",
                     now=NOW)


def test_cleanup_removes_expired_lines(tmp_path):
    _write_old_and_new(tmp_path)
    removed = cleanup_expired_activity(tmp_path, 90, now=NOW)
    assert removed == 1
    records = _records(tmp_path / ACTIVITY_FILE)
    assert len(records) == 1 and records[0]["result"] == "新しい"


def test_cleanup_unlimited_when_zero_days(tmp_path):
    _write_old_and_new(tmp_path)
    assert cleanup_expired_activity(tmp_path, 0, now=NOW) == 0
    assert len(_records(tmp_path / ACTIVITY_FILE)) == 2


def test_cleanup_keeps_lines_without_readable_time(tmp_path):
    _write_old_and_new(tmp_path)
    path = tmp_path / ACTIVITY_FILE
    with open(path, "a", encoding="utf-8") as f:
        f.write("壊れた行\n")
        f.write('{"kind": "tool_call"}\n')  # 時刻が無い行も残す
    removed = cleanup_expired_activity(tmp_path, 90, now=NOW)
    assert removed == 1
    lines = [line for line in
             path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 3


def test_auto_cleanup_runs_once_per_day(tmp_path):
    _write_old_and_new(tmp_path)
    assert auto_cleanup_activity(tmp_path, 90, now=NOW) == 1
    # 同じ日にもう一度呼んでも、掃除済みとして何もしない
    record_tool_call(tmp_path, "main", "ledger", "now", "", "古いはず",
                     now=NOW - timedelta(days=100))
    assert auto_cleanup_activity(tmp_path, 90, now=NOW) == 0
    # 翌日なら再び掃除する
    assert auto_cleanup_activity(
        tmp_path, 90, now=NOW + timedelta(days=1)) == 1


# ------------------------------------------------------- Session 経由

class _ToolLoopModel:
    """1 回目は道具（now）を呼び、2 回目は答えるスタブ。"""

    def __init__(self) -> None:
        self.rounds = 0

    def chat(self, system_prompt, messages, tools):
        self.rounds += 1
        if self.rounds == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": "now", "args": {}}])
        return Message("assistant", "時刻です")


def test_session_records_tool_call(tmp_path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _ToolLoopModel())
    assert session.say("今何時？") == "時刻です"
    records = _records(tmp_path / ACTIVITY_FILE)
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "tool_call"
    assert record["tool"] == "now"
    assert record["ok"] is True
    assert record["session_id"] == "main"
    assert record["mode"] == "ledger"


class _ShellModel:
    """run_shell を 1 回呼び、次に答えるスタブ。"""

    def __init__(self) -> None:
        self.rounds = 0

    def chat(self, system_prompt, messages, tools):
        self.rounds += 1
        if self.rounds == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "s1", "name": "run_shell",
                 "args": {"command": "echo hi"}}])
        return Message("assistant", "終わりました")


def test_session_records_approval_and_denied_tool_call(tmp_path,
                                                       monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path)
    # 承認は既定で有効。承認者が却下すると、承認の記録と
    # 却下された道具の記録の両方が残る
    session = Session(cfg, _ShellModel(),
                      approver=lambda name, args: False)
    assert session.say("実行して") == "終わりました"
    records = _records(tmp_path / ACTIVITY_FILE)
    kinds = [r["kind"] for r in records]
    assert kinds == ["approval", "tool_call"]
    assert records[0]["result"] == "denied"
    assert records[0]["summary"] == "echo hi"
    assert records[1]["ok"] is False
    assert "承認されませんでした" in records[1]["result"]


def test_session_records_approved_tool_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _ShellModel(),
                      approver=lambda name, args: True)
    assert session.say("実行して") == "終わりました"
    records = _records(tmp_path / ACTIVITY_FILE)
    assert [r["kind"] for r in records] == ["approval", "tool_call"]
    assert records[0]["result"] == "approved"
    assert records[1]["ok"] is True
    assert records[1]["result"] == "hi"


class _PlainModel:
    """道具なしで即答するスタブ。"""

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "答え")


def test_session_records_cli_launch_only_for_cli_providers(tmp_path):
    cfg = Config(data_dir=tmp_path, provider="claude-code")
    session = Session(cfg, _PlainModel())
    session.say("こんにちは")
    records = _records(tmp_path / ACTIVITY_FILE)
    assert len(records) == 1
    assert records[0]["kind"] == "cli_launch"
    assert records[0]["tool"] == "claude-code"


def test_session_no_cli_launch_for_api_providers(tmp_path):
    cfg = Config(data_dir=tmp_path)  # 既定は API 系（muse）
    session = Session(cfg, _PlainModel())
    session.say("こんにちは")
    assert not (tmp_path / ACTIVITY_FILE).exists()


# ------------------------------------------------------- 外部送信

class _FakeTelegramApi:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_telegram_send_is_recorded(tmp_path):
    cfg = Config(data_dir=tmp_path, telegram_bot_token="token",
                 telegram_allowed_chats="123")
    api = _FakeTelegramApi()
    notify = build_proactive_notifier(cfg, api=api)
    assert notify is not None
    notify("default", "default", "知らせです")
    assert api.sent == [(123, "知らせです")]
    records = _records(tmp_path / ACTIVITY_FILE)
    assert len(records) == 1
    record = records[0]
    assert record["kind"] == "external_send"
    assert record["tool"] == "telegram"
    assert record["target"] == "123"
    assert record["summary"] == "知らせです"
    # 配信先でないユーザーの投稿は送らず、記録もしない
    notify("default", "other", "別の知らせ")
    assert len(_records(tmp_path / ACTIVITY_FILE)) == 1


# ------------------------------------------------------- Web

@pytest.fixture()
def server(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _PlainModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get_json(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_activity_api_returns_newest_first(server):
    base, tmp_path = server
    record_tool_call(tmp_path, "main", "ledger", "now", "", "1件目",
                     now=NOW)
    record_approval(tmp_path, "main", "ledger", "run_shell", "echo hi",
                    True, now=NOW + timedelta(seconds=1))
    status, data = _get_json(base + "/api/activity")
    assert status == 200
    assert data["tenant"] == "default" and data["user"] == "default"
    assert [e["kind"] for e in data["entries"]] == [
        "approval", "tool_call"]
    # limit が効く
    status, data = _get_json(base + "/api/activity?limit=1")
    assert len(data["entries"]) == 1
    # 記録の無い組み合わせは空で返る
    status, data = _get_json(base + "/api/activity?tenant=acme&user=bob")
    assert status == 200 and data["entries"] == []


def test_page_has_activity_panel(server):
    base, _tmp_path = server
    with urllib.request.urlopen(base + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert 'id="activityBtn"' in html
    assert 'id="activityPanel"' in html
    assert 'id="activityRows"' in html
