"""ユーザーデータの書き出し（export.py / /api/export / export コマンド）のテスト。

ZIP に export-info.json が先頭で入り、ユーザー置き場のファイルが
相対パスのまま収まること、ユーザー・テナントごとの分離（既定
ユーザーの書き出しに他のユーザー・テナントの置き場が混ざらない
こと）、空のユーザーでも書き出せること、Web API の応答ヘッダー、
CLI でファイルが書けること、ページにボタンが含まれることを
確認する。
"""
import http.client
import io
import json
import re
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.export import (
    EXPORT_INFO_NAME,
    build_export_zip,
    default_export_filename,
    iter_export_files,
)
from kuma_agent.llm import Message
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


def _request(addr, method: str, path: str) -> tuple[int, dict, bytes]:
    host, port = addr
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read()
    finally:
        conn.close()


def _open_zip(body: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(body))


def _read_info(zf: zipfile.ZipFile) -> dict:
    return json.loads(zf.read(EXPORT_INFO_NAME).decode("utf-8"))


def _seed_default_user(data_dir: Path) -> None:
    """既定ユーザー（＝データ置き場そのもの）に一通りのファイルを置く。"""
    (data_dir / "MEMORY.md").write_text("- 覚えていること", encoding="utf-8")
    (data_dir / "SOUL.md").write_text("# 性格", encoding="utf-8")
    (data_dir / "tasks.json").write_text("[]", encoding="utf-8")
    conv = data_dir / "conversations"
    conv.mkdir()
    (conv / "main.jsonl").write_text('{"role": "user"}\n', encoding="utf-8")
    outputs = data_dir / "outputs"
    outputs.mkdir()
    (outputs / "index.json").write_text("[]", encoding="utf-8")
    (outputs / "report.txt").write_text("成果物", encoding="utf-8")
    (data_dir / "usage.jsonl").write_text("{}\n", encoding="utf-8")


# ------------------------------------------------------- export 単体

def test_export_zip_contains_user_files(tmp_path: Path):
    _seed_default_user(tmp_path)
    zf = _open_zip(build_export_zip(tmp_path, "default", "default"))
    names = zf.namelist()
    assert names[0] == EXPORT_INFO_NAME  # 先頭は書き出し情報
    for expected in ("MEMORY.md", "SOUL.md", "tasks.json",
                     "conversations/main.jsonl", "outputs/index.json",
                     "outputs/report.txt", "usage.jsonl"):
        assert expected in names
    assert zf.read("MEMORY.md").decode("utf-8") == "- 覚えていること"
    info = _read_info(zf)
    assert info["tenant"] == "default"
    assert info["user"] == "default"
    assert info["exported_at"]
    assert info["kuma_agent_version"]
    assert "書き出し" in info["description"]


def test_export_empty_user_has_only_info(tmp_path: Path):
    # 置き場そのものが無いユーザーでも書き出せる
    zf = _open_zip(build_export_zip(tmp_path, "default", "ghost"))
    assert zf.namelist() == [EXPORT_INFO_NAME]
    # データ置き場自体が無い場合も同様
    zf = _open_zip(build_export_zip(tmp_path / "nope", "default", "default"))
    assert zf.namelist() == [EXPORT_INFO_NAME]
    assert iter_export_files(tmp_path, "default", "ghost") == []


def test_export_info_uses_given_time(tmp_path: Path):
    now = datetime(2026, 11, 3, 9, 30, 0)
    zf = _open_zip(build_export_zip(tmp_path, "default", "default", now=now))
    assert _read_info(zf)["exported_at"] == "2026-11-03T09:30:00"


def test_export_users_and_tenants_are_isolated(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("既定の記憶", encoding="utf-8")
    alice = tmp_path / "users" / "alice"
    alice.mkdir(parents=True)
    (alice / "MEMORY.md").write_text("アリスの記憶", encoding="utf-8")
    work = tmp_path / "tenants" / "work"
    (work / "MEMORY.md").parent.mkdir(parents=True)
    (work / "MEMORY.md").write_text("仕事の記憶", encoding="utf-8")
    bob = work / "users" / "bob"
    bob.mkdir(parents=True)
    (bob / "MEMORY.md").write_text("ボブの記憶", encoding="utf-8")

    # 既定ユーザー: 他のユーザー・テナントの置き場は混ざらない
    zf = _open_zip(build_export_zip(tmp_path, "default", "default"))
    assert zf.namelist() == [EXPORT_INFO_NAME, "MEMORY.md"]
    assert zf.read("MEMORY.md").decode("utf-8") == "既定の記憶"

    # 名前付きユーザー: 自分の置き場だけ
    zf = _open_zip(build_export_zip(tmp_path, "default", "alice"))
    assert zf.namelist() == [EXPORT_INFO_NAME, "MEMORY.md"]
    assert zf.read("MEMORY.md").decode("utf-8") == "アリスの記憶"

    # 別テナントの既定ユーザー: 配下の users/ は混ざらない
    zf = _open_zip(build_export_zip(tmp_path, "work", "default"))
    assert zf.namelist() == [EXPORT_INFO_NAME, "MEMORY.md"]
    assert zf.read("MEMORY.md").decode("utf-8") == "仕事の記憶"

    # 別テナントの名前付きユーザー
    zf = _open_zip(build_export_zip(tmp_path, "work", "bob"))
    assert _read_info(zf)["tenant"] == "work"
    assert _read_info(zf)["user"] == "bob"
    assert zf.read("MEMORY.md").decode("utf-8") == "ボブの記憶"


def test_default_export_filename():
    now = datetime(2026, 11, 3, 9, 30, 0)
    assert default_export_filename("default", "default", now) \
        == "kuma-export-default-default-2026-11-03.zip"
    assert default_export_filename("work", "alice", now) \
        == "kuma-export-work-alice-2026-11-03.zip"


# ------------------------------------------------------------ Web

def test_page_has_export_button(server):
    status, _, body = _request(server, "GET", "/")
    assert status == 200
    text = body.decode("utf-8")
    assert 'id="exportBtn"' in text
    assert ">書き出し</button>" in text
    assert "/api/export" in text


def test_web_export_download(server, tmp_path: Path):
    _seed_default_user(tmp_path)
    status, headers, body = _request(server, "GET", "/api/export")
    assert status == 200
    assert headers["Content-Type"] == "application/zip"
    disposition = headers["Content-Disposition"]
    assert disposition.startswith("attachment;")
    assert re.search(
        r"kuma-export-default-default-\d{4}-\d{2}-\d{2}\.zip", disposition)
    zf = _open_zip(body)
    assert zf.namelist()[0] == EXPORT_INFO_NAME
    assert "conversations/main.jsonl" in zf.namelist()
    # サーバー側に書き出しファイルは残らない（置き場に .zip が増えない）
    assert not list(tmp_path.glob("*.zip"))


def test_web_export_scoped_by_user(server, tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("既定の記憶", encoding="utf-8")
    alice = tmp_path / "users" / "alice"
    alice.mkdir(parents=True)
    (alice / "MEMORY.md").write_text("アリスの記憶", encoding="utf-8")
    status, _, body = _request(server, "GET", "/api/export?user=alice")
    assert status == 200
    zf = _open_zip(body)
    assert _read_info(zf)["user"] == "alice"
    assert zf.read("MEMORY.md").decode("utf-8") == "アリスの記憶"


def test_web_export_empty_user(server):
    status, headers, body = _request(server, "GET", "/api/export?user=ghost")
    assert status == 200
    assert headers["Content-Type"] == "application/zip"
    assert _open_zip(body).namelist() == [EXPORT_INFO_NAME]


def test_web_export_requires_login_when_auth_enabled(tmp_path: Path):
    with _running(tmp_path, web_password="secret-pw") as addr:
        status, _, _ = _request(addr, "GET", "/api/export")
        assert status == 401


# ------------------------------------------------------------ CLI

def test_cli_export_writes_default_filename(tmp_path: Path,
                                            monkeypatch, capsys):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "MEMORY.md").write_text("- 記憶", encoding="utf-8")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setenv("KUMA_DATA_DIR", str(data_dir))
    monkeypatch.chdir(cwd)
    assert cli.main(["export"]) == 0
    written = list(cwd.glob("kuma-export-default-default-*.zip"))
    assert len(written) == 1
    assert str(written[0].name) in capsys.readouterr().out
    zf = _open_zip(written[0].read_bytes())
    assert zf.namelist()[0] == EXPORT_INFO_NAME
    assert zf.read("MEMORY.md").decode("utf-8") == "- 記憶"


def test_cli_export_out_option(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "tasks.json").write_text("[]", encoding="utf-8")
    monkeypatch.setenv("KUMA_DATA_DIR", str(data_dir))
    target = tmp_path / "backup.zip"
    assert cli.main(["export", "--out", str(target)]) == 0
    zf = _open_zip(target.read_bytes())
    assert "tasks.json" in zf.namelist()


def test_cli_export_scoped_by_user(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    alice = data_dir / "users" / "alice"
    alice.mkdir(parents=True)
    (alice / "MEMORY.md").write_text("アリスの記憶", encoding="utf-8")
    (data_dir / "MEMORY.md").write_text("既定の記憶", encoding="utf-8")
    monkeypatch.setenv("KUMA_DATA_DIR", str(data_dir))
    target = tmp_path / "alice.zip"
    assert cli.main(["export", "--user", "alice",
                     "--out", str(target)]) == 0
    zf = _open_zip(target.read_bytes())
    assert _read_info(zf)["user"] == "alice"
    assert zf.read("MEMORY.md").decode("utf-8") == "アリスの記憶"
