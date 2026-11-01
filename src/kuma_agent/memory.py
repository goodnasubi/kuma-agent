"""長期記憶（MEMORY.md）と会話履歴（JSONL）の読み書き。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class Memory:
    def __init__(self, data_dir: Path) -> None:
        self.memory_file = data_dir / "MEMORY.md"
        self.conversations_dir = data_dir / "conversations"

    def load_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def remember(self, fact: str) -> None:
        """長期記憶に 1 行追記する。"""
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        with self.memory_file.open("a", encoding="utf-8") as f:
            f.write(f"- {fact}\n")

    def search(self, keyword: str) -> list[str]:
        """長期記憶からキーワードを含む行を返す（当面は単純一致）。"""
        text = self.load_long_term()
        return [line for line in text.splitlines() if keyword in line]

    def append_turn(self, role: str, content: str) -> None:
        """会話履歴に 1 発話を追記する（日付ごとの JSONL）。"""
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        path = self.conversations_dir / f"{datetime.now():%Y-%m-%d}.jsonl"
        record = {"ts": datetime.now().isoformat(timespec="seconds"), "role": role, "content": content}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
