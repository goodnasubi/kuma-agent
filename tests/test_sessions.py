"""セッション管理（部屋分け・再開・要約による圧縮・Web 切替）のテスト。"""
import json
import threading
import urllib.request
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import Memory, list_session_ids, normalize_session_id
from kuma_agent.session import SUMMARY_PREFIX, Session
from kuma_agent.web import build_server


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


# ---------------------------------------------------------------- 名前と記録

def test_normalize_session_id():
    assert normalize_session_id("main") == "main"
    assert normalize_session_id(" 仕事 メモ ") == "仕事-メモ"
    assert normalize_session_id("../etc/passwd").endswith("etc-passwd")
    assert ".." not in normalize_session_id("../../x")
    assert normalize_session_id("") == "main"
    assert normalize_session_id(None) == "main"
    assert normalize_session_id("///") == "main"


def test_memory_writes_per_session_file(tmp_path: Path):
    mem = Memory(tmp_path, "work")
    mem.append_turn("user", "仕事の話")
    path = tmp_path / "conversations" / "work.jsonl"
    assert path.exists()
    records = mem.load_turns()
    assert records[0]["role"] == "user"
    assert records[0]["content"] == "仕事の話"
    assert records[0]["session"] == "work"
    assert list_session_ids(tmp_path) == ["work"]


# ---------------------------------------------------------------- 再開

def test_session_resume_restores_history(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    first = Session(cfg, _EchoModel(), "room1")
    first.say("覚えていて")
    assert (tmp_path / "conversations" / "room1.jsonl").exists()

    resumed = Session(cfg, _EchoModel(), "room1")
    pairs = [(m.role, m.content) for m in resumed.history]
    assert ("user", "覚えていて") in pairs
    assert ("assistant", "返答: 覚えていて") in pairs
    # 添付のバイト列は復元されない（発話はプレーンな Message になる）
    assert all(not m.attachments for m in resumed.history)


def test_sessions_do_not_mix(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    Session(cfg, _EchoModel(), "work").say("仕事の相談")
    Session(cfg, _EchoModel(), "private").say("内緒の相談")

    work = Session(cfg, _EchoModel(), "work")
    contents = [m.content for m in work.history]
    assert any("仕事の相談" in c for c in contents)
    assert all("内緒" not in c for c in contents)
    assert (tmp_path / "conversations" / "work.jsonl").exists()
    assert (tmp_path / "conversations" / "private.jsonl").exists()


# ---------------------------------------------------------------- 圧縮

class _SummaryModel:
    """要約専用プロンプトが来たら要約文を返すスタブ。それ以外は通常応答。"""

    def __init__(self) -> None:
        self.summary_calls = 0

    def chat(self, system_prompt, messages, tools):
        if "要約者" in system_prompt:
            self.summary_calls += 1
            assert tools == []  # 要約は道具なしで呼ぶ
            return Message("assistant", "これまでの要約テキスト")
        return Message("assistant", f"返答: {messages[-1].content}")


def test_compaction_replaces_old_turns_with_summary(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, compact_after=4, compact_keep=2)
    model = _SummaryModel()
    session = Session(cfg, model)
    session.say("一つ目")
    session.say("二つ目")
    assert model.summary_calls == 0  # 発話数 4 ではまだ発動しない
    session.say("三つ目")  # 発話数 6 になり圧縮が発動する

    assert model.summary_calls == 1
    turns = [m for m in session.history if m.role in ("user", "assistant")]
    assert len(turns) == 3  # 要約 1 件 + 直近 2 件
    assert turns[0].role == "user"
    assert turns[0].content.startswith(SUMMARY_PREFIX)
    assert "これまでの要約テキスト" in turns[0].content
    assert [m.content for m in turns[1:]] == ["三つ目", "返答: 三つ目"]

    # 記録にも summary として残り、再開すると要約発話として復元される
    mem = Memory(tmp_path)
    assert any(r["role"] == "summary" and "要約テキスト" in r["content"]
               for r in mem.load_turns())
    resumed = Session(cfg, _EchoModel())
    assert resumed.history[0].content.startswith(SUMMARY_PREFIX)


class _FailingSummaryModel:
    def chat(self, system_prompt, messages, tools):
        if "要約者" in system_prompt:
            raise RuntimeError("要約に失敗")
        return Message("assistant", "ok")


def test_compaction_failure_keeps_history(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, compact_after=4, compact_keep=2)
    session = Session(cfg, _FailingSummaryModel())
    for i in range(3):
        assert session.say(f"発話{i}") == "ok"  # 例外が漏れない
    turns = [m for m in session.history if m.role in ("user", "assistant")]
    assert len(turns) == 6  # 履歴は壊れずそのまま残る
    assert all(not m.content.startswith(SUMMARY_PREFIX) for m in turns)


def test_compaction_disabled_with_zero(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, compact_after=0, compact_keep=2)
    model = _SummaryModel()
    session = Session(cfg, model)
    for i in range(5):
        session.say(f"発話{i}")
    assert model.summary_calls == 0
    assert len(session.history) == 10


# ---------------------------------------------------------------- Web

class _TaggedModel:
    """部屋名を前置して返すスタブ（部屋の取り違え検出用）。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"{self.tag}: {messages[-1].content}")


@pytest.fixture()
def web_server(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)

    def factory(name: str) -> Session:
        return Session(cfg, _TaggedModel(name), name)

    httpd = build_server(factory("main"), host="127.0.0.1", port=0,
                         session_factory=factory)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_web_sessions_are_separate(web_server: str):
    reply_a = _post_json(web_server + "/api/chat",
                         {"message": "Aの話", "session": "room-a"})
    assert reply_a == {"reply": "room-a: Aの話"}
    reply_main = _post_json(web_server + "/api/chat", {"message": "既定の話"})
    assert reply_main == {"reply": "main: 既定の話"}

    hist_a = _get_json(web_server + "/api/history?session=room-a")["messages"]
    assert [m["content"] for m in hist_a] == ["Aの話", "room-a: Aの話"]
    hist_b = _get_json(web_server + "/api/history?session=room-b")["messages"]
    assert hist_b == []
    hist_main = _get_json(web_server + "/api/history")["messages"]
    assert all("Aの話" not in m["content"] for m in hist_main)

    sessions = _get_json(web_server + "/api/sessions")["sessions"]
    assert "main" in sessions and "room-a" in sessions


def test_resume_after_compaction_keeps_recent_turns(tmp_path: Path):
    """圧縮後に再開しても、要約＋残した直近の発話が復元される。"""
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.compact_after = 4
    cfg.compact_keep = 2
    first = Session(cfg, _SummaryModel(), session_id="room")
    for text in ("一", "二", "三"):
        first.say(text)
    assert len(first.history) == 3

    resumed = Session(cfg, _SummaryModel(), session_id="room")
    assert [m.content for m in resumed.history] == [m.content for m in first.history]
