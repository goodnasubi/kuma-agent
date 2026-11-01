"""長期記憶（MEMORY.md）と会話履歴（JSONL）の読み書き。"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .tasks import TaskBoard

DEFAULT_SESSION_ID = "main"
_MAX_SESSION_ID_LEN = 64

DEFAULT_TENANT = "default"
TENANTS_DIRNAME = "tenants"

DEFAULT_USER = "default"
USERS_DIRNAME = "users"


def _normalize_name(raw: str | None, fallback: str) -> str:
    """名前をファイル名に安全な形へ正規化する（共通処理）。"""
    if not raw:
        return fallback
    chars = []
    for ch in str(raw).strip():
        chars.append(ch if (ch.isalnum() or ch in "-_") else "-")
    name = "".join(chars).strip("-.")[:_MAX_SESSION_ID_LEN].strip("-.")
    return name or fallback


def normalize_session_id(raw: str | None) -> str:
    """セッション ID をファイル名に安全な形へ正規化する。

    英数字（日本語の文字も含む）と "-" "_" はそのまま残し、パス区切りや
    空白などファイル名に使えない文字は "-" に置き換える。空になった
    場合は既定の "main" にする。
    """
    return _normalize_name(raw, DEFAULT_SESSION_ID)


def normalize_tenant_name(raw: str | None) -> str:
    """テナント名をファイル名に安全な形へ正規化する。

    セッション ID と同じ正規化を通し、空になった場合は既定の
    "default" にする。
    """
    return _normalize_name(raw, DEFAULT_TENANT)


def tenant_dir(data_dir: Path, tenant: str | None) -> Path:
    """テナントの実効データ置き場を返す。

    既定テナント（"default"）は後方互換のため data_dir をそのまま
    使い、それ以外は data_dir / "tenants" / <テナント名> を使う。
    """
    name = normalize_tenant_name(tenant)
    if name == DEFAULT_TENANT:
        return data_dir
    return data_dir / TENANTS_DIRNAME / name


def normalize_user_name(raw: str | None) -> str:
    """ユーザー名をファイル名に安全な形へ正規化する。

    テナント名と同じ正規化を通し、空になった場合は既定の
    "default" にする。
    """
    return _normalize_name(raw, DEFAULT_USER)


def user_dir(tenant_data_dir: Path, user: str | None) -> Path:
    """テナントの置き場の中での、ユーザーの実効データ置き場を返す。

    既定ユーザー（"default"）は後方互換のためテナントの置き場を
    そのまま使い、それ以外は
    <テナント置き場> / "users" / <ユーザー名> を使う。
    """
    name = normalize_user_name(user)
    if name == DEFAULT_USER:
        return tenant_data_dir
    return tenant_data_dir / USERS_DIRNAME / name


def list_user_names(tenant_data_dir: Path) -> list[str]:
    """ユーザー名の一覧（既定ユーザー＋保存済みユーザーのディレクトリ名）。"""
    names = [DEFAULT_USER]
    root = tenant_data_dir / USERS_DIRNAME
    if root.is_dir():
        names += sorted(p.name for p in root.iterdir() if p.is_dir())
    return names


def list_tenant_names(data_dir: Path) -> list[str]:
    """テナント名の一覧（既定テナント＋保存済みテナントのディレクトリ名）。"""
    names = [DEFAULT_TENANT]
    root = data_dir / TENANTS_DIRNAME
    if root.is_dir():
        names += sorted(p.name for p in root.iterdir() if p.is_dir())
    return names


def list_session_ids(data_dir: Path) -> list[str]:
    """保存済みセッションの ID 一覧（conversations/*.jsonl のファイル名）。"""
    conv = data_dir / "conversations"
    if not conv.is_dir():
        return []
    return sorted(p.stem for p in conv.glob("*.jsonl"))


def iter_user_dirs(data_dir: Path) -> Iterator[Path]:
    """全テナント×ユーザーの実効データ置き場を列挙する。

    既定テナント×既定ユーザーの置き場（data_dir そのもの）から、
    保存済みのテナント・ユーザーの置き場までをすべて返す。
    """
    for tenant in list_tenant_names(data_dir):
        tdir = tenant_dir(data_dir, tenant)
        for user in list_user_names(tdir):
            yield user_dir(tdir, user)


@dataclass
class CleanupResult:
    """クリーンアップで削除した件数の内訳。"""
    sessions_removed: int = 0
    tasks_removed: int = 0

    @property
    def total(self) -> int:
        return self.sessions_removed + self.tasks_removed


def cleanup_expired(data_dir: Path, retention_days: int,
                    now: datetime | None = None,
                    dry_run: bool = False) -> CleanupResult:
    """保持期限を過ぎた記録を全テナント×ユーザー置き場から掃除する。

    対象は次の 2 つだけで、長期記憶（MEMORY.md）と人格ファイルは
    消さない:
    - 部屋の記録ファイル（conversations/*.jsonl）のうち、最終更新が
      期限より古いもの（ファイルごと削除）
    - 台帳（tasks.json）のうち、完了（done）してから更新が期限より
      古い用事（レコードだけ除去し、ファイルは残す）

    retention_days が 0 以下なら無期限として何もしない。置き場が
    無かったり読めなかったりしても例外で落とさず、その分を飛ばす。
    dry_run では削除せず、対象の件数だけを数えて返す。
    """
    result = CleanupResult()
    if retention_days <= 0:
        return result
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)
    cutoff_ts = cutoff.timestamp()
    for udir in iter_user_dirs(data_dir):
        conv = udir / "conversations"
        if conv.is_dir():
            for path in sorted(conv.glob("*.jsonl")):
                try:
                    if path.stat().st_mtime >= cutoff_ts:
                        continue
                    if not dry_run:
                        path.unlink()
                    result.sessions_removed += 1
                except OSError:
                    continue  # 読めない・消せないファイルは飛ばす
        if (udir / "tasks.json").is_file():
            try:
                board = TaskBoard(udir)
                result.tasks_removed += board.prune_done_before(
                    cutoff, dry_run=dry_run)
            except Exception:
                continue  # 壊れた台帳などは飛ばす
    return result


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

    def append_turn(self, role: str, content: str,
                    quote: str | None = None) -> None:
        """会話履歴に 1 発話を追記する（セッションごとの JSONL）。

        quote は引用返信の引用元の本文。無い発話ではキー自体を
        付けない（旧来の記録形式と互換に保つ）。
        """
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now().isoformat(timespec="seconds"),
                  "session": self.session_id, "role": role, "content": content}
        if quote:
            record["quote"] = quote
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
