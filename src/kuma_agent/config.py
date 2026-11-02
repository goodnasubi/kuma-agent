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


# 文脈の分け方のモード（詳細は docs/DESIGN.md の「モード」節）:
# - ledger（相棒モード、既定）: 部屋は実質 1 つ（main）に固定し、
#   抱えている用事は用事の一覧で分けて覚える
# - wall（スレッドモード）: 会話を部屋（スレッド）ごとに分ける。用事の一覧は無効
MODES = ("wall", "ledger")
DEFAULT_MODE = "ledger"


def normalize_mode(raw: str | None) -> str:
    """モード名を正規化する。

    旧値 both は後方互換のため ledger に読み替え、それ以外の不正な
    値も既定（ledger）に倒す。
    """
    mode = (raw or "").strip().lower()
    if mode == "both":
        return "ledger"
    return mode if mode in MODES else DEFAULT_MODE


# モードのユーザー向け表示名。内部の識別子（wall / ledger）は
# 設定値としてだけ使い、画面や文面ではこちらの名前で出す。
# 対応付けはこの辞書に集約する
MODE_LABELS: dict[str, str] = {
    "wall": "スレッドモード",
    "ledger": "相棒モード",
}


def mode_label(mode: str | None) -> str:
    """モードの表示名を返す。不正な値は既定モードの表示名に倒す。"""
    return MODE_LABELS[normalize_mode(mode)]


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("KUMA_DATA_DIR", "data")))
    tenant: str = field(default_factory=lambda: os.environ.get("KUMA_TENANT", "default"))
    user: str = field(default_factory=lambda: os.environ.get("KUMA_USER", "default"))
    # 文脈の分け方のモード（wall / ledger）。不正な値と旧値 both は
    # __post_init__ で既定の ledger に倒す
    mode: str = field(default_factory=lambda: os.environ.get("KUMA_MODE", DEFAULT_MODE))
    provider: str = field(default_factory=lambda: os.environ.get("KUMA_LLM_PROVIDER", "muse"))
    model: str | None = field(default_factory=lambda: os.environ.get("KUMA_LLM_MODEL") or None)
    model_api_key: str = ""

    def __post_init__(self) -> None:
        # どの経路で作られてもモードは正規化済みの値にそろえる
        self.mode = normalize_mode(self.mode)

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
    # 記録の保持期限（日数）。部屋の記録と完了した用事は、最終更新から
    # この日数を過ぎたら自動で掃除される（cleanup_expired）。0 で無期限
    retention_days: int = field(
        default_factory=lambda: _env_int("KUMA_RETENTION_DAYS", 90))
    # 能動発信の巡回間隔（秒）。Web サーバーの裏側で定期的に全テナント×
    # 全ユーザーを巡回し、言うべきことがあるユーザーにだけ発言する
    # （proactive）。0 以下で巡回しない
    proactive_interval_seconds: int = field(
        default_factory=lambda: _env_int("KUMA_PROACTIVE_INTERVAL_SECONDS", 300))
    # MCP サーバーへの要求の応答待ち上限（秒）。サーバーがこの時間に
    # 応答しなければ、その道具の呼び出しは失敗として扱う（接続は
    # 捨てて次回に再起動を試みる）。0 以下は既定値として扱う
    mcp_timeout_seconds: int = field(
        default_factory=lambda: _env_int("KUMA_MCP_TIMEOUT_SECONDS", 60))

    @property
    def tenant_data_dir(self) -> Path:
        """このテナントの実効データ置き場。

        既定テナント（"default"）は後方互換のため data_dir をそのまま
        返し、それ以外は data_dir / "tenants" / <テナント名> を返す。
        人格・長期記憶・部屋・用事の記録はすべてこの置き場の下に分かれる。
        """
        return tenant_dir(self.data_dir, self.tenant)

    @property
    def user_data_dir(self) -> Path:
        """このユーザーの実効データ置き場。

        既定ユーザー（"default"）は後方互換のためテナントの置き場を
        そのまま返し、それ以外は
        <テナント置き場> / "users" / <ユーザー名> を返す。
        人格・長期記憶・部屋・用事の記録はすべてこの置き場の下に分かれる。
        """
        return user_dir(self.tenant_data_dir, self.user)

    @property
    def memory_file(self) -> Path:
        return self.user_data_dir / "MEMORY.md"

    @property
    def conversations_dir(self) -> Path:
        return self.user_data_dir / "conversations"
