"""設定。環境変数と CLI 引数から組み立てる。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("KUMA_DATA_DIR", "data")))
    model: str = "muse-wonder-1.0"
    model_api_key: str = field(default_factory=lambda: os.environ.get("META_MODEL_API_KEY") or os.environ.get("MODEL_API_KEY", ""))
    max_tool_rounds: int = 16

    @property
    def memory_file(self) -> Path:
        return self.data_dir / "MEMORY.md"

    @property
    def conversations_dir(self) -> Path:
        return self.data_dir / "conversations"
