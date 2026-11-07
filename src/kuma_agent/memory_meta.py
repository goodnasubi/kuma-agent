"""長期記憶の項目の属性（memory_meta.json、AM-2）。

MEMORY.md の項目ごとに「覚えた日（since）」と「期限（until、
任意）」を、本文とは別のファイルに持つ仕組み（詳細は
docs/DESIGN.md の「長期記憶の属性と棚卸し」節）:

- 置き場はユーザーの置き場の memory_meta.json。本文
  （MEMORY.md）は今までどおり人が読めて手で直せる形を保ち、
  属性はここにだけ置く
- 項目との照合は、整理（memory_tidy）の重複除去と同じ実質
  判定キー（行頭の記号と空白・前後の記号を落とした本文）で
  行う。利用者が MEMORY.md を手で編集して項目が変わると、
  古い属性は対応が取れなくなる（孤児）。孤児は整理の適用時と
  棚卸し（memory_audit）の際に掃除する
- 属性が無い項目は「長く残す情報」として扱う（安全側）。
  ファイルが無い・壊れている場合も、属性なしとして壊れずに
  動く（棚卸し側の判定に使うのは since / until だけで、
  確からしさのような数値は持たない）
- since は「覚えた日」。ただし会話で改めて触れられたり
  （remember の再記録・自動抽出での再言及）、棚卸しの確認に
  利用者が答えたことが記録される場合は、その日に更新される
  （AM-3 の「確認済みの印」を兼ねる）
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterable

META_FILENAME = "memory_meta.json"


def _segment_key_of(text: str) -> str:
    """項目本文から照合キーを作る（memory_tidy の実質判定キー）。

    循環 import を避けるため、呼び出し時点に取り込む。
    """
    from .memory_tidy import _segment_key
    return _segment_key(text)


def _valid_date(value: object) -> str | None:
    """YYYY-MM-DD として解釈できる文字列ならそのまま、 else None。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value if parsed.strftime("%Y-%m-%d") == value else None


def load_memory_meta(user_data_dir: Path) -> dict[str, dict[str, str]]:
    """memory_meta.json を {照合キー: {"since": ..., "until": ...}} で読む。

    ファイルが無い・壊れている場合は空（属性なし）。since が
    日付として読めない項目は落とし、until が読めない場合は
    until だけを落とす（期限なし＝長く残す情報として扱う）。
    """
    try:
        raw = json.loads(
            (user_data_dir / META_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), dict):
        return {}
    meta: dict[str, dict[str, str]] = {}
    for key, value in raw["items"].items():
        if not isinstance(key, str) or not key or not isinstance(value, dict):
            continue
        since = _valid_date(value.get("since"))
        if since is None:
            continue
        entry = {"since": since}
        until = _valid_date(value.get("until"))
        if until is not None:
            entry["until"] = until
        meta[key] = entry
    return meta


def save_memory_meta(user_data_dir: Path,
                     meta: dict[str, dict[str, str]]) -> None:
    """memory_meta.json を書き直す（キーは照合キー）。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"items": meta}, ensure_ascii=False, sort_keys=True)
    (user_data_dir / META_FILENAME).write_text(text + "\n", encoding="utf-8")


def meta_for(meta: dict[str, dict[str, str]], segment_text: str,
             ) -> dict[str, str] | None:
    """読み込み済みの属性表から、項目本文に対応する属性を引く。"""
    return meta.get(_segment_key_of(segment_text))


def record_memory_items(user_data_dir: Path,
                        items: Iterable[tuple[str, str | None]],
                        now: datetime | None = None) -> int:
    """項目が増えた経路から、属性を記録・更新する。

    items は (項目の本文, 期限 "YYYY-MM-DD" または None) の列。
    すでに属性がある項目は since を今日に更新する（改めて
    触れられた＝確認された扱い）。期限が渡された場合だけ until
    を上書きし、渡されなければ既存の期限はそのまま残す。
    記録した件数を返す。項目が無ければファイルには触れない。
    """
    pairs = [(text, until) for text, until in items
             if _segment_key_of(text)]
    if not pairs:
        return 0
    now = now or datetime.now()
    today = now.date().isoformat()
    meta = load_memory_meta(user_data_dir)
    for text, until in pairs:
        key = _segment_key_of(text)
        entry = dict(meta.get(key) or {})
        entry["since"] = today
        valid_until = _valid_date(until)
        if valid_until is not None:
            entry["until"] = valid_until
        meta[key] = entry
    save_memory_meta(user_data_dir, meta)
    return len(pairs)


def forget_memory_items(user_data_dir: Path,
                        texts: Iterable[str]) -> int:
    """項目本文に対応する属性を取り除く（項目自体を外したとき用）。

    対応する属性が無ければ何もしない。取り除いた件数を返す。
    """
    keys = {_segment_key_of(text) for text in texts}
    keys.discard("")
    if not keys:
        return 0
    meta = load_memory_meta(user_data_dir)
    removed = 0
    for key in keys:
        if meta.pop(key, None) is not None:
            removed += 1
    if removed:
        save_memory_meta(user_data_dir, meta)
    return removed


def prune_memory_meta(user_data_dir: Path, memory_text: str) -> int:
    """現在の MEMORY.md 本文に無い項目の属性（孤児）を掃除する。

    利用者の手編集や整理の書き換えで項目が消えた場合に、
    対応の取れない属性が残り続けないようにする。属性ファイル
    が無い・孤児が無い場合はファイルに触れない。掃除した
    件数を返す。
    """
    from .memory import split_memory_segments

    meta = load_memory_meta(user_data_dir)
    if not meta:
        return 0
    live = {_segment_key_of(seg.text)
            for seg in split_memory_segments(memory_text)}
    orphans = [key for key in meta if key not in live]
    if not orphans:
        return 0
    for key in orphans:
        del meta[key]
    save_memory_meta(user_data_dir, meta)
    return len(orphans)
