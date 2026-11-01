"""長期記憶（MEMORY.md）と会話履歴（JSONL）の読み書き。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DEFAULT_SESSION_ID = "main"
_MAX_SESSION_ID_LEN = 64


def normalize_session_id(raw: str | None) -> str:
    """セッション ID をファイル名に安全な形へ正規化する。

    英数字（日本語の文字も含む）と "-" "_" はそのまま残し、パス区切りや
    空白などファイル名に使えない文字は "-" に置き換える。空になった
    場合は既定の "main" にする。
    """
    if not raw:
        return DEFAULT_SESSION_ID
    chars = []
    for ch in str(raw).strip():
        chars.append(ch if (ch.isalnum() or ch in "-_") else "-")
    name = "".join(chars).strip("-.")[:_MAX_SESSION_ID_LEN].strip("-.")
    return name or DEFAULT_SESSION_ID


def list_session_ids(data_dir: Path) -> list[str]:
    """保存済みセッションの ID 一覧（conversations/*.jsonl のファイル名）。"""
    conv = data_dir / "conversations"
    if not conv.is_dir():
        return []
    return sorted(p.stem for p in conv.glob("*.jsonl"))


class Memory:
    def __init__(self, data_dir: Path, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.data_dir = data_dir
        self.session_id = normalize_session_id(session_id)
        self.memory_file = data_dir / "MEMORY.md"
        self.conversations_dir = data_dir / "conversations"

    @property
    def conversation_file(self) -> Path:
        """このセッションの会話記録ファイル。"""
        return self.conversations_dir / f"{self.session_id}.jsonl"

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
        """会話履歴に 1 発話を追記する（セッションごとの JSONL）。"""
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now().isoformat(timespec="seconds"),
                  "session": self.session_id, "role": role, "content": content}
        with self.conversation_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_summary(self, content: str) -> None:
        """圧縮で作った要約を記録する（role は "summary"）。"""
        self.append_turn("summary", content)

    def load_turns(self) -> list[dict]:
        """このセッションの過去の記録を古い順に読み戻す。

        壊れた行は読み飛ばす。レコードは {"role", "content", ...} の辞書で、
        role には "user" / "assistant" / "summary" が入る。
        """
        if not self.conversation_file.exists():
            return []
        records: list[dict] = []
        for line in self.conversation_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and "role" in record:
                records.append(record)
        return records
