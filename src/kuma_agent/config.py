"""設定。環境変数と CLI 引数から組み立てる。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("KUMA_DATA_DIR", "data")))
    provider: str = field(default_factory=lambda: os.environ.get("KUMA_LLM_PROVIDER", "muse"))
    model: str | None = field(default_factory=lambda: os.environ.get("KUMA_LLM_MODEL") or None)
    model_api_key: str = ""

    @property
    def is_cli_provider(self) -> bool:
        return self.provider.endswith("-cli") or self.provider == "claude-code"

    def resolve_api_key(self) -> str:
        """プロバイダに応じた環境変数から API キーを引く。"""
        if self.model_api_key:
            return self.model_api_key
        env_names = {
            "muse": ("META_MODEL_API_KEY", "MODEL_API_KEY"),
            "claude": ("ANTHROPIC_API_KEY",),
            "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        }.get(self.provider, ())
        for name in env_names:
            if os.environ.get(name):
                return os.environ[name]
        return ""
    max_tool_rounds: int = 16

    @property
    def memory_file(self) -> Path:
        return self.data_dir / "MEMORY.md"

    @property
    def conversations_dir(self) -> Path:
        return self.data_dir / "conversations"
