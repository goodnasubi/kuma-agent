"""「次のアクション」の提案（リアクションで実行）のテスト。

頭脳の返事に付いた専用マーカーが本文・記録から取り除かれて提案
として保存されること、👍 で実行指示文が返り（実行は一回限り）、
👎 で見送りになること、提案の無い発話では従来どおりのリアク
ション動作になることを確認する。流儀は test_reactions.py に倣う。
"""
import json
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import (
    Memory,
    extract_next_action,
    proposal_instruction,
)
from kuma_agent.session import Session
from kuma_agent.web import build_server, render_page

PROPOSAL_TEXT = "明日の予定をカレンダーで確認する"


def _memory(tmp_path: Path) -> Memory:
    mem = Memory(tmp_path, "room1")
    mem.append_turn("user", "こんにちは")
    mem.append_turn("assistant", "こんにちは。お手伝いします")
    return mem


def test_extract_next_action_strips_marker():
    body, proposal = extract_next_action(
        f"了解です。\n[[next: {PROPOSAL_TEXT}]]")
    assert body == "了解です。"
    assert proposal == PROPOSAL_TEXT


def test_extract_next_action_without_marker():
    assert extract_next_action("普通の返事") == ("普通の返事", None)


def test_extract_next_action_keeps_body_when_marker_only():
    # 本文が空になる場合はマーカー扱いしない
    text = f"[[next: {PROPOSAL_TEXT}]]"
    assert extract_next_action(text) == (text, None)
    assert extract_next_action("[[next: ]]") == ("[[next: ]]", None)


def test_proposal_roundtrip(tmp_path):
    mem = _memory(tmp_path)
    mem.set_proposal(1, PROPOSAL_TEXT)
    assert mem.load_proposals() == {
        1: {"text": PROPOSAL_TEXT, "state": "pending"}}
    # 別ファイルに {位置: {text, state}} で保存され、読み直しても残る
    proposals_file = tmp_path / "conversations" / "room1.proposals.json"
    assert json.loads(proposals_file.read_text(encoding="utf-8")) == {
        "1": {"text": PROPOSAL_TEXT, "state": "pending"}}
    assert Memory(tmp_path, "room1").load_proposals() == {
        1: {"text": PROPOSAL_TEXT, "state": "pending"}}
    # 宛先は assistant の発話だけ
    with pytest.raises(ValueError):
        mem.set_proposal(0, PROPOSAL_TEXT)  # 利用者の発話
    with pytest.raises(ValueError):
        mem.set_proposal(9, PROPOSAL_TEXT)  # 存在しない発話


def test_react_to_proposal_approve_is_one_shot(tmp_path):
    mem = _memory(tmp_path)
    mem.set_proposal(1, PROPOSAL_TEXT)
    # 外す操作（ON になっていない）では何も起きない
    assert mem.react_to_proposal(1, "👍", turned_on=False) is None
    assert mem.load_proposals()[1]["state"] == "pending"
    # 👍 が付いたら実行指示文が返り、状態は done になる
    assert mem.react_to_proposal(1, "👍", turned_on=True) == \
        proposal_instruction(PROPOSAL_TEXT)
    assert mem.load_proposals()[1]["state"] == "done"
    # 二度目は返らない（実行は一回限り）
    assert mem.react_to_proposal(1, "👍", turned_on=True) is None
    # done の後に 👎 を付けても見送りにはならない
    assert mem.react_to_proposal(1, "👎", turned_on=True) is None
    assert mem.load_proposals()[1]["state"] == "done"


def test_react_to_proposal_dismiss(tmp_path):
    mem = _memory(tmp_path)
    mem.set_proposal(1, PROPOSAL_TEXT)
    assert mem.react_to_proposal(1, "👎", turned_on=True) is None
    assert mem.load_proposals()[1]["state"] == "dismissed"
    # 見送った後は 👍 でも実行されない
    assert mem.react_to_proposal(1, "👍", turned_on=True) is None


def test_react_to_proposal_ignores_other_cases(tmp_path):
    mem = _memory(tmp_path)
    # 提案の無い発話では 👍 でも何も返らない
    assert mem.react_to_proposal(1, "👍", turned_on=True) is None
    mem.set_proposal(1, PROPOSAL_TEXT)
    # 👍👎 以外の絵文字は提案に影響しない
    assert mem.react_to_proposal(1, "❤️", turned_on=True) is None
    assert mem.load_proposals()[1]["state"] == "pending"


class _ProposalModel:
    """「提案して」と言われたらマーカー付きで返すスタブ頭脳。"""

    def __init__(self):
        self.prompts: list[str] = []
        self.seen: list[Message] = []

    def chat(self, system_prompt, messages, tools):
        self.prompts.append(system_prompt)
        self.seen = list(messages)
        if messages[-1].content == "提案して":
            return Message(
                "assistant",
                f"分かりました。よければ 👍 で進めます。\n"
                f"[[next: {PROPOSAL_TEXT}]]")
        return Message("assistant", "了解です")


def test_session_strips_marker_and_saves_proposal(tmp_path):
    cfg = Config(data_dir=tmp_path)
    model = _ProposalModel()
    session = Session(cfg, model)
    reply = session.say("提案して")
    # 返事の本文・履歴・記録のどれにもマーカーは残らない
    assert "[[next:" not in reply
    assert reply.startswith("分かりました。")
    assert all("[[next:" not in m.content for m in session.history)
    turns = session.memory.load_turns()
    assert all("[[next:" not in str(t["content"]) for t in turns)
    # 提案は相棒の発話（rid=1）に保留中で保存される
    assert session.memory.load_proposals() == {
        1: {"text": PROPOSAL_TEXT, "state": "pending"}}
    # システムプロンプトにマーカーの約束が入っている
    assert "[[next:" in model.prompts[0]


def test_proposal_note_reaches_model_context_only(tmp_path):
    """提案の注記（状態つき）はモデルへの入力にだけ入る。"""
    cfg = Config(data_dir=tmp_path)
    model = _ProposalModel()
    session = Session(cfg, model)
    session.say("提案して")
    session.say("つづき")
    by_rid = {m.record_index: m for m in model.seen
              if m.record_index is not None}
    assert f"（この発話で次のアクションを提案した: {PROPOSAL_TEXT}）" \
        in by_rid[1].content
    # 実行済みになると状態も伝わる
    session.memory.react_to_proposal(1, "👍", turned_on=True)
    session.say("さらに")
    by_rid = {m.record_index: m for m in model.seen
              if m.record_index is not None}
    assert "／実行済み）" in by_rid[1].content
    # 履歴と記録には注記が混ざらない
    assert all("提案した" not in m.content for m in session.history)
    turns = session.memory.load_turns()
    assert all("提案した" not in str(t["content"]) for t in turns)


@contextmanager
def _running(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _ProposalModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(base: str, path: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path) as res:
        return json.loads(res.read().decode("utf-8"))


def test_proposal_over_http(tmp_path):
    with _running(tmp_path) as base:
        status, chat = _post(base, "/api/chat", {"message": "提案して"})
        assert status == 200
        assert "[[next:" not in chat["reply"]
        assert chat["proposal"] == {
            "text": PROPOSAL_TEXT, "state": "pending"}
        rid = chat["rid"]

        history = _get(base, "/api/history")
        by_rid = {m["rid"]: m for m in history["messages"] if "rid" in m}
        assert by_rid[rid]["proposal"] == {
            "text": PROPOSAL_TEXT, "state": "pending"}

        # 👍 で実行指示文が返り、提案は done になる
        status, data = _post(base, "/api/reactions",
                             {"rid": rid, "emoji": "👍"})
        assert status == 200
        assert data["reactions"] == ["👍"]
        assert data["action"] == proposal_instruction(PROPOSAL_TEXT)
        assert data["proposal"] == {
            "text": PROPOSAL_TEXT, "state": "done"}

        # 👍 を外しても（もう一度押しても）二度目は実行されない
        status, data = _post(base, "/api/reactions",
                             {"rid": rid, "emoji": "👍"})
        assert status == 200
        assert "action" not in data
        assert data["proposal"]["state"] == "done"


def test_proposal_dismiss_over_http(tmp_path):
    with _running(tmp_path) as base:
        status, chat = _post(base, "/api/chat", {"message": "提案して"})
        assert status == 200
        rid = chat["rid"]
        # 👎 で見送り。チャットターンは発生しない（action は無い）
        status, data = _post(base, "/api/reactions",
                             {"rid": rid, "emoji": "👎"})
        assert status == 200
        assert "action" not in data
        assert data["proposal"] == {
            "text": PROPOSAL_TEXT, "state": "dismissed"}
        history = _get(base, "/api/history")
        by_rid = {m["rid"]: m for m in history["messages"] if "rid" in m}
        assert by_rid[rid]["proposal"]["state"] == "dismissed"


def test_reaction_without_proposal_over_http(tmp_path):
    """提案の無い発話へのリアクションは従来どおり（余分なキーは無い）。"""
    with _running(tmp_path) as base:
        status, chat = _post(base, "/api/chat", {"message": "やあ"})
        assert status == 200
        assert "proposal" not in chat
        status, data = _post(base, "/api/reactions",
                             {"rid": chat["rid"], "emoji": "👍"})
        assert status == 200
        assert data == {"rid": chat["rid"], "reactions": ["👍"],
                        "agent_reaction": "👍"}


def test_render_page_has_proposal_ui():
    page = render_page("muse")
    assert "proposalChip" in page
    assert "renderProposalChip" in page
    assert "sendInstructionMessage" in page
    assert "👎" in page  # 見送りの合図がパレットにも出る
