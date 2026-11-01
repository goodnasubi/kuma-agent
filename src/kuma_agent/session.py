"""1 回の対話セッション。履歴管理と道具呼び出しの循環。"""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .llm import Message, ModelClient
from .memory import Memory
from .persona import load_persona
from .tools import ToolBox


class Session:
    def __init__(self, cfg: Config, model: ModelClient) -> None:
        self.cfg = cfg
        self.model = model
        self.memory = Memory(cfg.data_dir)
        self.tools = ToolBox(Path.cwd())
        self.history: list[Message] = []
        memory_text = self.memory.load_long_term()
        self.system_prompt = load_persona(cfg.data_dir)
        if memory_text:
            self.system_prompt += f"\n\n# 長期記憶\n{memory_text}"

    def say(self, user_text: str) -> str:
        """利用者の発話を受け、道具の循環を経て最終応答を返す。"""
        self.history.append(Message("user", user_text))
        self.memory.append_turn("user", user_text)
        for _ in range(self.cfg.max_tool_rounds):
            reply = self.model.chat(self.system_prompt, self.history, self.tools.schemas())
            self.history.append(reply)
            if not reply.tool_calls:
                self.memory.append_turn("assistant", reply.content)
                return reply.content
            for call in reply.tool_calls:
                result = self.tools.call(call["name"], call.get("args", {}))
                self.history.append(Message("tool", result, tool_name=call["name"]))
        return "（道具の呼び出しが上限に達しました）"
