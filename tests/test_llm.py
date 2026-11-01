import json

import httpx

from kuma_agent.llm import Message, ModelClient
from kuma_agent.session import Session
from kuma_agent.config import Config


def _client(handler) -> ModelClient:
    return ModelClient("test-key", transport=httpx.MockTransport(handler))


def test_chat_plain_reply_and_request_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "やあ"}}],
        })

    reply = _client(handler).chat("人格テキスト", [Message("user", "こんにちは")], [])
    assert reply.content == "やあ"
    assert reply.tool_calls == []
    assert seen["model"] == "muse-wonder-1.0"
    assert seen["messages"][0] == {"role": "developer", "content": "人格テキスト"}
    assert seen["messages"][1] == {"role": "user", "content": "こんにちは"}
    assert "tools" not in seen


def test_chat_tool_call_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "now", "arguments": "{}"}},
            ]}}],
        })

    tools = [{"name": "now", "description": "時刻", "parameters": {"type": "object", "properties": {}}}]
    reply = _client(handler).chat("sys", [Message("user", "今何時？")], tools)
    assert reply.tool_calls == [{"id": "call_1", "name": "now", "args": {}}]


class _FakeModel:
    """1 回目は now ツールを呼び、2 回目は本文を返す偽モデル。"""

    def __init__(self):
        self.rounds = 0

    def chat(self, system_prompt, messages, tools):
        self.rounds += 1
        if self.rounds == 1:
            assert tools  # 道具スキーマが渡っている
            return Message("assistant", "", tool_calls=[{"id": "c1", "name": "now", "args": {}}])
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert tool_msgs and tool_msgs[0].tool_call_id == "c1"
        return Message("assistant", f"今は {tool_msgs[0].content} です")


def test_session_tool_loop(tmp_path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _FakeModel())
    out = session.say("今何時？")
    assert out.startswith("今は ")
    log = (tmp_path / "conversations").glob("*.jsonl")
    assert any(log)


def test_tool_message_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    history = [
        Message("user", "時刻は？"),
        Message("assistant", "", tool_calls=[{"id": "c1", "name": "now", "args": {}}]),
        Message("tool", "2026-11-01T20:00:00", tool_call_id="c1"),
    ]
    _client(handler).chat("sys", history, [])
    msgs = seen["messages"]
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "now"
    assert msgs[3] == {"role": "tool", "tool_call_id": "c1", "content": "2026-11-01T20:00:00"}
