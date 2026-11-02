"""能動発信の「知らせる・知らせない」の好み（notify_prefs.json）。

好みはユーザーごとに持ち、ユーザーの置き場に
{"muted_topics": ["語", ...], "quiet_start": "22:00",
 "quiet_end": "07:00"}
と書く（能動発信の巡回が proactive.py で参照する）。

- muted_topics: 知らせない話題の語。用事の題名＋メモにその語が
  （大小文字を区別せず）部分一致したら、その用事は能動発信の
  日報にもリマインダーにも出さない
- quiet_start / quiet_end: 静かな時間帯（"HH:MM"）。両方そろって
  正しく読めるときだけ有効で、その間は日報（定期の用事まとめ）
  だけを出さない。リマインダーは利用者が時刻を指定したもの
  なので、時間帯でも出す

ファイルが無い・壊れている場合は「好みなし」として動く（例外で
落とさない）。壊れた項目は、その項目だけを無かったことにする。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

PREFS_FILENAME = "notify_prefs.json"


def parse_hhmm(raw: object) -> time | None:
    """ "HH:MM" 形式の文字列を time として読む。読めなければ None。"""
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _format_hhmm(moment: time) -> str:
    """"HH:MM" 形式（ゼロ埋め）にそろえる。"""
    return f"{moment.hour:02d}:{moment.minute:02d}"


@dataclass
class NotifyPrefs:
    """1 ユーザー分の「知らせる・知らせない」の好み。"""

    muted_topics: list[str] = field(default_factory=list)
    # 静かな時間帯。"HH:MM" 形式で、両方そろったときだけ有効
    quiet_start: str | None = None
    quiet_end: str | None = None

    @property
    def quiet_enabled(self) -> bool:
        """静かな時間帯が有効かどうか（開始と終了の両方が要る）。"""
        return (parse_hhmm(self.quiet_start) is not None
                and parse_hhmm(self.quiet_end) is not None)

    def is_muted(self, *texts: str) -> bool:
        """渡したテキストのいずれかにミュート語が含まれれば True。

        照合は大小文字を区別しない部分一致（用事の題名とメモを
        渡す想定）。
        """
        joined = "\n".join(texts).casefold()
        return any(topic.casefold() in joined
                   for topic in self.muted_topics if topic)

    def in_quiet_hours(self, now: datetime) -> bool:
        """now が静かな時間帯に入っていれば True。

        開始を含み終了を含まない（開始 == 終了は時間帯なしとして
        扱う）。開始より終了が前なら日をまたぐ範囲として扱う
        （例: 22:00〜07:00）。
        """
        start = parse_hhmm(self.quiet_start)
        end = parse_hhmm(self.quiet_end)
        if start is None or end is None or start == end:
            return False
        current = now.time()
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def to_dict(self) -> dict:
        """保存・API 応答用の辞書にする。"""
        return {
            "muted_topics": list(self.muted_topics),
            "quiet_start": self.quiet_start,
            "quiet_end": self.quiet_end,
        }


def _clean_topics(raw: object) -> list[str]:
    """muted_topics の生データを整える。

    文字列だけを取り、前後の空白を除き、空のものは捨て、大小文字
    を区別せずに重複を取り除く（順序は保つ）。
    """
    if not isinstance(raw, list):
        return []
    topics: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        topic = item.strip()
        if not topic or topic.casefold() in seen:
            continue
        seen.add(topic.casefold())
        topics.append(topic)
    return topics


def load_notify_prefs(user_dir: Path) -> NotifyPrefs:
    """ユーザーの置き場から好みを読む。無い・壊れていたら好みなし。"""
    try:
        raw = json.loads(
            (user_dir / PREFS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NotifyPrefs()
    if not isinstance(raw, dict):
        return NotifyPrefs()
    prefs = NotifyPrefs(muted_topics=_clean_topics(raw.get("muted_topics")))
    start = parse_hhmm(raw.get("quiet_start"))
    end = parse_hhmm(raw.get("quiet_end"))
    if start is not None and end is not None:
        # 両方そろったときだけ有効。"HH:MM" の形にそろえて持つ
        prefs.quiet_start = _format_hhmm(start)
        prefs.quiet_end = _format_hhmm(end)
    return prefs


def save_notify_prefs(user_dir: Path, prefs: NotifyPrefs) -> None:
    """好みをユーザーの置き場に保存する。"""
    user_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(prefs.to_dict(), ensure_ascii=False, indent=2)
    (user_dir / PREFS_FILENAME).write_text(text + "\n", encoding="utf-8")


def add_muted_topic(user_dir: Path, topic: str) -> NotifyPrefs:
    """知らせない話題の語を 1 つ追加して保存し、最新の好みを返す。

    すでに登録されている語（大小文字を区別しない）は重ねて
    入れない。空の語は ValueError。
    """
    topic = topic.strip()
    if not topic:
        raise ValueError("知らせない話題の語が空です")
    prefs = load_notify_prefs(user_dir)
    if topic.casefold() not in {t.casefold() for t in prefs.muted_topics}:
        prefs.muted_topics.append(topic)
        save_notify_prefs(user_dir, prefs)
    return prefs


def remove_muted_topic(user_dir: Path, topic: str) -> NotifyPrefs:
    """知らせない話題の語を 1 つ外して保存し、最新の好みを返す。

    照合は大小文字を区別しない。登録されていない語なら何も
    変えない（好みはそのまま返す）。
    """
    topic = topic.strip()
    prefs = load_notify_prefs(user_dir)
    kept = [t for t in prefs.muted_topics if t.casefold() != topic.casefold()]
    if len(kept) != len(prefs.muted_topics):
        prefs.muted_topics = kept
        save_notify_prefs(user_dir, prefs)
    return prefs


def set_quiet_hours(user_dir: Path, start: str, end: str) -> NotifyPrefs:
    """静かな時間帯を設定して保存し、最新の好みを返す。

    開始・終了のどちらかでも "HH:MM" として読めなければ
    ValueError（壊れた値をファイルに書かないため）。
    """
    start_time = parse_hhmm(start)
    end_time = parse_hhmm(end)
    if start_time is None or end_time is None:
        raise ValueError("時間帯は HH:MM 形式で指定してください")
    prefs = load_notify_prefs(user_dir)
    prefs.quiet_start = _format_hhmm(start_time)
    prefs.quiet_end = _format_hhmm(end_time)
    save_notify_prefs(user_dir, prefs)
    return prefs


def clear_quiet_hours(user_dir: Path) -> NotifyPrefs:
    """静かな時間帯を解除して保存し、最新の好みを返す。"""
    prefs = load_notify_prefs(user_dir)
    if prefs.quiet_start is not None or prefs.quiet_end is not None:
        prefs.quiet_start = None
        prefs.quiet_end = None
        save_notify_prefs(user_dir, prefs)
    return prefs
