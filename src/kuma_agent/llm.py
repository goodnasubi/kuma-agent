"""LLM プロバイダのクライアント群。

内部の Message 表現は共通で、プロバイダごとに API 形式へ変換する。

- muse:   Meta Model API（OpenAI 互換 Chat Completions、developer ロール）
- claude: Anthropic Messages API（system 分離、tool_use / tool_result）
- gemini: Google Gemini API（generateContent、functionCall / functionResponse）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

import httpx

MAX_TOKENS = 8192


@dataclass
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
    tool_calls: list[dict] = field(default_factory=list)  # {"id", "name", "args"}
    tool_call_id: str | None = None  # role == "tool" のとき対応する呼び出しの id
    tool_name: str | None = None  # role == "tool" のとき道具名（Gemini は名前で対応付ける）


class ChatModel(Protocol):
    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        ...


def _http(base_url: str, headers: dict, transport, timeout: float = 300.0) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers=headers, timeout=timeout, transport=transport)


# ---------------------------------------------------------------- Muse

class MetaModelClient:
    """Meta Model API（Muse）。OpenAI 互換。"""

    BASE_URL = "https://api.meta.ai/v1"
    DEFAULT_MODEL = "muse-wonder-1.0"

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None, transport=None) -> None:
        self.model = model or self.DEFAULT_MODEL
        self._client = _http(base_url or self.BASE_URL,
                             {"Authorization": f"Bearer {api_key}"}, transport)

    @staticmethod
    def _to_api(msg: Message) -> dict:
        if msg.role == "tool":
            return {"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content}
        if msg.role == "assistant" and msg.tool_calls:
            return {
                "role": "assistant",
                "content": msg.content or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": json.dumps(c.get("args", {}))}}
                    for c in msg.tool_calls
                ],
            }
        return {"role": msg.role, "content": msg.content}

    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "developer", "content": system_prompt}]
            + [self._to_api(m) for m in messages],
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {
                    "name": t["name"], "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}})}}
                for t in tools
            ]
        resp = self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()["choices"][0]["message"]
        calls = []
        for c in data.get("tool_calls") or []:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": c.get("id", ""), "name": fn.get("name", ""), "args": args})
        return Message("assistant", data.get("content") or "", tool_calls=calls)


# ---------------------------------------------------------------- Claude

class AnthropicClient:
    """Anthropic Messages API（Claude）。"""

    BASE_URL = "https://api.anthropic.com"
    DEFAULT_MODEL = "claude-sonnet-4-5"
    VERSION = "2023-06-01"

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None, transport=None) -> None:
        self.model = model or self.DEFAULT_MODEL
        self._client = _http(base_url or self.BASE_URL,
                             {"x-api-key": api_key, "anthropic-version": self.VERSION},
                             transport)

    @staticmethod
    def _to_api(messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        pending_tools: list[dict] = []  # 連続する道具結果は 1 つの user 発話にまとめる

        def flush_tools() -> None:
            if pending_tools:
                out.append({"role": "user", "content": list(pending_tools)})
                pending_tools.clear()

        for msg in messages:
            if msg.role == "tool":
                pending_tools.append({
                    "type": "tool_result", "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                })
                continue
            flush_tools()
            if msg.role == "assistant" and msg.tool_calls:
                blocks: list[dict] = []
                if msg.content:
                    blocks.append({"type": "text", "text": msg.content})
                blocks += [
                    {"type": "tool_use", "id": c["id"], "name": c["name"],
                     "input": c.get("args", {})}
                    for c in msg.tool_calls
                ]
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": msg.role, "content": msg.content})
        flush_tools()
        return out

    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        payload: dict = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system_prompt,
            "messages": self._to_api(messages),
        }
        if tools:
            payload["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t.get("parameters", {"type": "object", "properties": {}})}
                for t in tools
            ]
        resp = self._client.post("/v1/messages", json=payload)
        resp.raise_for_status()
        text, calls = "", []
        for block in resp.json().get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                calls.append({"id": block.get("id", ""), "name": block.get("name", ""),
                              "args": block.get("input", {})})
        return Message("assistant", text, tool_calls=calls)


# ---------------------------------------------------------------- Gemini

class GeminiClient:
    """Google Gemini API（generateContent）。道具は名前で対応付ける。"""

    BASE_URL = "https://generativelanguage.googleapis.com"
    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None, transport=None) -> None:
        self.model = model or self.DEFAULT_MODEL
        self._client = _http(base_url or self.BASE_URL,
                             {"x-goog-api-key": api_key}, transport)

    @staticmethod
    def _to_api(messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        pending: list[dict] = []  # 連続する道具応答は 1 つの user 発話にまとめる

        def flush() -> None:
            if pending:
                out.append({"role": "user", "parts": list(pending)})
                pending.clear()

        for msg in messages:
            if msg.role == "tool":
                pending.append({"functionResponse": {
                    "name": msg.tool_name or "",
                    "response": {"result": msg.content},
                }})
                continue
            flush()
            if msg.role == "assistant":
                parts: list[dict] = []
                if msg.content:
                    parts.append({"text": msg.content})
                parts += [{"functionCall": {"name": c["name"], "args": c.get("args", {})}}
                          for c in msg.tool_calls]
                out.append({"role": "model", "parts": parts})
            else:
                out.append({"role": "user", "parts": [{"text": msg.content}]})
        flush()
        return out

    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        payload: dict = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": self._to_api(messages),
        }
        if tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": t["name"], "description": t.get("description", ""),
                 "parameters": t.get("parameters", {"type": "object", "properties": {}})}
                for t in tools
            ]}]
        resp = self._client.post(f"/v1beta/models/{self.model}:generateContent", json=payload)
        resp.raise_for_status()
        text, calls = "", []
        parts = resp.json()["candidates"][0]["content"].get("parts", [])
        for i, part in enumerate(parts):
            if "text" in part:
                text += part["text"]
            elif "functionCall" in part:
                fc = part["functionCall"]
                calls.append({"id": f"gemini-{i}", "name": fc.get("name", ""),
                              "args": fc.get("args", {})})
        return Message("assistant", text, tool_calls=calls)


# ---------------------------------------------------------------- factory

API_PROVIDERS = ("muse", "claude", "gemini")

_API_CLIENTS = {
    "muse": MetaModelClient,
    "claude": AnthropicClient,
    "gemini": GeminiClient,
}


def create_client(provider: str, api_key: str = "", model: str | None = None, **kwargs) -> ChatModel:
    """provider 名からバックエンドを作る。

    API 系（muse / claude / gemini）は api_key を使い、CLI 系
    （claude-code / gemini-cli）は外部 CLI を実行する（キー不要）。
    """
    from .agent_cli import CLI_PROVIDERS, create_cli_client

    if provider in CLI_PROVIDERS:
        return create_cli_client(provider, model=model, **kwargs)
    cls = _API_CLIENTS.get(provider)
    if cls is None:
        raise ValueError(f"未知のプロバイダ: {provider}")
    return cls(api_key, model=model, **kwargs)


PROVIDERS = API_PROVIDERS + ("claude-code", "gemini-cli")
