"""アバター（クマの顔画像）の管理。

Web UI のヘッダーや会話に表示されるクマの顔を、利用者が
差し替えられるようにする仕組み（詳細は docs/DESIGN.md の
「アバター管理」節）:

- 置き場はユーザーごと。候補の一覧と「今のアバター」は
  avatars.json、画像の実体は avatars/<id>.<拡張子> に持つ
- アップロードできるのは PNG / JPEG / GIF / WebP / SVG で、
  種別はファイル名ではなく中身（先頭バイト）で判定する。
  大きさは 2MB まで
- アップロードした画像はそのまま今のアバターになる。
  「選ぶ」で候補を切り替え、「元に戻す」で既定の顔
  （assets/avatar.svg）に戻る。既定に戻しても候補は残る
- SVG は画像として表示する経路（<img>）でのみ使い、ページ
  本体へは埋め込まない。配信側でも Content-Security-Policy:
  sandbox を添え、直接開かれた場合にスクリプトが動かない
  ようにする
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 置き場のファイル名（ユーザーの置き場に置く）
INDEX_FILENAME = "avatars.json"
AVATARS_DIRNAME = "avatars"

# アップロードできる大きさの上限
MAX_AVATAR_BYTES = 2 * 1024 * 1024

# 種別ごとの (media type, 拡張子)
_TYPE_PNG = ("image/png", "png")
_TYPE_JPEG = ("image/jpeg", "jpg")
_TYPE_GIF = ("image/gif", "gif")
_TYPE_WEBP = ("image/webp", "webp")
_TYPE_SVG = ("image/svg+xml", "svg")


def detect_avatar_type(data: bytes) -> tuple[str, str] | None:
    """画像の中身から (media type, 拡張子) を判定する。

    対応する形式でなければ None。SVG はテキストとして読め、
    `<svg` を含むものだけを受ける（HTML などの紛れ込みを防ぐ）。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _TYPE_PNG
    if data.startswith(b"\xff\xd8\xff"):
        return _TYPE_JPEG
    if data.startswith((b"GIF87a", b"GIF89a")):
        return _TYPE_GIF
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _TYPE_WEBP
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "<svg" in text[:4096].lower():
        return _TYPE_SVG
    return None


def validate_avatar(data: bytes) -> tuple[str, str]:
    """アップロードされた画像を検証し、(media type, 拡張子) を返す。

    空・大きすぎ・対応しない形式は ValueError（日本語の理由つき）。
    """
    if not data:
        raise ValueError("画像が空です")
    if len(data) > MAX_AVATAR_BYTES:
        raise ValueError(
            f"画像が大きすぎます（{MAX_AVATAR_BYTES // (1024 * 1024)}MB まで）")
    detected = detect_avatar_type(data)
    if detected is None:
        raise ValueError(
            "対応していない画像形式です（PNG / JPEG / GIF / WebP / SVG）")
    return detected


@dataclass
class AvatarItem:
    """アバターの候補 1 件。"""
    id: str
    media_type: str
    ext: str
    uploaded_at: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "media_type": self.media_type,
                "uploaded_at": self.uploaded_at}

    @classmethod
    def from_dict(cls, record: object) -> AvatarItem | None:
        if not isinstance(record, dict):
            return None
        item_id = record.get("id")
        media_type = record.get("media_type")
        if not isinstance(item_id, str) or not item_id:
            return None
        if not isinstance(media_type, str):
            return None
        ext = {"image/png": "png", "image/jpeg": "jpg",
               "image/gif": "gif", "image/webp": "webp",
               "image/svg+xml": "svg"}.get(media_type)
        if ext is None:
            return None
        return cls(id=item_id, media_type=media_type, ext=ext,
                   uploaded_at=record.get("uploaded_at")
                   if isinstance(record.get("uploaded_at"), str) else "")


@dataclass
class AvatarStore:
    """ユーザーごとのアバターの置き場（avatars.json + avatars/）。"""
    user_data_dir: Path
    _items: list[AvatarItem] = field(default_factory=list, init=False)
    _current: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._load()

    # ------------------------------------------------------ 保存まわり

    @property
    def _index_path(self) -> Path:
        return self.user_data_dir / INDEX_FILENAME

    @property
    def _files_dir(self) -> Path:
        return self.user_data_dir / AVATARS_DIRNAME

    def _file_path(self, item: AvatarItem) -> Path:
        return self._files_dir / f"{item.id}.{item.ext}"

    def _load(self) -> None:
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        self._items = []
        records = raw.get("items") if isinstance(raw, dict) else None
        if isinstance(records, list):
            for record in records:
                item = AvatarItem.from_dict(record)
                if item is not None:
                    self._items.append(item)
        current = raw.get("current") if isinstance(raw, dict) else None
        self._current = current if isinstance(current, str) else None
        if self._current is not None and self.get(self._current) is None:
            self._current = None  # 実在しない候補を指していたら既定に戻す

    def _save(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"current": self._current,
                   "items": [{"id": i.id, "media_type": i.media_type,
                              "uploaded_at": i.uploaded_at}
                             for i in self._items]}
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        self._index_path.write_text(text + "\n", encoding="utf-8")

    # ------------------------------------------------------ 参照

    def list(self) -> list[AvatarItem]:
        """候補の一覧（新しく追加した順）。"""
        return list(reversed(self._items))

    def get(self, item_id: str) -> AvatarItem | None:
        for item in self._items:
            if item.id == item_id:
                return item
        return None

    @property
    def current_id(self) -> str | None:
        """今のアバターの id。既定の顔なら None。"""
        return self._current

    def current_item(self) -> AvatarItem | None:
        if self._current is None:
            return None
        return self.get(self._current)

    def read_item(self, item_id: str) -> tuple[bytes, str] | None:
        """候補の画像を (実体, media type) で読む。無ければ None。"""
        item = self.get(item_id)
        if item is None:
            return None
        try:
            return self._file_path(item).read_bytes(), item.media_type
        except OSError:
            return None

    def read_current(self) -> tuple[bytes, str] | None:
        """今のアバターの画像を読む。既定の顔なら None。"""
        if self._current is None:
            return None
        return self.read_item(self._current)

    # ------------------------------------------------------ 変更

    def add(self, data: bytes, *,
            now: datetime | None = None) -> AvatarItem:
        """画像を候補に加え、今のアバターにする。"""
        media_type, ext = validate_avatar(data)
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        item = AvatarItem(id=uuid.uuid4().hex[:12],
                          media_type=media_type, ext=ext,
                          uploaded_at=stamp)
        self._files_dir.mkdir(parents=True, exist_ok=True)
        self._file_path(item).write_bytes(data)
        self._items.append(item)
        self._current = item.id
        self._save()
        return item

    def select(self, item_id: str) -> AvatarItem | None:
        """候補を今のアバターにする。対象が無ければ None。"""
        item = self.get(item_id)
        if item is None:
            return None
        self._current = item.id
        self._save()
        return item

    def reset(self) -> None:
        """既定の顔に戻す（候補は残す）。"""
        self._current = None
        self._save()

    def delete(self, item_id: str) -> AvatarItem | None:
        """候補を削除する。今のアバターなら既定の顔に戻る。"""
        item = self.get(item_id)
        if item is None:
            return None
        self._items = [i for i in self._items if i.id != item_id]
        if self._current == item_id:
            self._current = None
        self._save()
        try:
            self._file_path(item).unlink()
        except OSError:
            pass  # 実体が既に無い場合も削除は成立させる
        return item
