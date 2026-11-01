import json

import httpx

from kuma_agent.llm import AnthropicClient, GeminiClient, Message, create_client


def test_factory_and_unknown_provider():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    assert isinstance(create_client("claude", "k", transport=transport), AnthropicClient)
    assert isinstance(create_client("gemini", "k", transport=transport), GeminiClient)
    try:
        create_client("bogus", "k")
        raise AssertionError("should have raised")
    except ValueError:
        pass


def test_claude_request_and_tool_use_parsing():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [
            {"type": "text", "text": "調べます"},
            {"type": "tool_use", "id": "toolu_1", "name": "now", "input": {}},
        ]})

    client = AnthropicClient("k", transport=httpx.MockTransport(handler))
    tools = [{"name": "now", "description": "時刻",
              "parameters": {"type": "object", "properties": {}}}]
    reply = client.chat("人格", [Message("user", "今何時？")], tools)
    assert reply.content == "調べます"
    assert reply.tool_calls == [{"id": "toolu_1", "name": "now", "args": {}}]
    assert seen["system"] == "人格"
    assert seen["model"] == "claude-sonnet-4-5"
    assert seen["tools"][0]["input_schema"] == {"type": "object", "properties": {}}


def test_claude_tool_result_grouping():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = AnthropicClient("k", transport=httpx.MockTransport(handler))
    history = [
        Message("user", "時刻は？"),
        Message("assistant", "", tool_calls=[{"id": "t1", "name": "now", "args": {}},
                                             {"id": "t2", "name": "now", "args": {}}]),
        Message("tool", "10:00", tool_call_id="t1", tool_name="now"),
        Message("tool", "10:00", tool_call_id="t2", tool_name="now"),
    ]
    client.chat("sys", history, [])
    msgs = seen["messages"]
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["t1", "t2"]


def test_gemini_request_and_function_call_parsing():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        seen["path"] = request.url.path
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "now", "args": {}}},
        ]}}]})

    client = GeminiClient("k", transport=httpx.MockTransport(handler))
    tools = [{"name": "now", "description": "時刻",
              "parameters": {"type": "object", "properties": {}}}]
    reply = client.chat("人格", [Message("user", "今何時？")], tools)
    assert reply.tool_calls[0]["name"] == "now"
    assert seen["path"].endswith("/v1beta/models/gemini-2.5-flash:generateContent")
    assert seen["systemInstruction"]["parts"][0]["text"] == "人格"
    assert seen["tools"][0]["functionDeclarations"][0]["name"] == "now"


def test_gemini_function_response_shape():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    client = GeminiClient("k", transport=httpx.MockTransport(handler))
    history = [
        Message("user", "時刻は？"),
        Message("assistant", "", tool_calls=[{"id": "gemini-0", "name": "now", "args": {}}]),
        Message("tool", "10:00", tool_call_id="gemini-0", tool_name="now"),
    ]
    client.chat("sys", history, [])
    contents = seen["contents"]
    assert contents[1]["role"] == "model"
    assert contents[1]["parts"][0]["functionCall"]["name"] == "now"
    assert contents[2]["parts"][0]["functionResponse"] == {
        "name": "now", "response": {"result": "10:00"}}
