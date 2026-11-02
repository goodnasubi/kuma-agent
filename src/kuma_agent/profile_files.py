"""人格・記憶ファイルの読み書き（Web UI の編集画面用）。

対象はユーザーの置き場にある 4 ファイルだけ: 人格ファイル
（SOUL.md / IDENTITY.md / USER.md）と長期記憶（MEMORY.md）。それ
以外の名前はホワイトリストで拒否し、読み書きは必ずその置き場の
中だけに閉じる。

保存は安全側に倒す: 既存の内容は「ファイル名.bak-<日時>」へ控えを
取ってから、一時ファイルへ書いて置き換える（原子的な書き換え）。
本文にはサイズ上限（MAX_PROFILE_BYTES）があり、超過は ValueError
にする（呼び出し側で 400 として返す）。

反映タイミング: 人格と長期記憶は Session の生成時に読み込まれる
（persona.load_persona と Memory.load_long_term）。そのため保存
した内容が効くのは、その後に新しく作られるセッション（会話）から
で、すでに開いているセッションの人格・記憶は途中で変わらない。
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .persona import PERSONA_FILES

# 編集できるファイルはこの 4 つだけ（人格ファイル＋長期記憶）
PROFILE_FILES = (*PERSONA_FILES, "MEMORY.md")

# 本文のサイズ上限（バイト数、UTF-8 換算）
MAX_PROFILE_BYTES = 200 * 1024


def _profile_path(user_data_dir: Path, name: str) -> Path:
    """ホワイトリストのファイル名を置き場内のパスにする。

    リストにない名前（区切り文字や .. を含むものも含む）は
    ValueError にする。名前はファイル名そのものと完全一致で
    照合するため、置き場の外を指すことはない。
    """
    if name not in PROFILE_FILES:
        raise ValueError(f"編集できるファイルではありません: {name}")
    return user_data_dir / name


def read_profile_file(user_data_dir: Path, name: str) -> str:
    """人格・記憶ファイルの本文を返す。ファイルが無ければ空文字列。

    対象外の名前は ValueError。
    """
    path = _profile_path(user_data_dir, name)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def save_profile_file(user_data_dir: Path, name: str, content: str,
                      now: datetime | None = None) -> Path | None:
    """人格・記憶ファイルへ本文を保存する。

    既存ファイルがある場合は、書き換える前にその内容を
    「ファイル名.bak-<日時>」へ控えとして残し、そのパスを返す
    （新規作成の場合は控えは無く None を返す）。書き換えは一時
    ファイルへ書いてから os.replace で置き換えるため、途中で
    止まっても元のファイルが壊れた状態にはならない。

    対象外の名前と、サイズ上限を超える本文は ValueError。
    """
    path = _profile_path(user_data_dir, name)
    if len(content.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError(
            f"本文が大きすぎます（上限 {MAX_PROFILE_BYTES} バイト）: {name}")
    backup: Path | None = None
    if path.exists():
        backup = _backup_path(path, now)
        backup.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return backup


def _backup_path(path: Path, now: datetime | None = None) -> Path:
    """控えの置き場を決める（memory_tidy と同じ「.bak-<日時>」形式）。

    同じ日時の控えが既にある場合は連番を付けて上書きを避ける。
    """
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    candidate = path.parent / f"{path.name}.bak-{stamp}"
    counter = 1
    while candidate.exists():
        candidate = path.parent / f"{path.name}.bak-{stamp}-{counter}"
        counter += 1
    return candidate
