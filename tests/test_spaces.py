"""小さな画面（残しておける HTML ページ）のテスト。

置き場（目次の検証・本文ファイルの読み書き・更新・削除・
名前引き・上限）、道具（両モードの登録と往復・活動表示）、
Web API（一覧・本文・操作）と画面タブの存在を確認する。
"""
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.spaces import MAX_SPACES, SpaceStore, validate_space
from kuma_agent.tools import ToolBox, activity_label
from kuma_agent.web import build_server

HTML = "<!doctype html><html><body><h1>体重の記録</h1></body></html>"


def _store(tmp_path: Path) -> SpaceStore:
    return SpaceStore(tmp_path)


# ------------------------------------------------------------ 置き場

def test_create_and_read_back(tmp_path: Path):
    store = _store(tmp_path)
    space = store.create("体重の記録", HTML)
    assert space.title == "体重の記録"
    assert store.read_html(space.id) == HTML
    # 本文は spaces/<id>.html、目次は spaces.json に分かれて残る
    assert (tmp_path / "spaces" / f"{space.id}.html").exists()
    # 別のインスタンスからも読める（保存されている）
    again = SpaceStore(tmp_path)
    assert [s.title for s in again.list()] == ["体重の記録"]
    assert again.read_html(space.id) == HTML


def test_list_is_recently_updated_first(tmp_path: Path):
    store = _store(tmp_path)
    a = store.create("A", HTML, now=datetime(2026, 11, 1, 9, 0, 0))
    b = store.create("B", HTML, now=datetime(2026, 11, 2, 9, 0, 0))
    assert [s.id for s in store.list()] == [b.id, a.id]
    store.update(a.id, html="<p>更新</p>", now=datetime(2026, 11, 3))
    assert [s.id for s in store.list()] == [a.id, b.id]


def test_update_title_and_html(tmp_path: Path):
    store = _store(tmp_path)
    space = store.create("旧題", HTML)
    updated = store.update(space.id, title="新題")
    assert updated is not None and updated.title == "新題"
    assert store.read_html(space.id) == HTML  # 本文はそのまま
    updated = store.update(space.id, html="<p>新しい本文</p>")
    assert updated is not None and updated.title == "新題"
    assert store.read_html(space.id) == "<p>新しい本文</p>"
    assert store.update("no-such-id", title="x") is None


def test_delete_removes_index_and_file(tmp_path: Path):
    store = _store(tmp_path)
    space = store.create("消す画面", HTML)
    removed = store.delete(space.id)
    assert removed is not None and removed.id == space.id
    assert store.list() == []
    assert not (tmp_path / "spaces" / f"{space.id}.html").exists()
    assert store.delete(space.id) is None


def test_find_by_title(tmp_path: Path):
    store = _store(tmp_path)
    store.create("体重の記録", HTML)
    target = store.create("読書リスト", HTML)
    assert store.find("読書リスト") is not None
    assert store.find("読書") is not None
    assert store.find("書リ") is not None
    assert store.find(target.id) is not None
    assert store.find("存在しない") is None
    assert store.find("") is None


def test_validation():
    with pytest.raises(ValueError):
        validate_space("", HTML)
    with pytest.raises(ValueError):
        validate_space("題", "")
    with pytest.raises(ValueError):
        validate_space("題" * 121, HTML)
    with pytest.raises(ValueError):
        validate_space("題", "あ" * 200_001)
    with pytest.raises(ValueError):
        validate_space(None, None)


def test_create_rejects_beyond_cap(tmp_path: Path):
    store = _store(tmp_path)
    for i in range(MAX_SPACES):
        store.create(f"画面{i}", HTML)
    with pytest.raises(ValueError):
        store.create("あふれる画面", HTML)


def test_broken_index_starts_empty(tmp_path: Path):
    (tmp_path / "spaces.json").write_text("{壊れた", encoding="utf-8")
    store = _store(tmp_path)
    assert store.list() == []
    space = store.create("作り直し", HTML)
    assert store.read_html(space.id) == HTML


# ------------------------------------------------------------ 道具

class _DummyModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "ok")


def _toolbox(tmp_path: Path, mode: str) -> ToolBox:
    cfg = Config(data_dir=tmp_path, mode=mode)
    session = Session(cfg, _DummyModel())
    return session.tools


@pytest.mark.parametrize("mode", ["ledger", "wall"])
def test_tools_registered_in_both_modes(tmp_path: Path, mode: str):
    tools = _toolbox(tmp_path, mode)
    names = {s["name"] for s in tools.schemas()}
    assert {"create_space", "update_space", "list_spaces",
            "delete_space"} <= names


def test_tools_roundtrip(tmp_path: Path):
    tools = _toolbox(tmp_path, "ledger")
    result = tools.call("create_space",
                        {"title": "体重の記録", "html": HTML})
    assert "画面を作りました" in result and "体重の記録" in result
    assert "体重の記録" in tools.call("list_spaces", {})
    result = tools.call("update_space",
                        {"space": "体重", "html": "<p>更新後</p>"})
    assert "更新しました" in result
    store = SpaceStore(tmp_path)
    space = store.find("体重の記録")
    assert space is not None and store.read_html(space.id) == "<p>更新後</p>"
    assert "削除しました" in tools.call(
        "delete_space", {"space": "体重の記録"})
    assert tools.call("list_spaces", {}) == "画面はまだありません"
    assert tools.call("delete_space", {"space": "無い画面"}).startswith(
        "error:")


def test_activity_labels():
    assert activity_label("create_space") == "画面を作成中"
    assert activity_label("update_space") == "画面を更新中"
    assert activity_label("delete_space") == "画面を削除中"


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
             payload: dict | None = None) -> tuple[int, dict]:
    if payload is None:
        try:
            with urllib.request.urlopen(base + path) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_spaces_over_http(tmp_path: Path):
    with _running(tmp_path) as base:
        status, data = _request(base, "/api/spaces")
        assert status == 200 and data["spaces"] == []

        status, data = _request(base, "/api/spaces/act", {
            "action": "create", "title": "体重の記録", "html": HTML})
        assert status == 200
        assert [s["title"] for s in data["spaces"]] == ["体重の記録"]
        space_id = data["spaces"][0]["id"]

        # 本文は別 API で取る
        status, data = _request(base, f"/api/spaces/content?id={space_id}")
        assert status == 200 and data["html"] == HTML

        status, data = _request(base, "/api/spaces/act", {
            "action": "update", "id": space_id, "html": "<p>新</p>"})
        assert status == 200
        status, data = _request(base, f"/api/spaces/content?id={space_id}")
        assert status == 200 and data["html"] == "<p>新</p>"

        status, data = _request(base, "/api/spaces/act", {
            "action": "delete", "id": space_id})
        assert status == 200 and data["spaces"] == []
        status, _ = _request(base, f"/api/spaces/content?id={space_id}")
        assert status == 404


def test_spaces_over_http_errors(tmp_path: Path):
    with _running(tmp_path) as base:
        status, _ = _request(base, "/api/spaces/act", {
            "action": "create", "title": "", "html": HTML})
        assert status == 400
        status, _ = _request(base, "/api/spaces/act", {
            "action": "update", "id": "nope", "html": "<p>x</p>"})
        assert status == 404
        status, _ = _request(base, "/api/spaces/act", {
            "action": "delete", "id": "nope"})
        assert status == 404


def test_page_has_spaces_tab(tmp_path: Path):
    with _running(tmp_path) as base:
        with urllib.request.urlopen(base + "/") as res:
            page = res.read().decode("utf-8")
    assert 'data-tab="spaces"' in page
    assert 'id="spacesPanel"' in page
    assert 'id="spaceFrame"' in page
    assert 'sandbox="allow-scripts"' in page
