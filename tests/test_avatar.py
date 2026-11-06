"""アバター管理（アップロード／選択／元に戻す）のテスト。

種別の判定（中身で見る）、置き場（追加・選択・削除・既定への
復帰・保存）、Web API（アップロード・画像の配信・操作）と
画面（ヘッダーの <img> と人格パネルのアバター欄）を確認する。
"""
import base64
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from kuma_agent.avatar import AvatarStore, detect_avatar_type
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.web import build_server, render_page

# 1x1 の透明 PNG
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF_BYTES = b"GIF89a" + b"\x00" * 32
WEBP_BYTES = b"RIFF" + b"\x24\x00\x00\x00" + b"WEBP" + b"\x00" * 16
SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'


# ------------------------------------------------------------ 種別の判定

def test_detect_types():
    assert detect_avatar_type(PNG_BYTES) == ("image/png", "png")
    assert detect_avatar_type(JPEG_BYTES) == ("image/jpeg", "jpg")
    assert detect_avatar_type(GIF_BYTES) == ("image/gif", "gif")
    assert detect_avatar_type(WEBP_BYTES) == ("image/webp", "webp")
    assert detect_avatar_type(SVG_BYTES) == ("image/svg+xml", "svg")
    assert detect_avatar_type(b"just text") is None
    assert detect_avatar_type(b"<html><body>x</body></html>") is None
    assert detect_avatar_type(b"") is None


# ------------------------------------------------------------ 置き場

def test_add_selects_and_reads_back(tmp_path: Path):
    store = AvatarStore(tmp_path)
    assert store.current_id is None
    assert store.read_current() is None
    item = store.add(PNG_BYTES)
    assert store.current_id == item.id
    assert store.read_current() == (PNG_BYTES, "image/png")
    # 別のインスタンスからも読める（保存されている）
    again = AvatarStore(tmp_path)
    assert again.current_id == item.id
    assert again.read_item(item.id) == (PNG_BYTES, "image/png")


def test_select_reset_delete(tmp_path: Path):
    store = AvatarStore(tmp_path)
    a = store.add(PNG_BYTES)
    b = store.add(SVG_BYTES)
    assert store.current_id == b.id  # 新しく足した方が今のアバター
    assert store.select(a.id) is not None
    assert store.current_id == a.id
    assert store.select("nope") is None
    store.reset()
    assert store.current_id is None
    assert len(store.list()) == 2  # 候補は残る
    # 今のアバターを消すと既定に戻る
    store.select(b.id)
    removed = store.delete(b.id)
    assert removed is not None and store.current_id is None
    assert store.read_item(b.id) is None
    assert not (tmp_path / "avatars" / f"{b.id}.svg").exists()
    assert store.delete("nope") is None


def test_add_rejects_bad_images(tmp_path: Path):
    store = AvatarStore(tmp_path)
    with pytest.raises(ValueError):
        store.add(b"")
    with pytest.raises(ValueError):
        store.add(b"not an image")
    with pytest.raises(ValueError):
        store.add(PNG_BYTES + b"\x00" * (2 * 1024 * 1024))
    assert store.list() == []


def test_broken_index_starts_empty(tmp_path: Path):
    (tmp_path / "avatars.json").write_text("{壊れた", encoding="utf-8")
    store = AvatarStore(tmp_path)
    assert store.list() == [] and store.current_id is None


# ------------------------------------------------------------ Web API と画面

class _SimpleModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "通常の返答です")


@contextmanager
def _running(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _SimpleModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(base: str, path: str,
             payload: dict | None = None) -> tuple[int, object]:
    if payload is None:
        try:
            with urllib.request.urlopen(base + path) as res:
                body = res.read()
                try:
                    return res.status, json.loads(body.decode("utf-8"))
                except ValueError:
                    return res.status, body
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body.decode("utf-8"))
            except ValueError:
                return e.code, body
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _upload_payload(data: bytes) -> dict:
    return {"name": "face.png", "media_type": "image/png",
            "data_base64": base64.b64encode(data).decode("ascii")}


def test_avatar_over_http(tmp_path: Path):
    with _running(tmp_path) as base:
        # 最初は既定の顔（SVG が返る）
        status, body = _request(base, "/api/avatar/image")
        assert status == 200 and isinstance(body, bytes)
        assert b"<svg" in body
        status, data = _request(base, "/api/avatar")
        assert status == 200 and data["current"] is None
        assert data["items"] == []

        # アップロードすると今のアバターになる
        status, data = _request(base, "/api/avatar/upload",
                                _upload_payload(PNG_BYTES))
        assert status == 200
        item_id = data["current"]["id"]
        assert data["current"]["media_type"] == "image/png"
        status, body = _request(base, "/api/avatar/image")
        assert status == 200 and body == PNG_BYTES
        status, body = _request(base, f"/api/avatar/image?id={item_id}")
        assert status == 200 and body == PNG_BYTES

        # 元に戻すと既定の顔に。候補は残る
        status, data = _request(base, "/api/avatar/act",
                                {"action": "reset"})
        assert status == 200 and data["current"] is None
        assert len(data["items"]) == 1
        status, body = _request(base, "/api/avatar/image")
        assert status == 200 and b"<svg" in body

        # 選ぶ → 削除の往復
        status, data = _request(base, "/api/avatar/act",
                                {"action": "select", "id": item_id})
        assert status == 200 and data["current"]["id"] == item_id
        status, data = _request(base, "/api/avatar/act",
                                {"action": "delete", "id": item_id})
        assert status == 200 and data["items"] == []
        assert data["current"] is None
        status, _ = _request(base, f"/api/avatar/image?id={item_id}")
        assert status == 404


def test_avatar_over_http_errors(tmp_path: Path):
    with _running(tmp_path) as base:
        status, _ = _request(base, "/api/avatar/upload",
                             _upload_payload(b"not an image"))
        assert status == 400
        status, _ = _request(base, "/api/avatar/upload",
                             {"data_base64": "!!!"})
        assert status == 400
        status, _ = _request(base, "/api/avatar/act",
                             {"action": "select", "id": "nope"})
        assert status == 404
        status, _ = _request(base, "/api/avatar/act",
                             {"action": "delete", "id": "nope"})
        assert status == 404
        status, _ = _request(base, "/api/avatar/act", {"action": "bogus"})
        assert status == 400


def test_page_has_avatar_management():
    page = render_page("muse")
    assert 'id="avatarImg"' in page
    assert 'src="/api/avatar/image"' in page
    assert 'id="avatarPreview"' in page
    assert 'id="avatarUpload"' in page
    assert 'id="avatarReset"' in page
    # 明示の SVG が渡された場合は従来どおり埋め込む
    page2 = render_page("muse", avatar_svg="<svg></svg>")
    assert "<svg></svg>" in page2
