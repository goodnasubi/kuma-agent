"""発話へのリアクション（Memory の保存と Web API）のテスト。

付け外しが保存され、読み直しても残ること。使える絵文字の範囲と
宛先（user / assistant の発話だけ）の検証が効くこと。Web 経由では
/api/chat の応答に rid が付き、/api/reactions の付け外しが
/api/history に反映されることを確認する。
"""
import json
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import Memory
from kuma_agent.session import Session
from kuma_agent.web import build_server, render_page


def _memory(tmp_path: Path) -> Memory:
    mem = Memory(tmp_path, "room1")
    mem.append_turn("user", "こんにちは")
    mem.append_turn("assistant", "こんにちは。お手伝いします")
    return mem


def test_toggle_reaction_roundtrip(tmp_path):
    mem = _memory(tmp_path)
    assert mem.toggle_reaction(1, "👍") == ["👍"]
    assert mem.toggle_reaction(1, "❤️") == ["👍", "❤️"]
    assert mem.toggle_reaction(1, "👍") == ["❤️"]
    assert mem.load_reactions() == {1: ["❤️"]}
    # 読み直しても残る
    assert Memory(tmp_path, "room1").load_reactions() == {1: ["❤️"]}
    # 全部外すとキー自体が消える
    assert mem.toggle_reaction(1, "❤️") == []
    assert mem.load_reactions() == {}


def test_toggle_reaction_validates_emoji_and_target(tmp_path):
    mem = _memory(tmp_path)
    with pytest.raises(ValueError):
        mem.toggle_reaction(1, "🔥")  # 許可されていない絵文字
    with pytest.raises(ValueError):
        mem.toggle_reaction(9, "👍")  # 存在しない発話
    mem.append_turn("summary", "要約")
    with pytest.raises(ValueError):
        mem.toggle_reaction(2, "👍")  # 要約は宛先にできない


def test_append_turn_returns_record_index(tmp_path):
    mem = Memory(tmp_path, "room1")
    assert mem.append_turn("user", "一つ目") == 0
    assert mem.append_turn("assistant", "二つ目") == 1


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


@contextmanager
def _running(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
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


def test_reactions_over_http(tmp_path):
    with _running(tmp_path) as base:
        status, chat = _post(base, "/api/chat", {"message": "やあ"})
        assert status == 200
        assert chat["user_rid"] == 0
        assert chat["rid"] == 1

        status, data = _post(base, "/api/reactions",
                             {"rid": chat["rid"], "emoji": "🎉"})
        assert status == 200
        assert data == {"rid": 1, "reactions": ["🎉"],
                        "agent_reaction": "🎉"}

        history = _get(base, "/api/history")
        by_rid = {m["rid"]: m for m in history["messages"] if "rid" in m}
        assert by_rid[1]["reactions"] == ["🎉"]
        assert by_rid[1]["agent_reaction"] == "🎉"
        assert "reactions" not in by_rid[0]
        assert "agent_reaction" not in by_rid[0]

        status, data = _post(base, "/api/reactions",
                             {"rid": chat["rid"], "emoji": "🎉"})
        assert status == 200
        assert data["reactions"] == []
        assert data["agent_reaction"] is None

        history = _get(base, "/api/history")
        by_rid = {m["rid"]: m for m in history["messages"] if "rid" in m}
        assert "agent_reaction" not in by_rid[1]

        status, _ = _post(base, "/api/reactions",
                          {"rid": chat["rid"], "emoji": "🔥"})
        assert status == 400
        status, _ = _post(base, "/api/reactions",
                          {"rid": 99, "emoji": "👍"})
        assert status == 400


def test_render_page_has_reaction_ui():
    page = render_page("muse")
    assert "reactionPalette" in page
    assert "👍" in page and "🙏" in page
    assert "agentChip" in page  # クマのリアクション返しのチップ


def test_agent_reaction_follows_user_reactions(tmp_path):
    """クマの分は利用者の最後の絵文字に追従し、全部外れたら消える。"""
    mem = _memory(tmp_path)
    mem.toggle_reaction(1, "👍")
    assert mem.load_agent_reactions() == {1: "👍"}
    mem.toggle_reaction(1, "❤️")
    assert mem.load_agent_reactions() == {1: "❤️"}
    mem.toggle_reaction(1, "❤️")  # 最後のものを外すと残りに戻る
    assert mem.load_agent_reactions() == {1: "👍"}
    # 別ファイルに保存され、読み直しても残る
    agent_file = tmp_path / "conversations" / "room1.agent-reactions.json"
    assert json.loads(agent_file.read_text(encoding="utf-8")) == {"1": "👍"}
    assert Memory(tmp_path, "room1").load_agent_reactions() == {1: "👍"}
    mem.toggle_reaction(1, "👍")  # 全部外すとクマの分も消える
    assert mem.load_agent_reactions() == {}
    # 外したとき（利用者の分が無い操作）は付け直されない
    mem.toggle_reaction(0, "🙏")  # 利用者自身の発話にも返る
    assert mem.load_agent_reactions() == {0: "🙏"}


class _CaptureModel:
    """モデルへ渡った発話を記録するスタブ。"""

    def __init__(self):
        self.seen: list[Message] = []

    def chat(self, system_prompt, messages, tools):
        self.seen = list(messages)
        return Message("assistant", "了解です")


def test_reaction_note_reaches_model_context_only(tmp_path):
    """リアクションの注記はモデルへの入力にだけ入り、記録と履歴には残らない。"""
    cfg = Config(data_dir=tmp_path)
    model = _CaptureModel()
    session = Session(cfg, model)
    session.say("やあ")
    session.memory.toggle_reaction(1, "❤️")
    session.say("つづき")
    by_rid = {m.record_index: m for m in model.seen
              if m.record_index is not None}
    assert "（この発話に利用者が❤️を付けた）" in by_rid[1].content
    assert "付けた" not in by_rid[0].content
    # 履歴の発話そのものと会話記録には注記が混ざらない
    assert all("付けた" not in m.content for m in session.history)
    turns = session.memory.load_turns()
    assert all("付けた" not in str(t["content"]) for t in turns)
