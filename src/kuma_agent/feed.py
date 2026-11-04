"""フィード（能動発信と用事の進み具合の時系列）の記録と読み出し。

フィードタブに並べる読み物の置き場。エントリはユーザーの置き場の
feed.jsonl へ 1 件 1 行で追記する（使用量の usage.jsonl と同じ流儀）。

種別（kind）は次の 3 つ:
- briefing: 朝のまとめ
- proactive: 声かけ・日報・リマインダー・定期ジョブの結果などの
  能動発信
- task: 用事の進み具合（追加・メモや状態の更新・完了などの変化）

記録は「実際に起きたことの脇の記録」で、本体の動作（会話への
投稿、用事の保存）を失敗させない。追記側（append_feed_entry）は
例外を外へ出さず、読み出し側（load_feed_entries）は壊れた行を
読み飛ばす。

過去分の取り込みはしない。能動発信は会話記録に proactive の印が
付いているため見分け自体はできるが、朝のまとめとそれ以外の
能動発信を印だけでは区別できない。種別を取り違えて残すより、
この版から新規に起きた分だけを貯める。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

FEED_FILE = "feed.jsonl"

KIND_BRIEFING = "briefing"
KIND_PROACTIVE = "proactive"
KIND_TASK = "task"
FEED_KINDS = (KIND_BRIEFING, KIND_PROACTIVE, KIND_TASK)

# 一覧で返す件数の既定と上限（フィードは読み物なので新しい方を優先する）
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# 追記を直列化する（Web サーバーや巡回が同時に記録することがある）
_write_lock = threading.Lock()


@dataclass
class FeedEntry:
    """フィードの 1 エントリ。title は見出し（無ければ空文字列）。"""
    ts: str
    kind: str
    title: str
    body: str

    def to_dict(self) -> dict:
        return {"ts": self.ts, "kind": self.kind,
                "title": self.title, "body": self.body}

    @classmethod
    def from_dict(cls, record: object) -> FeedEntry | None:
        """辞書から復元する。形が不正なら None（読み飛ばす）。

        種別が既知の 3 つのいずれかで、本文が空でないものだけを
        エントリとして扱う。
        """
        if not isinstance(record, dict):
            return None
        kind = record.get("kind")
        body = record.get("body")
        if kind not in FEED_KINDS:
            return None
        if not isinstance(body, str) or not body:
            return None
        return cls(
            ts=str(record.get("ts") or ""),
            kind=kind,
            title=str(record.get("title") or ""),
            body=body,
        )


def append_feed_entry(user_data_dir: Path, kind: str, body: str,
                      title: str = "",
                      now: datetime | None = None) -> None:
    """フィードへ 1 件追記する。

    種別が既知の範囲外、または本文が空のときは何も書かない。
    記録に失敗しても呼び出し側（投稿や用事の保存）を落とさない
    よう、例外は外へ出さない。
    """
    if kind not in FEED_KINDS or not body:
        return
    entry = FeedEntry(
        ts=(now or datetime.now()).isoformat(timespec="seconds"),
        kind=kind, title=title, body=body)
    try:
        with _write_lock:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            with open(user_data_dir / FEED_FILE, "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(entry.to_dict(),
                                   ensure_ascii=False) + "\n")
    except Exception:
        pass  # 記録の失敗で本体の動作を落とさない


def _normalize_limit(limit: object) -> int:
    """件数の指定を既定・上限の範囲へそろえる。"""
    try:
        value = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def load_feed_entries(user_data_dir: Path, limit: object = None,
                      kind: str | None = None) -> list[FeedEntry]:
    """フィードのエントリを新しい順に返す。

    kind を渡すとその種別だけに絞る（既知の種別でない指定は
    絞り込みとして扱わず、全種別を返す）。limit は件数の上限で、
    既定は DEFAULT_LIMIT、上限は MAX_LIMIT。ファイルが無い・
    壊れている場合は空（壊れた行は読み飛ばす）。
    """
    path = user_data_dir / FEED_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries: list[FeedEntry] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        entry = FeedEntry.from_dict(record)
        if entry is not None:
            entries.append(entry)
    entries.reverse()  # 追記順（古い順）→ 新しい順
    if kind in FEED_KINDS:
        entries = [e for e in entries if e.kind == kind]
    return entries[:_normalize_limit(limit)]
