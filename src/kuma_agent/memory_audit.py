"""長期記憶の棚卸し（AM-3、巡回で古さを点検する）。

能動発信の巡回に足す、発言を伴わない裏の仕事（詳細は
docs/DESIGN.md の「長期記憶の属性と棚卸し」節）:

- 頻度は 1 日 1 回まで（自動抽出・自動整理と同じ流儀）。
  実施日はユーザーの置き場の memory_audit_state.json に持つ。
  環境変数 KUMA_MEMORY_AUDIT=0 で棚卸し全体を無効化できる
- 期限を過ぎた一時的な項目（属性 until が今日より前）は、
  控え（MEMORY.md.bak-<日時>、整理と同じ作法）を取ってから
  自動で MEMORY.md から外し、外した旨をフィードに 1 件だけ
  残す（種別は proactive）。会話への投稿はしない。属性も
  あわせて取り除く
- 変わり得る事実は自動では消さない。属性の since から
  CONFIRM_AGE_DAYS 日以上動きが無く、内容に状態の語や日付を
  含む項目を候補として 1 件だけ選び、状態ファイルに
  「今日の確認候補」として残す。巡回側（proactive）が朝の
  まとめの材料に渡し、まとめが投稿されたら「尋ねた」印を
  付ける。尋ねた項目は ASKED_SUPPRESS_DAYS 日の間は候補に
  選ばない（毎日同じことを聞かないため）。利用者が会話で
  答える・改めて触れると、remember や自動抽出の記録で since
  が更新され、候補から外れる
- 属性の孤児（手編集などで対応が取れなくなった分）は、
  ここでも掃除する（memory_meta.prune_memory_meta）
- モデルは使わない。記憶が無いユーザーでは何もせず、
  実施日も記録しない（材料が無い日を試行として数えない、
  自動抽出と同じ考え方）
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .feed import KIND_PROACTIVE, append_feed_entry
from .memory import Memory, split_memory_segments
from .memory_meta import (
    forget_memory_items,
    load_memory_meta,
    prune_memory_meta,
)
from .memory_tidy import _is_heading_segment, _segment_key, backup_memory_file

# 状態ファイル（ユーザーの置き場に置く。他の巡回の状態と
# 同じ「日付と進捗を JSON で記録する」流儀）
STATE_FILENAME = "memory_audit_state.json"

# 無効化の環境変数（"0" で無効、それ以外・未設定は有効）。
# memory_extract の KUMA_MEMORY_EXTRACT と同じ流儀で、
# モジュール側で呼ぶ時点に読む
AUDIT_ENV = "KUMA_MEMORY_AUDIT"

# この日数以上 since が動いていない項目を確認候補の対象にする
CONFIRM_AGE_DAYS = 90

# 一度尋ねた項目を、次に候補へ選ぶまでの間隔（日数）
ASKED_SUPPRESS_DAYS = 30

# 「変わり得る事実」の手がかりになる状態の語。判定は消すため
# ではなく確認を促すためだけに使うので、広めに取る
_STATE_WORDS = (
    "引っ越し", "引っ越", "転居", "転職", "就職", "退職", "入社",
    "勤務", "働い", "通勤", "通学", "通院", "入院", "治療", "妊娠",
    "出産", "結婚", "離婚", "単身赴任", "留学", "在住", "住んで",
    "住まい", "所属", "通って", "通う", "務め", "飼って", "飼い",
)

# 具体的な日付を含む項目も、時点に結びついた事実として候補にする
_DATE_IN_TEXT_RE = re.compile(r"(?:\d{4}年|\d{1,2}月\d{1,2}日)")


def audit_enabled() -> bool:
    """棚卸しが有効かどうか（KUMA_MEMORY_AUDIT=0 で無効化）。"""
    return os.environ.get(AUDIT_ENV, "") != "0"


# ---------------------------------------------------------- 状態

def _load_state(user_data_dir: Path) -> dict:
    """memory_audit_state.json を読む。無い・壊れていたら空の辞書。"""
    try:
        raw = json.loads(
            (user_data_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(user_data_dir: Path, state: dict) -> None:
    """memory_audit_state.json を書き直す（他の項目は保持する）。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False)
    (user_data_dir / STATE_FILENAME).write_text(text + "\n", encoding="utf-8")


def _asked_map(state: dict) -> dict[str, str]:
    """状態から「尋ねた項目と日付」の対応を取り出す（壊れた分は除く）。"""
    raw = state.get("asked")
    if not isinstance(raw, dict):
        return {}
    asked: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(key, str) and key and isinstance(value, str):
            asked[key] = value
    return asked


# ---------------------------------------------------------- 判定

def _expired_keys(memory_text: str, meta: dict, today: str) -> set[str]:
    """期限（until）が今日より前の項目の照合キーの集合。

    until は YYYY-MM-DD の文字列なので、そのまま辞書式に
    比較できる（load_memory_meta で形式は検証済み）。
    """
    keys: set[str] = set()
    for seg in split_memory_segments(memory_text):
        entry = meta.get(_segment_key(seg.text))
        if entry and entry.get("until") and entry["until"] < today:
            keys.add(_segment_key(seg.text))
    return keys


def _looks_changeable(text: str) -> bool:
    """変わり得る事実らしいかどうか（状態の語・日付の有無）。"""
    return (any(word in text for word in _STATE_WORDS)
            or _DATE_IN_TEXT_RE.search(text) is not None)


def _pick_confirm(memory_text: str, meta: dict,
                  asked: dict[str, str],
                  today: str, now: datetime) -> tuple[str, str] | None:
    """確認候補を 1 件選ぶ。(照合キー, 項目本文) か None を返す。

    条件: 見出しでない項目で、属性の since から
    CONFIRM_AGE_DAYS 日以上経っていて、内容が変わり得る事実
    らしく、最近（ASKED_SUPPRESS_DAYS 日以内に）尋ねていない
    もの。複数あれば since が一番古いものを選び、同点は
    MEMORY.md の行が早い方にする。
    """
    cutoff = (now.date() - timedelta(days=ASKED_SUPPRESS_DAYS)).isoformat()
    candidates: list[tuple[str, int, str, str]] = []
    for seg in split_memory_segments(memory_text):
        if _is_heading_segment(seg.text):
            continue
        key = _segment_key(seg.text)
        entry = meta.get(key)
        if not entry or not entry.get("since"):
            continue
        try:
            since = datetime.strptime(entry["since"], "%Y-%m-%d").date()
        except ValueError:
            continue
        if (now.date() - since).days < CONFIRM_AGE_DAYS:
            continue
        if not _looks_changeable(seg.text):
            continue
        asked_on = asked.get(key)
        if asked_on and asked_on > cutoff:
            continue  # 最近尋ねたばかりのものは間を空ける
        candidates.append((entry["since"], seg.line, key, seg.text))
    if not candidates:
        return None
    candidates.sort()
    _since, _line, key, text = candidates[0]
    return key, text


# ---------------------------------------------------------- 棚卸しの実行

@dataclass
class AuditResult:
    """棚卸し 1 回分の結果。

    removed は期限切れで外した項目の本文（古い順）。backup_path
    は控えを取った場合のそのパス。confirm_text は今日の確認
    候補（朝のまとめの材料。無ければ None）。
    """
    removed: list[str] = field(default_factory=list)
    backup_path: Path | None = None
    confirm_text: str | None = None


def audit_memory(user_data_dir: Path,
                 now: datetime | None = None) -> AuditResult | None:
    """巡回用の棚卸し。条件を満たさなければ None（何もしない）。

    条件: 棚卸しが有効（KUMA_MEMORY_AUDIT=0 でない）、本日まだ
    実施していない（1 日 1 回まで）、そのユーザーの長期記憶が
    ある。実施したら、まず実施日を記録する（途中で失敗して
    も、その日はやり直さない）。期限切れの外し込みとフィード
    への記録、確認候補の選出と状態への保存までを行う。
    """
    now = now or datetime.now()
    if not audit_enabled():
        return None
    state = _load_state(user_data_dir)
    today = now.date().isoformat()
    if state.get("last_audit_on") == today:
        return None  # 本日は実施済み
    memory = Memory(user_data_dir)
    text = memory.load_long_term()
    if not text.strip():
        return None  # 記憶が無い（実施として記録しない）
    state["last_audit_on"] = today
    _save_state(user_data_dir, state)

    meta = load_memory_meta(user_data_dir)
    result = AuditResult()
    expired = _expired_keys(text, meta, today)
    if expired:
        segments = split_memory_segments(text)
        removed = [seg.text for seg in segments
                   if _segment_key(seg.text) in expired]
        kept = [seg.text for seg in segments
                if _segment_key(seg.text) not in expired]
        result.backup_path = backup_memory_file(
            memory.memory_file, text, now=now)
        new_text = "\n".join(kept) + ("\n" if kept else "")
        memory.memory_file.parent.mkdir(parents=True, exist_ok=True)
        memory.memory_file.write_text(new_text, encoding="utf-8")
        forget_memory_items(user_data_dir, removed)
        result.removed = removed
        lines = [f"期限を過ぎた一時的な記憶を {len(removed)} 件外しました"
                 f"（控え: {result.backup_path.name}）。"]
        lines += [f"・{item}" for item in removed]
        append_feed_entry(user_data_dir, KIND_PROACTIVE,
                          "\n".join(lines), now=now)
        text = new_text
        meta = load_memory_meta(user_data_dir)

    # 属性の孤児掃除（手編集で対応が取れなくなった分）
    prune_memory_meta(user_data_dir, text)

    picked = _pick_confirm(text, meta, _asked_map(state), today, now)
    if picked is not None:
        key, confirm_text = picked
        state["confirm"] = {"key": key, "text": confirm_text, "on": today}
        result.confirm_text = confirm_text
    else:
        state.pop("confirm", None)
    # 尋ねた記録は、間隔（ASKED_SUPPRESS_DAYS）を過ぎた分を落とす
    cutoff = (now.date() - timedelta(days=ASKED_SUPPRESS_DAYS)).isoformat()
    asked = {key: day for key, day in _asked_map(state).items()
             if day > cutoff}
    if asked:
        state["asked"] = asked
    else:
        state.pop("asked", None)
    _save_state(user_data_dir, state)
    return result


def pending_confirm(user_data_dir: Path,
                    now: datetime | None = None) -> tuple[str, str] | None:
    """今日の確認候補（未だ尋ねていないもの）を (キー, 本文) で返す。

    棚卸しが当日分として状態に残した候補だけを対象にする
    （棚卸しが無効化された後などに、古い候補が朝のまとめへ
    混ざらないように）。本日すでに尋ねたものは返さない。
    """
    now = now or datetime.now()
    today = now.date().isoformat()
    state = _load_state(user_data_dir)
    confirm = state.get("confirm")
    if not isinstance(confirm, dict) or confirm.get("on") != today:
        return None
    key = confirm.get("key")
    text = confirm.get("text")
    if not isinstance(key, str) or not key or not isinstance(text, str):
        return None
    if _asked_map(state).get(key) == today:
        return None
    return key, text


def mark_confirm_asked(user_data_dir: Path, key: str,
                       now: datetime | None = None) -> None:
    """確認候補を「尋ねた」として記録する（朝のまとめ投稿後）。

    今日の確認候補がこのキーなら候補を取り下げ、尋ねた日を
    記録する（ASKED_SUPPRESS_DAYS 日の間は再び候補に選ばない）。
    """
    if not key:
        return
    now = now or datetime.now()
    today = now.date().isoformat()
    state = _load_state(user_data_dir)
    asked = _asked_map(state)
    asked[key] = today
    state["asked"] = asked
    confirm = state.get("confirm")
    if isinstance(confirm, dict) and confirm.get("key") == key:
        state.pop("confirm", None)
    _save_state(user_data_dir, state)
