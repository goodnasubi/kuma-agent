"""Web UI の人格・記憶の編集（/api/profile）のテスト。

対象 4 ファイル（SOUL.md / IDENTITY.md / USER.md / MEMORY.md）の
読み取り（無い場合は空）と保存、保存時の控え（.bak-<日時>）の
作成、ホワイトリスト外の名前の拒否、ユーザーごとの分離、サイズ
上限の拒否、ページにボタンとパネルが含まれることを確認する。
"""
import http.client
import json
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.profile_files import (
    MAX_PROFILE_BYTES,
    PROFILE_FILES,
    read_profile_file,
    save_profile_file,
)
from kuma_agent.session import Session
from kuma_agent.web import build_server


class _EchoModel:
    """受け取った発話をそのまま返す偽モデル。"""

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


@contextmanager
def _running(tmp_path: Path, **cfg_kwargs):
    """偽モデルで Web サーバーを起動し、(host, port) を渡す。"""
    cfg = Config(data_dir=tmp_path, **cfg_kwargs)
    session = Session(cfg, _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def server(tmp_path: Path):
    with _running(tmp_path) as addr:
        yield addr


def _request(addr, method: str, path: str, body=None,
             headers: dict | None = None) -> tuple[int, dict, str]:
    host, port = addr
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8")
    finally:
        conn.close()


def _get_profile(addr, name: str, **extra: str):
    from urllib.parse import urlencode
    query = urlencode({"name": name, **extra})
    status, _, body = _request(addr, "GET", f"/api/profile?{query}")
    return status, json.loads(body)


def _post_profile(addr, payload: dict):
    status, _, body = _request(
        addr, "POST", "/api/profile",
        body=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    return status, json.loads(body)


# ---------------------------------------------------------- ページ

def test_page_has_profile_button_and_panel(server):
    status, _, body = _request(server, "GET", "/")
    assert status == 200
    assert 'id="profileBtn"' in body
    assert ">人格・記憶<" in body
    assert 'id="profilePanel"' in body
    assert 'id="profileText"' in body
    assert 'id="profileSave"' in body
    for name in PROFILE_FILES:  # 切り替えの選択肢に 4 ファイルが出る
        assert name in body


# ---------------------------------------------------------- 読み取り

def test_read_missing_file_returns_empty(server):
    for name in PROFILE_FILES:
        status, data = _get_profile(server, name)
        assert status == 200
        assert data["name"] == name
        assert data["content"] == ""


def test_read_existing_file(server, tmp_path: Path):
    (tmp_path / "SOUL.md").write_text("# 性格\n穏やか", encoding="utf-8")
    status, data = _get_profile(server, "SOUL.md")
    assert status == 200
    assert data["content"] == "# 性格\n穏やか"


def test_read_without_name_is_400(server):
    status, _, _ = _request(server, "GET", "/api/profile")
    assert status == 400


# ---------------------------------------------------------- 保存

def test_save_then_read_roundtrip(server, tmp_path: Path):
    status, data = _post_profile(
        server, {"name": "MEMORY.md", "content": "- 猫を飼っている"})
    assert status == 200
    assert data["saved"] is True
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 猫を飼っている"
    status, data = _get_profile(server, "MEMORY.md")
    assert status == 200
    assert data["content"] == "- 猫を飼っている"


def test_save_creates_backup_of_previous_content(server, tmp_path: Path):
    _post_profile(server, {"name": "USER.md", "content": "前の内容"})
    _post_profile(server, {"name": "USER.md", "content": "新しい内容"})
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == "新しい内容"
    backups = sorted(tmp_path.glob("USER.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "前の内容"


def test_first_save_has_no_backup(server, tmp_path: Path):
    _post_profile(server, {"name": "SOUL.md", "content": "初回"})
    assert not list(tmp_path.glob("SOUL.md.bak-*"))


def test_save_without_content_is_400(server):
    status, _ = _post_profile(server, {"name": "SOUL.md"})
    assert status == 400


# -------------------------------------------------- ホワイトリスト

@pytest.mark.parametrize("name", [
    "config.py", "../MEMORY.md", "sub/SOUL.md", "SOUL.md.bak-1",
    "memory.md", "", "TENANT.md",
])
def test_names_outside_whitelist_are_rejected(server, name: str):
    status, _ = _get_profile(server, name)
    assert status == 400
    status, _ = _post_profile(server, {"name": name, "content": "x"})
    assert status == 400


# ---------------------------------------------------- ユーザー分離

def test_users_are_isolated(server, tmp_path: Path):
    _post_profile(server, {"name": "SOUL.md", "content": "アリスの性格",
                           "user": "alice"})
    # 別ユーザーからは見えない
    status, data = _get_profile(server, "SOUL.md", user="bob")
    assert status == 200
    assert data["content"] == ""
    # 既定ユーザーからも見えない
    status, data = _get_profile(server, "SOUL.md")
    assert data["content"] == ""
    # 別ユーザーが書いても、アリス側は変わらない
    _post_profile(server, {"name": "SOUL.md", "content": "ボブの性格",
                           "user": "bob"})
    _, data = _get_profile(server, "SOUL.md", user="alice")
    assert data["content"] == "アリスの性格"
    alice_file = tmp_path / "users" / "alice" / "SOUL.md"
    assert alice_file.read_text(encoding="utf-8") == "アリスの性格"


# ---------------------------------------------------------- 上限

def test_oversize_content_is_rejected(server, tmp_path: Path):
    # 上限はバイト数（UTF-8 換算）で判定する
    big = "あ" * (MAX_PROFILE_BYTES // 3 + 1)
    status, data = _post_profile(
        server, {"name": "MEMORY.md", "content": big})
    assert status == 400
    assert "error" in data
    assert not (tmp_path / "MEMORY.md").exists()


def test_content_just_under_limit_is_saved(server):
    text = "a" * (MAX_PROFILE_BYTES - 1)
    status, _ = _post_profile(server, {"name": "MEMORY.md", "content": text})
    assert status == 200


# ---------------------------------------------------------- 認証

def test_profile_api_requires_login_when_auth_enabled(tmp_path: Path):
    with _running(tmp_path, web_password="secret-pw") as addr:
        status, _ = _get_profile(addr, "SOUL.md")
        assert status == 401
        status, _ = _post_profile(
            addr, {"name": "SOUL.md", "content": "x"})
        assert status == 401
        assert not (tmp_path / "SOUL.md").exists()


# ---------------------------------------------- profile_files 単体

def test_profile_files_roundtrip_and_backup(tmp_path: Path):
    assert read_profile_file(tmp_path, "IDENTITY.md") == ""
    assert save_profile_file(tmp_path, "IDENTITY.md", "私はクマ") is None
    backup = save_profile_file(tmp_path, "IDENTITY.md", "私はクマ2")
    assert backup is not None
    assert backup.name.startswith("IDENTITY.md.bak-")
    assert backup.read_text(encoding="utf-8") == "私はクマ"
    assert read_profile_file(tmp_path, "IDENTITY.md") == "私はクマ2"
    # 一時ファイルは残らない
    assert not (tmp_path / "IDENTITY.md.tmp").exists()


def test_profile_files_reject_unknown_names(tmp_path: Path):
    with pytest.raises(ValueError):
        read_profile_file(tmp_path, "evil.txt")
    with pytest.raises(ValueError):
        save_profile_file(tmp_path, "../evil.txt", "x")


def test_profile_files_reject_oversize(tmp_path: Path):
    with pytest.raises(ValueError):
        save_profile_file(tmp_path, "MEMORY.md",
                          "a" * (MAX_PROFILE_BYTES + 1))
