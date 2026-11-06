"""小さな画面（残しておける Web ページ）の置き場。

会話の中で作った小さな画面（記録表・一覧・メモボードのような
HTML ページ）を、会話が終わっても残しておき、Web UI の
「画面」タブからいつでも開けるようにする仕組み。Muse の
Spaces（残る小さな画面）の kuma-agent 版で、詳細は
docs/DESIGN.md の「小さな画面」節:

- 置き場はユーザーごと。目次（タイトルと日時）は
  spaces.json、本文の HTML は spaces/<id>.html に分けて持つ
  （一覧では本文を読まない）
- 画面の中身は利用者とエージェントが作った HTML そのもの。
  表示は Web UI 側で隔離した枠（sandbox の iframe）に入れる
  前提で、ここでは中身の検査はしない（長さの上限だけ見る）
- 件数にも上限を設け、古いものを自動で消すことはしない
  （いっぱいなら追加を断る。削除は利用者と道具から）
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 置き場のファイル名（ユーザーの置き場に置く）
INDEX_FILENAME = "spaces.json"
SPACES_DIRNAME = "spaces"

# 上限: 件数・タイトルの長さ・HTML の長さ
MAX_SPACES = 50
MAX_TITLE_CHARS = 120
MAX_HTML_CHARS = 200_000


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def validate_space(title: object, html: object) -> tuple[str, str]:
    """画面の入力を検証し、(タイトル, HTML) をそろえて返す。

    タイトルは空でなく上限以内、HTML は空でなく上限以内。
    不正なら ValueError（日本語の理由つき）。
    """
    title_text = title.strip() if isinstance(title, str) else ""
    if not title_text:
        raise ValueError("画面のタイトルが空です")
    if len(title_text) > MAX_TITLE_CHARS:
        raise ValueError(
            f"画面のタイトルが長すぎます（{MAX_TITLE_CHARS} 文字まで）")
    html_text = html if isinstance(html, str) else ""
    if not html_text.strip():
        raise ValueError("画面の HTML が空です")
    if len(html_text) > MAX_HTML_CHARS:
        raise ValueError(
            f"画面の HTML が大きすぎます（{MAX_HTML_CHARS} 文字まで）")
    return title_text, html_text


@dataclass
class Space:
    """小さな画面 1 件（目次分。本文の HTML は別ファイル）。"""
    id: str
    title: str
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title,
                "created_at": self.created_at,
                "updated_at": self.updated_at}

    @classmethod
    def from_dict(cls, record: object) -> Space | None:
        if not isinstance(record, dict):
            return None
        space_id = record.get("id")
        title = record.get("title")
        if not isinstance(space_id, str) or not space_id:
            return None
        if not isinstance(title, str) or not title:
            return None
        return cls(
            id=space_id, title=title,
            created_at=record.get("created_at")
            if isinstance(record.get("created_at"), str) else "",
            updated_at=record.get("updated_at")
            if isinstance(record.get("updated_at"), str) else "")


@dataclass
class SpaceStore:
    """ユーザーごとの小さな画面の置き場（spaces.json + spaces/）。"""
    user_data_dir: Path
    _spaces: list[Space] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._load()

    # ------------------------------------------------------ 保存まわり

    @property
    def _index_path(self) -> Path:
        return self.user_data_dir / INDEX_FILENAME

    @property
    def _content_dir(self) -> Path:
        return self.user_data_dir / SPACES_DIRNAME

    def _content_path(self, space_id: str) -> Path:
        # id は自前で発行した英数だけの文字列。パストラバーサルを
        # 避けるため、英数と "-" 以外を含む id は置き場の外を指す
        # ものとして扱い、呼び出し側で弾く（_safe_id で検査する）
        return self._content_dir / f"{space_id}.html"

    @staticmethod
    def _safe_id(space_id: str) -> bool:
        return bool(space_id) and all(
            ch.isalnum() or ch in "-_" for ch in space_id)

    def _load(self) -> None:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        records = raw.get("spaces") if isinstance(raw, dict) else None
        self._spaces = []
        if isinstance(records, list):
            for record in records:
                space = Space.from_dict(record)
                if space is not None and self._safe_id(space.id):
                    self._spaces.append(space)

    def _save(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"spaces": [s.to_dict() for s in self._spaces]}
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        self._index_path.write_text(text + "\n", encoding="utf-8")

    def _write_html(self, space_id: str, html: str) -> None:
        self._content_dir.mkdir(parents=True, exist_ok=True)
        self._content_path(space_id).write_text(html, encoding="utf-8")

    # ------------------------------------------------------ 参照

    def list(self) -> list[Space]:
        """画面の一覧（新しく更新した順）。"""
        return sorted(self._spaces,
                      key=lambda s: (s.updated_at, s.created_at),
                      reverse=True)

    def get(self, space_id: str) -> Space | None:
        for space in self._spaces:
            if space.id == space_id:
                return space
        return None

    def find(self, query: str) -> Space | None:
        """id の完全一致 → タイトルの完全一致 → 前方一致 → 部分一致。

        道具とプログラムからの「名前または id」指定用。複数が
        部分一致したら新しく更新したものを返す。
        """
        text = str(query or "").strip()
        if not text:
            return None
        ordered = self.list()
        for space in ordered:
            if space.id == text:
                return space
        for matcher in (lambda s: s.title == text,
                        lambda s: s.title.startswith(text),
                        lambda s: text in s.title):
            for space in ordered:
                if matcher(space):
                    return space
        return None

    def read_html(self, space_id: str) -> str | None:
        """画面の HTML 本文を読む。無ければ None。"""
        if self.get(space_id) is None or not self._safe_id(space_id):
            return None
        try:
            return self._content_path(space_id).read_text(encoding="utf-8")
        except OSError:
            return None

    # ------------------------------------------------------ 変更

    def create(self, title: str, html: str, *,
               now: datetime | None = None) -> Space:
        """画面を追加する。上限に達していたら ValueError。"""
        title_text, html_text = validate_space(title, html)
        if len(self._spaces) >= MAX_SPACES:
            raise ValueError(
                f"画面は {MAX_SPACES} 件までです。不要な画面を"
                "削除してください")
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        space = Space(id=uuid.uuid4().hex[:12], title=title_text,
                      created_at=stamp, updated_at=stamp)
        self._write_html(space.id, html_text)
        self._spaces.append(space)
        self._save()
        return space

    def update(self, space_id: str, *, title: str | None = None,
               html: str | None = None,
               now: datetime | None = None) -> Space | None:
        """画面のタイトル／本文を更新する。対象が無ければ None。

        title と html の両方が None なら何もしない（対象は返す）。
        更新する項目だけ検証する。
        """
        space = self.get(space_id)
        if space is None:
            return None
        if title is None and html is None:
            return space
        new_title, new_html = space.title, None
        if html is not None:
            # タイトルも一緒に来ていたら両方を検証する
            new_title, new_html = validate_space(
                title if title is not None else space.title, html)
        elif title is not None:
            new_title, _ = validate_space(title, "<p>ok</p>")
        space.title = new_title
        if new_html is not None:
            self._write_html(space.id, new_html)
        space.updated_at = (now or datetime.now()).isoformat(
            timespec="seconds")
        self._save()
        return space

    def delete(self, space_id: str) -> Space | None:
        """画面を削除する（目次と本文の両方）。無ければ None。"""
        space = self.get(space_id)
        if space is None:
            return None
        self._spaces = [s for s in self._spaces if s.id != space_id]
        self._save()
        if self._safe_id(space_id):
            try:
                self._content_path(space_id).unlink()
            except OSError:
                pass  # 本文ファイルが既に無い場合も削除は成立させる
        return space
