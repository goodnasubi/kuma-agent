"""Meta Model API（Muse）のクライアント。

Meta Model API は OpenAI 互換で、土台は Chat Completions 形式:
- Base URL: https://api.meta.ai/v1、POST /chat/completions
- 認証: Authorization: Bearer <MODEL_API_KEY>
- モデル ID: muse-wonder-1.0（Muse Wonder）
- 人格などの誘導は `system` ではなく `developer` ロールで渡す
- 関数ツールは OpenAI 形式（tools / tool_calls / role=tool）
- 送ってはいけないもの: stop、logit_bias、reasoning_effort="none"
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-wonder-1.0"


@dataclass
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
    tool_calls: list[dict] = field(default_factory=list)  # {"id", "name", "args"}
    tool_call_id: str | None = None  # role == "tool" のとき対応する呼び出しの id


def _to_api_message(msg: Message) -> dict:
    if msg.role == "tool":
        return {"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content}
    if msg.role == "assistant" and msg.tool_calls:
        return {
            "role": "assistant",
            "content": msg.content or None,
            "tool_calls": [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {"name": c["name"], "arguments": json.dumps(c.get("args", {}))},
                }
                for c in msg.tool_calls
            ],
        }
    return {"role": msg.role, "content": msg.content}


def _from_api_message(data: dict) -> Message:
    calls = []
    for c in data.get("tool_calls") or []:
        fn = c.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        calls.append({"id": c.get("id", ""), "name": fn.get("name", ""), "args": args})
    return Message(role="assistant", content=data.get("content") or "", tool_calls=calls)


def _to_api_tool(schema: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get("description", ""),
            "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
        },
    }


class ModelClient:
    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=300.0,
            transport=transport,
        )

    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "developer", "content": system_prompt}]
            + [_to_api_message(m) for m in messages],
        }
        if tools:
            payload["tools"] = [_to_api_tool(t) for t in tools]
        resp = self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return _from_api_message(data["choices"][0]["message"])
