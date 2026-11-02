"""ユーザーデータの復元（restore.py / /api/import / import コマンド）のテスト。

書き出し ZIP との往復、置き換えの意味（ZIP に無いファイルが消
えること）、復元前の自動控え、不正な ZIP の拒否、パス
トラバーサルの防止、書き出しと同じ分離ルール（他のユーザー／
テナントの置き場に触れないこと）、CLI・Web の入口を確認する。
"""
import base64
import http.client
import io
import json
import shutil
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.export import BACKUPS_DIRNAME, EXPORT_INFO_NAME, build_export_zip
from kuma_agent.llm import Message
from kuma_agent.restore import (
    MAX_IMPORT_BYTES,
    read_export_info,
    restore_user_data,
    save_user_backup,
)
from kuma_agent.session import Session
from kuma_agent.web import build_server

_VALID_INFO = {"format": "kuma-agent export",
               "tenant": "default", "user": "default"}


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


def _request(addr, method: str, path: str) -> tuple[int, dict, bytes]:
    host, port = addr
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _post_json(addr, path: str, payload: dict) -> tuple[int, dict, bytes]:
    host, port = addr
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request("POST", path,
                     body=json.dumps(payload).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _craft_zip(files: dict[str, str], info: dict | None = _VALID_INFO) -> bytes:
    """任意の項目を持つ ZIP を作る（検証系のテスト用）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        if info is not None:
            zf.writestr(EXPORT_INFO_NAME,
                        json.dumps(info, ensure_ascii=False))
        for name, text in files.items():
            zf.writestr(name, text)
    return buffer.getvalue()


def _seed_default_user(data_dir: Path) -> None:
    """既定ユーザー（＝データ置き場そのもの）に一通りのファイルを置く。"""
    (data_dir / "MEMORY.md").write_text("- 覚えていること", encoding="utf-8")
    (data_dir / "tasks.json").write_text("[]", encoding="utf-8")
    conv = data_dir / "conversations"
    conv.mkdir()
    (conv / "main.jsonl").write_text('{"role": "user"}\n', encoding="utf-8")
    outputs = data_dir / "outputs"
    outputs.mkdir()
    (outputs / "report.txt").write_text("成果物", encoding="utf-8")


def _wipe(path: Path) -> None:
    """ディレクトリの中身をすべて消す。"""
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()


# ------------------------------------------------------- restore 単体

def test_round_trip_restores_exported_files(tmp_path: Path):
    _seed_default_user(tmp_path)
    body = build_export_zip(tmp_path, "default", "default")
    _wipe(tmp_path)
    result = restore_user_data(tmp_path, "default", "default", body)
    assert result.restored_files == 4
    assert result.skipped_files == 0
    assert result.tenant == "default"
    assert result.user == "default"
    assert result.source_tenant == "default"
    assert result.source_user == "default"
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 覚えていること"
    assert (tmp_path / "tasks.json").read_text(encoding="utf-8") == "[]"
    assert (tmp_path / "conversations" / "main.jsonl").read_text(
        encoding="utf-8") == '{"role": "user"}\n'
    assert (tmp_path / "outputs" / "report.txt").read_text(
        encoding="utf-8") == "成果物"
    # 書き出し情報そのものはデータとして書き込まれない
    assert not (tmp_path / EXPORT_INFO_NAME).exists()
    # 対象が空だったので控えは取らない
    assert result.backup_path is None


def test_restore_replaces_existing_files(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("古い記憶", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("余分", encoding="utf-8")
    body = _craft_zip({"MEMORY.md": "新しい記憶"})
    result = restore_user_data(tmp_path, "default", "default", body)
    assert result.restored_files == 1
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "新しい記憶"
    # ZIP に無いファイルは置き換えで消える
    assert not (tmp_path / "extra.txt").exists()


def test_restore_takes_backup_of_existing_data(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("消える前の記憶", encoding="utf-8")
    now = datetime(2026, 11, 3, 9, 30, 0)
    body = _craft_zip({"MEMORY.md": "復元後の記憶"})
    result = restore_user_data(tmp_path, "default", "default", body, now=now)
    assert result.backup_path is not None
    assert result.backup_path.parent == tmp_path / BACKUPS_DIRNAME
    assert result.backup_path.name == "user-default-default-20261103-093000.zip"
    assert result.backup_path.is_file()
    # 控えの中身は復元前の状態そのもの
    with zipfile.ZipFile(result.backup_path) as zf:
        assert zf.namelist()[0] == EXPORT_INFO_NAME
        assert zf.read("MEMORY.md").decode("utf-8") == "消える前の記憶"
    # 置き換えで控え自体は消えていない
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "復元後の記憶"


def test_restore_without_existing_data_takes_no_backup(tmp_path: Path):
    body = _craft_zip({"MEMORY.md": "記憶"})
    result = restore_user_data(tmp_path, "default", "ghost", body)
    assert result.backup_path is None
    assert not (tmp_path / BACKUPS_DIRNAME).exists()
    ghost = tmp_path / "users" / "ghost"
    assert (ghost / "MEMORY.md").read_text(encoding="utf-8") == "記憶"


def test_read_export_info_and_save_user_backup(tmp_path: Path):
    _seed_default_user(tmp_path)
    body = build_export_zip(tmp_path, "default", "default")
    info = read_export_info(body)
    assert info["format"] == "kuma-agent export"
    assert info["tenant"] == "default"
    path = save_user_backup(tmp_path, "default", "default",
                            now=datetime(2026, 11, 3, 9, 30, 0))
    assert path == tmp_path / BACKUPS_DIRNAME \
        / "user-default-default-20261103-093000.zip"
    assert read_export_info(path.read_bytes())["user"] == "default"


def test_restore_rejects_non_zip(tmp_path: Path):
    with pytest.raises(ValueError):
        read_export_info(b"this is not a zip")
    with pytest.raises(ValueError):
        restore_user_data(tmp_path, "default", "default", b"junk")


def test_restore_rejects_zip_without_info(tmp_path: Path):
    body = _craft_zip({"MEMORY.md": "記憶"}, info=None)
    with pytest.raises(ValueError):
        restore_user_data(tmp_path, "default", "default", body)


def test_restore_rejects_other_format(tmp_path: Path):
    body = _craft_zip({"MEMORY.md": "記憶"},
                      info={"format": "kuma-agent tenant backup",
                            "tenant": "default"})
    with pytest.raises(ValueError):
        restore_user_data(tmp_path, "default", "default", body)
    assert not (tmp_path / "MEMORY.md").exists()


def test_restore_rejects_too_large_zip(tmp_path: Path):
    with pytest.raises(ValueError):
        restore_user_data(tmp_path, "default", "default",
                          b"x" * (MAX_IMPORT_BYTES + 1))


def test_restore_rejects_path_traversal(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("元の記憶", encoding="utf-8")
    body = _craft_zip({"ok.txt": "ok", "../evil.txt": "evil"})
    with pytest.raises(ValueError):
        restore_user_data(tmp_path, "default", "default", body)
    # 置き場の外にも中にも、何も書かれていない（検証で中止）
    assert not (tmp_path.parent / "evil.txt").exists()
    assert not (tmp_path / "ok.txt").exists()
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "元の記憶"


def test_restore_rejects_symlink_entry(tmp_path: Path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(EXPORT_INFO_NAME, json.dumps(_VALID_INFO))
        info = zipfile.ZipInfo("link.txt")
        info.external_attr = 0o120777 << 16  # シンボリックリンク
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError):
        restore_user_data(tmp_path, "default", "default", buffer.getvalue())
    assert not (tmp_path / "link.txt").exists()


def test_restore_skips_other_users_and_tenants(tmp_path: Path):
    alice = tmp_path / "users" / "alice"
    alice.mkdir(parents=True)
    (alice / "MEMORY.md").write_text("アリスの記憶", encoding="utf-8")
    body = _craft_zip({
        "MEMORY.md": "既定の記憶",
        "users/alice/MEMORY.md": "書き換え",
        "tenants/work/MEMORY.md": "仕事の記憶",
    })
    result = restore_user_data(tmp_path, "default", "default", body)
    assert result.restored_files == 1
    assert result.skipped_files == 2
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "既定の記憶"
    # 他のユーザーの置き場は、項目も既存の中身もそのまま
    assert (alice / "MEMORY.md").read_text(encoding="utf-8") == "アリスの記憶"
    assert not (tmp_path / "tenants").exists()


# ------------------------------------------------------------ Web

def test_page_has_import_button(server):
    status, _, body = _request(server, "GET", "/")
    assert status == 200
    text = body.decode("utf-8")
    assert 'id="importBtn"' in text
    assert ">復元</button>" in text
    assert "/api/import" in text


def test_web_import_restores(server, tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("古い記憶", encoding="utf-8")
    body = build_export_zip(tmp_path, "default", "default")
    (tmp_path / "MEMORY.md").write_text("書き換わった記憶", encoding="utf-8")
    payload = {"archive": base64.b64encode(body).decode("ascii")}
    status, _, raw = _post_json(server, "/api/import", payload)
    assert status == 200
    data = json.loads(raw)
    assert data["ok"] is True
    assert data["restored_files"] >= 1
    assert data["source_tenant"] == "default"
    assert data["source_user"] == "default"
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "古い記憶"
    # 置き換え前の控えが取られ、応答にファイル名が入る
    assert data["backup"] is not None
    assert (tmp_path / BACKUPS_DIRNAME / data["backup"]).is_file()


def test_web_import_rejects_bad_archive(server):
    payload = {"archive": base64.b64encode(b"junk").decode("ascii")}
    status, _, raw = _post_json(server, "/api/import", payload)
    assert status == 400
    assert json.loads(raw)["error"]


def test_web_import_requires_login_when_auth_enabled(tmp_path: Path):
    with _running(tmp_path, web_password="secret-pw") as addr:
        payload = {"archive": base64.b64encode(b"junk").decode("ascii")}
        status, _, _ = _post_json(addr, "/api/import", payload)
        assert status == 401


# ------------------------------------------------------------ CLI

def test_cli_import_restores_to_info_target(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    alice = data_dir / "users" / "alice"
    alice.mkdir(parents=True)
    (alice / "MEMORY.md").write_text("アリスの記憶", encoding="utf-8")
    archive = tmp_path / "alice.zip"
    archive.write_bytes(build_export_zip(data_dir, "default", "alice"))
    (alice / "MEMORY.md").write_text("消えた記憶", encoding="utf-8")
    monkeypatch.setenv("KUMA_DATA_DIR", str(data_dir))
    # --tenant / --user 無しでは、ZIP に記録された組み合わせが対象
    assert cli.main(["import", str(archive)]) == 0
    assert (alice / "MEMORY.md").read_text(encoding="utf-8") == "アリスの記憶"


def test_cli_import_tenant_user_options(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "MEMORY.md").write_text("既定の記憶", encoding="utf-8")
    archive = tmp_path / "in.zip"
    archive.write_bytes(build_export_zip(data_dir, "default", "default"))
    monkeypatch.setenv("KUMA_DATA_DIR", str(data_dir))
    assert cli.main(["import", str(archive), "--user", "carol"]) == 0
    carol = data_dir / "users" / "carol"
    assert (carol / "MEMORY.md").read_text(encoding="utf-8") == "既定の記憶"


def test_cli_import_missing_archive(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path / "data"))
    assert cli.main(["import", str(tmp_path / "nope.zip")]) == 1
    assert "見つかりません" in capsys.readouterr().out


def test_cli_import_bad_archive(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path / "data"))
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"junk")
    assert cli.main(["import", str(bad)]) == 1
    assert "復元できませんでした" in capsys.readouterr().out
