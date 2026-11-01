"""引用返信（quote）の直列化・記録・UI 往復のテスト。"""
import json
import threading
import urllib.request
from pathlib import Path

import httpx

from kuma_agent.agent_cli import _render_prompt
from kuma_agent.config import Config
from kuma_agent.llm import (
    AnthropicClient,
    GeminiClient,
    Message,
    MetaModelClient,
    quote_block,
    truncate_quote,
)
from kuma_agent.memory import Memory
from kuma_agent.session import Session
from kuma_agent.tui import (
    format_log,
    numbered_messages,
    parse_quote_command,
    quote_text_for_number,
)
from kuma_agent.web import build_server, render_page


def _capture(client_cls, response_json: dict) -> tuple[dict, object]:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=response_json)

    client = client_cls("k", transport=httpx.MockTransport(handler))
    return seen, client


def test_quote_is_truncated_only_for_model_input():
    quote = "あ" * 250
    shortened = truncate_quote(quote)
    assert len(shortened) == 200
    assert shortened.endswith("…")
    assert quote_block("前の話") == "（「前の話」への返信）"


def test_muse_quote_precedes_body():
    seen, client = _capture(
        MetaModelClient,
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
    )
    client.chat("sys", [Message("user", "それについて", quote="元の発話")], [])
    assert seen["messages"][1]["content"] == "（「元の発話」への返信）\nそれについて"


def test_anthropic_quote_precedes_body():
    seen, client = _capture(
        AnthropicClient,
        {"content": [{"type": "text", "text": "ok"}]},
    )
    client.chat("sys", [Message("user", "それについて", quote="元の発話")], [])
    assert seen["messages"][0]["content"] == "（「元の発話」への返信）\nそれについて"


def test_gemini_quote_precedes_body():
    seen, client = _capture(
        GeminiClient,
        {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
    )
    client.chat("sys", [Message("user", "それについて", quote="元の発話")], [])
    assert seen["contents"][0]["parts"][0] == {
        "text": "（「元の発話」への返信）\nそれについて"}


def test_cli_prompt_contains_quote_block():
    prompt = _render_prompt(
        "人格", [Message("user", "それについて", quote="元の発話")])
    assert "利用者: （「元の発話」への返信）\nそれについて" in prompt


class _RecordingModel:
    def __init__(self) -> None:
        self.seen: list[Message] = []

    def chat(self, system_prompt, messages, tools):
        self.seen = list(messages)
        return Message("assistant", "分かりました")


def test_session_records_and_restores_quote(tmp_path: Path):
    model = _RecordingModel()
    cfg = Config(data_dir=tmp_path, mode="wall")  # 名前付き部屋の検証
    session = Session(cfg, model, session_id="room")
    session.say("その続き", quote="前の発話")

    assert model.seen[-1].quote == "前の発話"
    records = Memory(tmp_path, "room").load_turns()
    user_record = next(r for r in records if r["role"] == "user")
    assert user_record["quote"] == "前の発話"

    resumed = Session(cfg, _RecordingModel(), session_id="room")
    restored_user = next(m for m in resumed.history if m.role == "user")
    assert restored_user.quote == "前の発話"


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


def test_web_quote_roundtrip_and_page_controls(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        req = urllib.request.Request(
            base + "/api/chat",
            data=json.dumps({
                "message": "その続き",
                "quote": "引用元の発話",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            assert json.loads(resp.read().decode("utf-8")) == {
                "reply": "返答: その続き"}
        with urllib.request.urlopen(base + "/api/history", timeout=10) as resp:
            messages = json.loads(resp.read().decode("utf-8"))["messages"]
        user = next(m for m in messages if m["role"] == "user")
        assert user["quote"] == "引用元の発話"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    page = render_page("muse")
    assert 'id="quoteBox"' in page and 'id="clearQuote"' in page
    assert "quote-btn" in page and "pendingQuote" in page


def test_tui_quote_numbering_and_lookup():
    messages = [
        Message("user", "一つ目"),
        Message("assistant", "", tool_calls=[{"id": "c1", "name": "now", "args": {}}]),
        Message("tool", "結果", tool_call_id="c1", tool_name="now"),
        Message("assistant", "二つ目"),
        Message("user", "三つ目", quote="一つ目"),
    ]
    numbered = numbered_messages(messages)
    assert [(num, msg.content) for num, msg in numbered] == [
        (1, "一つ目"), (2, "二つ目"), (3, "三つ目")]
    assert quote_text_for_number(messages, 2) == "二つ目"
    assert quote_text_for_number(messages, 99) is None

    lines = format_log(messages, width=60)
    assert lines[0].startswith("[1] あなた: 一つ目")
    assert any("（「一つ目」への返信）" in line for line in lines)


def test_parse_quote_command():
    assert parse_quote_command("/quote 3") == 3
    assert parse_quote_command("  /quote  12 ") == 12
    assert parse_quote_command("/quote") == 0
    assert parse_quote_command("/quote abc") == 0
    assert parse_quote_command("/quote 0") == 0
    assert parse_quote_command("/quotefoo") is None
    assert parse_quote_command("普通の発話") is None
