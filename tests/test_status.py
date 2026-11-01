"""作業中表示（Session の状態通知・Web の /api/status・TUI の点）のテスト。"""
import json
import threading
import urllib.request
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import SUMMARIZING, THINKING, Session
from kuma_agent.tools import activity_label
from kuma_agent.tui import status_dots, status_line
from kuma_agent.web import build_server, render_page


# ---------------------------------------------------------------- 表示名

def test_activity_label_known_tools():
    assert activity_label("read_file") == "ファイルを読んでいます"
    assert activity_label("write_file") == "ファイルを書いています"
    assert activity_label("edit_file") == "ファイルを編集中"
    assert activity_label("run_shell") == "コマンドを実行中"
    assert activity_label("list_tasks") == "台帳を確認中"
    assert activity_label("create_task") == "台帳を更新中"
    assert activity_label("update_task") == "台帳を更新中"
    assert activity_label("write_memory") == "記憶に書き留め中"


def test_activity_label_includes_safe_hint():
    assert activity_label("read_file", {"path": "note.txt"}) == \
        "ファイルを読んでいます（note.txt）"
    assert activity_label("write_file", {"path": "a/b.txt"}) == \
        "ファイルを書いています（a/b.txt）"
    assert activity_label("create_task", {"title": "買い物"}) == \
        "台帳を更新中（買い物）"


def test_activity_label_ignores_unsafe_or_missing_hint():
    # コマンド文字列は表示に含めない
    assert activity_label("run_shell", {"command": "rm -rf /"}) == "コマンドを実行中"
    assert activity_label("read_file", {"path": 123}) == "ファイルを読んでいます"
    assert activity_label("read_file", {}) == "ファイルを読んでいます"


def test_activity_label_unknown_tool_is_generic():
    assert activity_label("search_web") == "道具を実行中（search_web）"


# ---------------------------------------------------------------- Session の通知

class _ToolModel:
    """1 回目で read_file を呼び、2 回目で最終応答を返すスタブ。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": "read_file", "args": {"path": "note.txt"}}])
        return Message("assistant", "読みました")


def test_session_notifies_status_sequence(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.txt").write_text("メモ", encoding="utf-8")
    seen: list[str] = []
    session = Session(Config(data_dir=tmp_path / "data"), _ToolModel(),
                      on_status=seen.append)
    assert session.say("読んで") == "読みました"
    assert seen == [THINKING,
                    "ファイルを読んでいます（note.txt）",
                    THINKING,
                    ""]


def test_session_status_callback_settable_as_attribute(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _ToolModel())
    seen: list[str] = []
    session.on_status = seen.append
    session.say("読んで", )
    assert seen[0] == THINKING and seen[-1] == ""


def test_session_survives_broken_status_callback(tmp_path: Path):
    def broken(status: str) -> None:
        raise RuntimeError("表示側の失敗")

    session = Session(Config(data_dir=tmp_path), _ToolModel(),
                      on_status=broken)
    assert session.say("読んで") == "読みました"


class _SummaryModel:
    def chat(self, system_prompt, messages, tools):
        if "要約者" in system_prompt:
            return Message("assistant", "要約テキスト")
        return Message("assistant", f"返答: {messages[-1].content}")


def test_session_notifies_summarizing_on_compaction(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, compact_after=2, compact_keep=1)
    seen: list[str] = []
    session = Session(cfg, _SummaryModel(), on_status=seen.append)
    session.say("一つ目")
    session.say("二つ目")  # 発話数が閾値を超えて圧縮が発動する
    assert SUMMARIZING in seen
    assert seen[-1] == ""


# ---------------------------------------------------------------- Web

class _SlowModel:
    """release されるまで応答を返さないスタブ。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def chat(self, system_prompt, messages, tools):
        self.started.set()
        self.release.wait(timeout=10)
        return Message("assistant", "遅い返答")


@pytest.fixture()
def slow_server(tmp_path: Path):
    model = _SlowModel()
    session = Session(Config(data_dir=tmp_path), model)
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", model
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_status_endpoint_reports_and_clears(slow_server):
    base, model = slow_server
    result: dict = {}

    def post() -> None:
        req = urllib.request.Request(
            base + "/api/chat",
            data=json.dumps({"message": "こんにちは"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            result.update(json.loads(resp.read().decode("utf-8")))

    poster = threading.Thread(target=post)
    poster.start()
    try:
        assert model.started.wait(timeout=5)
        during = _get_json(base + "/api/status")
        assert during["status"] == THINKING
    finally:
        model.release.set()
    poster.join(timeout=15)
    assert result["reply"] == "遅い返答"
    assert _get_json(base + "/api/status")["status"] == ""


def test_page_contains_status_indicator():
    page = render_page("muse", avatar_svg="<svg></svg>")
    assert "statusBox" in page
    assert "/api/status" in page


# ---------------------------------------------------------------- TUI の点

def test_status_dots_cycle_by_frame():
    assert [status_dots(i) for i in range(5)] == ["", ".", "..", "...", ""]


def test_status_line_appends_dots_and_hides_when_empty():
    assert status_line("考え中", 2) == "考え中.."
    assert status_line("", 2) == ""
