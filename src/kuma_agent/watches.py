"""見張り（条件の変化を知らせる仕組み）。

定期ジョブが「時刻が来たら仕事をする」のに対し、見張りは
「対象の状態が変わったら知らせる」。利用者が会話で登録した
見張りを、ユーザーごとの置き場の watches.json に保持する。
能動発信の巡回（proactive.py）が、間隔の来た見張りの対象を
確認し、前回のハッシュと違っていたら委任の子に知らせ文を
作らせて main の会話へ投稿する。確認そのものはモデルを使わ
ない（ハッシュの比較だけ）。

見張りの項目:
- id: 見張りの ID（使われていない番号を文字列で振る）
- title: 名前（利用者が呼ぶときの名前。削除や ON/OFF は名前
  でも ID でも指定できる）
- kind: 対象の種別。web（ページの本文）/ search（Web 検索の
  結果）/ file（作業ディレクトリ内のファイル）の 3 種
- target: 種別ごとの対象（web は URL、search は検索語、file
  は作業ディレクトリからの相対パス）
- interval_hours: 確認の間隔（時間、1 以上。既定 1）
- note: 知らせるときの指示（任意。例: 価格が下がっていたら
  その金額も添えて）
- enabled: 有効かどうか（既定は有効）
- last_hash: 前回確認した内容のハッシュ。空文字列は未確認
  （基準がまだ無い）。最初の確認は基準の記録だけで、知らせない
- last_summary: 前回確認した内容の短い要約（変化の前後を
  子へ渡して、知らせ文の材料にする）
- last_checked_at: 前回確認した時刻（ISO 8601）
- last_changed_at: 前回変化を検出した時刻（ISO 8601）
- last_error: 前回確認に失敗した理由（短い文）。成功したら空
- created_at: 作成時刻（ISO 8601）。未確認の見張りの期限
  判定の基準にも使う

ファイルが無い・壊れている場合は空の一覧として始める（例外で
落とさない）。壊れたレコードは読み飛ばす。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .webtools import fetch_page, web_search

WATCHES_FILENAME = "watches.json"

WATCH_KINDS = ("web", "search", "file")

# 種別の表示名
KIND_LABELS: dict[str, str] = {
    "web": "ページ",
    "search": "検索",
    "file": "ファイル",
}

# 要約として保持する最大の文字数（前後の内容を子へ渡す材料）
_MAX_SUMMARY_CHARS = 400


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_time(raw: object) -> datetime | None:
    """ISO 形式の時刻文字列を読む。読めなければ None。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _as_int(raw: object) -> int | None:
    """整数として読める値だけを int にする（真偽値は除く）。"""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    return None


def validate_watch(kind: object, target: object,
                   interval_hours: object = None) -> tuple[str, str, int]:
    """見張りの部品を検証し、(種別, 対象, 間隔) を正規化して返す。

    種別に合わない対象は ValueError。間隔は 1 時間以上で、
    省略時は 1 時間にする。
    """
    kind_name = str(kind or "").strip().lower()
    if kind_name not in WATCH_KINDS:
        raise ValueError(
            f"見張りの種別は {' / '.join(WATCH_KINDS)} のいずれかです")
    target_text = str(target or "").strip()
    if not target_text:
        raise ValueError("見張りの対象が空です")
    if kind_name == "web" and not target_text.startswith(
            ("http://", "https://")):
        raise ValueError("web の見張りの対象は http:// または https:// で"
                         "始まる URL にしてください")
    if interval_hours is None:
        hours = 1
    else:
        parsed = _as_int(interval_hours)
        if parsed is None or parsed < 1:
            raise ValueError("確認の間隔は 1 時間以上の整数にしてください")
        hours = parsed
    return kind_name, target_text, hours


def is_due(watch: Watch, now: datetime) -> bool:
    """確認の間隔が来ているかどうか。

    基準は前回確認した時刻（未確認なら作成時刻）。複数回分を
    逃しても、巡回で確認するのは 1 回だけ（追いつき確認はしない）。
    """
    if not watch.enabled:
        return False
    reference = (_parse_time(watch.last_checked_at)
                 or _parse_time(watch.created_at))
    if reference is None:
        return True
    return reference + timedelta(hours=watch.interval_hours) <= now


def _summarize(text: str) -> str:
    """確認した内容の短い要約（空白を潰して切り詰める）。"""
    collapsed = " ".join(text.split())
    if len(collapsed) > _MAX_SUMMARY_CHARS:
        collapsed = collapsed[:_MAX_SUMMARY_CHARS] + "…"
    return collapsed


def check_watch(watch: Watch, work_dir: Path) -> tuple[str, str]:
    """対象の現在状態を確認し、(ハッシュ, 要約) を返す。

    確認できなければ ValueError（理由は短い文）。file は作業
    ディレクトリの外を対象にできない（道具のファイル操作と
    同じ制限）。ファイルが無い状態も 1 つの状態として扱う
    （出現したら変化になる）。
    """
    if watch.kind == "web":
        text = fetch_page(watch.target)
        if text.startswith("error:"):
            raise ValueError(text.removeprefix("error:").strip())
        return hashlib.sha256(text.encode("utf-8")).hexdigest(), \
            _summarize(text)
    if watch.kind == "search":
        text = web_search(watch.target, limit=5)
        if text.startswith("error:"):
            raise ValueError(text.removeprefix("error:").strip())
        return hashlib.sha256(text.encode("utf-8")).hexdigest(), \
            _summarize(text)
    # file
    root = work_dir.resolve()
    path = (root / watch.target).resolve()
    if not str(path).startswith(str(root)):
        raise ValueError("作業ディレクトリの外は見張れません")
    if not path.exists():
        return hashlib.sha256(b"").hexdigest(), "（ファイルはありません）"
    if not path.is_file():
        raise ValueError("ファイルではありません")
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ValueError(f"ファイルを読めませんでした（{e}）") from e
    mtime = datetime.fromtimestamp(path.stat().st_mtime) \
        .isoformat(timespec="seconds")
    summary = f"{path.name}: {len(data)} バイト（更新 {mtime}）"
    return hashlib.sha256(data).hexdigest(), summary


@dataclass
class Watch:
    """見張り 1 件。"""
    id: str
    title: str
    kind: str
    target: str
    interval_hours: int = 1
    note: str = ""
    enabled: bool = True
    last_hash: str = ""
    last_summary: str = ""
    last_checked_at: str = ""
    last_changed_at: str = ""
    last_error: str = ""
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "target": self.target,
            "interval_hours": self.interval_hours,
            "note": self.note,
            "enabled": self.enabled,
            "last_hash": self.last_hash,
            "last_summary": self.last_summary,
            "last_checked_at": self.last_checked_at,
            "last_changed_at": self.last_changed_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, record: object) -> Watch | None:
        """辞書から復元する。形が不正なら None（読み飛ばす）。"""
        if not isinstance(record, dict):
            return None
        watch_id = record.get("id")
        title = record.get("title")
        if not isinstance(watch_id, str) or not watch_id:
            return None
        if not isinstance(title, str) or not title:
            return None
        try:
            kind, target, hours = validate_watch(
                record.get("kind"), record.get("target"),
                record.get("interval_hours"))
        except ValueError:
            return None
        enabled = record.get("enabled")
        return cls(
            id=watch_id,
            title=title,
            kind=kind,
            target=target,
            interval_hours=hours,
            note=str(record.get("note") or ""),
            enabled=enabled if isinstance(enabled, bool) else True,
            last_hash=str(record.get("last_hash") or ""),
            last_summary=str(record.get("last_summary") or ""),
            last_checked_at=str(record.get("last_checked_at") or ""),
            last_changed_at=str(record.get("last_changed_at") or ""),
            last_error=str(record.get("last_error") or ""),
            created_at=str(record.get("created_at") or _now()),
        )


class WatchBoard:
    """見張りの一覧。見張りファイル（watches.json）への保存・読み込みを持つ。

    用事の一覧（TaskBoard）と同じ流儀で、書き込みの前にはファイルを
    読み直し、他のセッションの追加を消さないようにする。
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.watches_file = data_dir / WATCHES_FILENAME
        self._watches: list[Watch] = self._load()

    def _load(self) -> list[Watch]:
        if not self.watches_file.exists():
            return []
        try:
            raw = json.loads(self.watches_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []  # 壊れたファイルは空として始める
        if not isinstance(raw, list):
            return []
        return [watch for record in raw
                if (watch := Watch.from_dict(record)) is not None]

    def _save(self) -> None:
        self.watches_file.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps([w.to_dict() for w in self._watches],
                          ensure_ascii=False, indent=2)
        self.watches_file.write_text(text + "\n", encoding="utf-8")

    def _next_id(self) -> str:
        used = {w.id for w in self._watches}
        n = 1
        while str(n) in used:
            n += 1
        return str(n)

    def add(self, title: str, kind: str, target: str,
            interval_hours: int = 1, note: str = "") -> Watch:
        """見張りを追加して保存する。部品の検証は validate_watch 済みの
        前提（種別・対象・間隔は検証を通った値を渡すこと）。"""
        self._watches = self._load()  # 他のセッションの追加を消さない
        watch = Watch(id=self._next_id(), title=title, kind=kind,
                      target=target, interval_hours=interval_hours,
                      note=note)
        self._watches.append(watch)
        self._save()
        return watch

    def list(self) -> list[Watch]:
        """見張りの一覧。返す前にファイルを読み直す。"""
        self._watches = self._load()
        return list(self._watches)

    def get(self, watch_id: str) -> Watch | None:
        for watch in self._watches:
            if watch.id == watch_id:
                return watch
        return None

    def find(self, key: str) -> Watch | None:
        """ID または名前で探す。名前は完全一致 → 前方一致の順に探す。"""
        self._watches = self._load()
        key = str(key or "").strip()
        if not key:
            return None
        for watch in self._watches:
            if watch.id == key:
                return watch
        for watch in self._watches:
            if watch.title == key:
                return watch
        for watch in self._watches:
            if watch.title.startswith(key):
                return watch
        return None

    def delete(self, key: str) -> Watch | None:
        """ID または名前で見張りを削除する。対象がなければ None。"""
        self._watches = self._load()
        watch = self.find(key)
        if watch is None:
            return None
        self._watches = [w for w in self._watches if w.id != watch.id]
        self._save()
        return watch

    def set_enabled(self, key: str, enabled: bool) -> Watch | None:
        """ID または名前で ON/OFF を切り替える。対象がなければ None。"""
        self._watches = self._load()
        watch = self.find(key)
        if watch is None:
            return None
        watch.enabled = enabled
        self._save()
        return watch

    def mark_checked(self, watch_id: str, now: datetime, *,
                     hash: str | None = None, summary: str = "",
                     error: str = "", changed: bool = False) -> None:
        """確認結果を記録する。

        hash が None のときは前回のハッシュを保つ（確認失敗時）。
        変化があったとき（changed）だけ last_changed_at を更新する。
        """
        self._watches = self._load()
        watch = self.get(watch_id)
        if watch is None:
            return
        watch.last_checked_at = now.isoformat(timespec="seconds")
        if hash is not None:
            watch.last_hash = hash
            watch.last_summary = summary
        watch.last_error = error
        if changed:
            watch.last_changed_at = now.isoformat(timespec="seconds")
        self._save()
