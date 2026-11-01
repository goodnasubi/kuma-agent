"""Meta Model API（Muse）のクライアント。

TODO: 対話と関数呼び出しの形式は Meta Model API の仕様に合わせて実装する。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    tool_name: str | None = None


class ModelClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        """1 回の応答（道具呼び出しを含む場合がある）を返す。未実装。"""
        raise NotImplementedError("Meta Model API の対話呼び出しは未実装")
