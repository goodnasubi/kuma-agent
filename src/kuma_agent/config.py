"""設定。環境変数と CLI 引数から組み立てる。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .memory import tenant_dir, user_dir


def _env_int(name: str, default: int) -> int:
    """環境変数を整数として読む。不正な値なら既定値にする。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("KUMA_DATA_DIR", "data")))
    tenant: str = field(default_factory=lambda: os.environ.get("KUMA_TENANT", "default"))
    user: str = field(default_factory=lambda: os.environ.get("KUMA_USER", "default"))
    provider: str = field(default_factory=lambda: os.environ.get("KUMA_LLM_PROVIDER", "muse"))
    model: str | None = field(default_factory=lambda: os.environ.get("KUMA_LLM_MODEL") or None)
    model_api_key: str = ""

    @property
    def is_cli_provider(self) -> bool:
        return self.provider in ("claude-code", "gemini-cli", "codex")

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
    # 会話の圧縮（コンパクション）: user/assistant の発話数が compact_after を
    # 超えたら古い発話を要約に置き換える。0 で無効。直近 compact_keep 件は残す
    compact_after: int = field(
        default_factory=lambda: _env_int("KUMA_COMPACT_AFTER_MESSAGES", 40))
    compact_keep: int = field(
        default_factory=lambda: _env_int("KUMA_COMPACT_KEEP_MESSAGES", 10))

    @property
    def tenant_data_dir(self) -> Path:
        """このテナントの実効データ置き場。

        既定テナント（"default"）は後方互換のため data_dir をそのまま
        返し、それ以外は data_dir / "tenants" / <テナント名> を返す。
        人格・長期記憶・部屋・台帳はすべてこの置き場の下に分かれる。
        """
        return tenant_dir(self.data_dir, self.tenant)

    @property
    def user_data_dir(self) -> Path:
        """このユーザーの実効データ置き場。

        既定ユーザー（"default"）は後方互換のためテナントの置き場を
        そのまま返し、それ以外は
        <テナント置き場> / "users" / <ユーザー名> を返す。
        人格・長期記憶・部屋・台帳はすべてこの置き場の下に分かれる。
        """
        return user_dir(self.tenant_data_dir, self.user)

    @property
    def memory_file(self) -> Path:
        return self.user_data_dir / "MEMORY.md"

    @property
    def conversations_dir(self) -> Path:
        return self.user_data_dir / "conversations"
