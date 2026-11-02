"""成果物（outputs）のテスト。

置き場と index の読み書き（コピー・同名回避・壊れた記録の読み飛ばし）、
deliver_file 道具の登録と不正パス、一覧 API とダウンロード
（実ファイルが返ること、index に無い名前や ../ を含む名前の拒否）、
パネルとボタンがページに含まれること、CLI の一覧、保持期限の
クリーンアップ対象外であることを確認する。
"""
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import cleanup_expired
from kuma_agent.outputs import OutputBoard, format_created_at, format_size
from kuma_agent.session import Session
from kuma_agent.tools import ToolBox, activity_label
from kuma_agent.web import build_server


def _write(path: Path, text: str = "中身") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 置き場と index

def test_board_add_copies_and_registers(tmp_path: Path):
    source = _write(tmp_path / "work" / "report.txt", "レポート本文")
    board = OutputBoard(tmp_path / "user")
    output = board.add(source, title="週次レポート")

    assert output.filename == "report.txt"
    assert output.title == "週次レポート"
    assert output.size == len("レポート本文".encode("utf-8"))
    assert output.created_at  # 登録日時が入る
    copied = tmp_path / "user" / "outputs" / "report.txt"
    assert copied.read_text(encoding="utf-8") == "レポート本文"
    # 元ファイルはそのまま残る（コピーであって移動ではない）
    assert source.exists()

    index = json.loads(
        (tmp_path / "user" / "outputs" / "index.json").read_text(
            encoding="utf-8"))
    assert index == [{"filename": "report.txt", "title": "週次レポート",
                      "size": output.size,
                      "created_at": output.created_at}]
    # 読み直しても同じ一覧
    assert [o.filename for o in OutputBoard(tmp_path / "user").list()] == [
        "report.txt"]


def test_board_title_defaults_to_filename(tmp_path: Path):
    source = _write(tmp_path / "memo.md")
    output = OutputBoard(tmp_path).add(source)
    assert output.title == "memo.md"
    output = OutputBoard(tmp_path).add(source, title="  ")
    assert output.title == "memo.md"


def test_board_same_name_gets_numbered(tmp_path: Path):
    board = OutputBoard(tmp_path / "user")
    first_src = _write(tmp_path / "a" / "report.txt", "1 つ目")
    second_src = _write(tmp_path / "b" / "report.txt", "2 つ目")
    third_src = _write(tmp_path / "c" / "report.txt", "3 つ目")

    assert board.add(first_src).filename == "report.txt"
    assert board.add(second_src).filename == "report-2.txt"
    assert board.add(third_src).filename == "report-3.txt"

    outputs_dir = tmp_path / "user" / "outputs"
    assert (outputs_dir / "report.txt").read_text(encoding="utf-8") == "1 つ目"
    assert (outputs_dir / "report-2.txt").read_text(
        encoding="utf-8") == "2 つ目"
    assert (outputs_dir / "report-3.txt").read_text(
        encoding="utf-8") == "3 つ目"
    assert [o.filename for o in OutputBoard(tmp_path / "user").list()] == [
        "report.txt", "report-2.txt", "report-3.txt"]


def test_board_rejects_missing_and_directory(tmp_path: Path):
    board = OutputBoard(tmp_path)
    with pytest.raises(FileNotFoundError):
        board.add(tmp_path / "ないファイル.txt")
    with pytest.raises(ValueError):
        board.add(tmp_path)  # ディレクトリ


def test_board_starts_empty_on_broken_index(tmp_path: Path):
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "index.json").write_text("{壊れた JSON", encoding="utf-8")
    board = OutputBoard(tmp_path)  # 例外で落ちない
    assert board.list() == []
    source = _write(tmp_path / "report.txt")
    assert board.add(source).filename == "report.txt"
    assert len(OutputBoard(tmp_path).list()) == 1

    (outputs_dir / "index.json").write_text(
        '{"not": "a list"}', encoding="utf-8")
    assert OutputBoard(tmp_path).list() == []


def test_board_skips_broken_records(tmp_path: Path):
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "ok.txt").write_text("ok", encoding="utf-8")
    (outputs_dir / "index.json").write_text(json.dumps([
        {"filename": "ok.txt", "title": "まともな成果物", "size": 2,
         "created_at": "2026-11-02T09:00:00"},
        {"filename": "../secret.txt", "title": "パスを含む名前"},
        {"title": "ファイル名がない"},
        "not a dict",
    ], ensure_ascii=False), encoding="utf-8")
    outputs = OutputBoard(tmp_path).list()
    assert [o.filename for o in outputs] == ["ok.txt"]
    assert outputs[0].title == "まともな成果物"


def test_board_resolve_download(tmp_path: Path):
    board = OutputBoard(tmp_path)
    source = _write(tmp_path / "report.txt", "本文")
    board.add(source)
    # index に無いファイルが outputs/ にあっても許可しない
    _write(tmp_path / "outputs" / "unregistered.txt", "未登録")

    resolved = board.resolve_download("report.txt")
    assert resolved is not None
    assert resolved.read_text(encoding="utf-8") == "本文"
    assert board.resolve_download("unregistered.txt") is None
    assert board.resolve_download("ない.txt") is None
    assert board.resolve_download("../report.txt") is None
    assert board.resolve_download("outputs/report.txt") is None
    assert board.resolve_download("") is None
    assert board.resolve_download(None) is None


def test_format_helpers():
    assert format_size(512) == "512 B"
    assert format_size(1536) == "1.5 KB"
    assert format_created_at("2026-11-02T09:30:00") == "2026/11/2 9:30"
    assert format_created_at("読めない") == "読めない"
    assert format_created_at("") == ""


def test_cleanup_does_not_touch_outputs(tmp_path: Path):
    board = OutputBoard(tmp_path)
    board.add(_write(tmp_path / "report.txt", "残すべき成果物"))
    outputs_file = tmp_path / "outputs" / "report.txt"
    index_file = tmp_path / "outputs" / "index.json"
    old = (datetime.now() - timedelta(days=365)).timestamp()
    for path in (outputs_file, index_file):
        os.utime(path, (old, old))
    result = cleanup_expired(tmp_path, retention_days=90)
    assert result.total == 0
    assert outputs_file.exists() and index_file.exists()
    assert [o.filename for o in OutputBoard(tmp_path).list()] == ["report.txt"]


# ---------------------------------------------------------------- 道具

def _toolbox(tmp_path: Path) -> ToolBox:
    return ToolBox(tmp_path, output_board=OutputBoard(tmp_path / "user"))


def test_output_tool_registered_only_with_board(tmp_path: Path):
    plain_names = {s["name"] for s in ToolBox(tmp_path).schemas()}
    assert "deliver_file" not in plain_names
    names = {s["name"] for s in _toolbox(tmp_path).schemas()}
    assert "deliver_file" in names


def test_deliver_file_roundtrip(tmp_path: Path):
    box = _toolbox(tmp_path)
    source = _write(tmp_path / "report.txt", "成果物の本文")
    out = box.call("deliver_file", {"path": "report.txt"})
    assert "成果物として登録しました" in out
    assert "Web画面の「成果物」から開けます" in out
    copied = tmp_path / "user" / "outputs" / "report.txt"
    assert copied.read_text(encoding="utf-8") == "成果物の本文"

    # 絶対パスとタイトル指定
    other = _write(tmp_path / "sub" / "data.csv", "a,b")
    out = box.call("deliver_file",
                   {"path": str(other), "title": "集計データ"})
    assert "集計データ" in out and "data.csv" in out
    outputs = OutputBoard(tmp_path / "user").list()
    assert [(o.filename, o.title) for o in outputs] == [
        ("report.txt", "report.txt"), ("data.csv", "集計データ")]

    # 同名は連番で回避される
    out = box.call("deliver_file", {"path": "report.txt"})
    assert "report-2.txt" in out


def test_deliver_file_rejects_bad_paths(tmp_path: Path):
    box = _toolbox(tmp_path)
    assert box.call("deliver_file", {"path": "ない.txt"}).startswith("error:")
    (tmp_path / "folder").mkdir()
    assert box.call("deliver_file", {"path": "folder"}).startswith("error:")
    assert box.call("deliver_file", {"path": ""}).startswith("error:")
    # 不正なものは 1 件も登録されていない
    assert OutputBoard(tmp_path / "user").list() == []


def test_deliver_file_activity_label():
    assert activity_label("deliver_file") == "成果物を用意しています"
    assert activity_label(
        "deliver_file", {"path": "report.txt"}) == (
            "成果物を用意しています（report.txt）")


def test_session_registers_deliver_file_in_both_modes(tmp_path: Path):
    class _Echo:
        def chat(self, system_prompt, messages, tools):
            return Message("assistant", "はい")

    for mode in ("ledger", "wall"):
        session = Session(Config(data_dir=tmp_path / mode, mode=mode), _Echo())
        names = {s["name"] for s in session.tools.schemas()}
        assert "deliver_file" in names


# ---------------------------------------------------------------- Web API

class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


@pytest.fixture()
def server(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


def _get_json(url: str) -> tuple[int, dict]:
    status, body, _headers = _get(url)
    return status, json.loads(body.decode("utf-8"))


def test_outputs_api_lists_scoped_outputs(server):
    base, tmp_path = server
    OutputBoard(tmp_path).add(
        _write(tmp_path / "work" / "report.txt", "本文"), title="報告書")
    bob_dir = tmp_path / "tenants" / "acme" / "users" / "bob"
    OutputBoard(bob_dir).add(_write(tmp_path / "work" / "bob.txt", "bob分"))

    status, data = _get_json(base + "/api/outputs")
    assert status == 200
    assert data["tenant"] == "default" and data["user"] == "default"
    assert len(data["outputs"]) == 1
    entry = data["outputs"][0]
    assert entry["filename"] == "report.txt"
    assert entry["title"] == "報告書"
    assert entry["size"] == len("本文".encode("utf-8"))
    assert entry["created_at"]

    query = urllib.parse.urlencode({"tenant": "acme", "user": "bob"})
    status, data = _get_json(base + "/api/outputs?" + query)
    assert status == 200
    assert [o["filename"] for o in data["outputs"]] == ["bob.txt"]

    # 成果物の無い組み合わせは空の一覧
    query = urllib.parse.urlencode({"tenant": "acme", "user": "alice"})
    status, data = _get_json(base + "/api/outputs?" + query)
    assert status == 200 and data["outputs"] == []


def test_outputs_download_returns_file(server):
    base, tmp_path = server
    OutputBoard(tmp_path).add(
        _write(tmp_path / "work" / "report.txt", "ダウンロードする本文"))
    status, body, headers = _get(
        base + "/api/outputs/download?name=report.txt")
    assert status == 200
    assert body.decode("utf-8") == "ダウンロードする本文"
    disposition = headers.get("Content-Disposition", "")
    assert disposition.startswith("attachment")
    assert "report.txt" in disposition


def test_outputs_download_rejects_unknown_and_traversal(server):
    base, tmp_path = server
    board = OutputBoard(tmp_path)
    board.add(_write(tmp_path / "work" / "report.txt", "本文"))
    _write(tmp_path / "outputs" / "unregistered.txt", "未登録")
    _write(tmp_path / "secret.txt", "秘密")

    for name in ("unregistered.txt", "ない.txt", "../secret.txt",
                 "..%2Fsecret.txt", "outputs/report.txt"):
        url = base + "/api/outputs/download?name=" + urllib.parse.quote(
            name, safe="")
        status, _body, _headers = _get(url)
        assert status in (400, 404), (name, status)

    # name 自体が無い場合も拒否
    status, _body, _headers = _get(base + "/api/outputs/download")
    assert status == 400


def test_page_has_outputs_panel(server):
    base, _tmp_path = server
    status, body, _headers = _get(base + "/")
    page = body.decode("utf-8")
    assert status == 200
    assert "outputsBtn" in page and "成果物" in page
    assert "outputsPanel" in page and "outputRows" in page
    assert "/api/outputs" in page and "/api/outputs/download" in page
    assert "ダウンロード" in page


# ---------------------------------------------------------------- CLI

def _setup_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KUMA_TENANT", raising=False)
    monkeypatch.delenv("KUMA_USER", raising=False)
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)


def test_cli_outputs(monkeypatch, tmp_path: Path, capsys):
    OutputBoard(tmp_path).add(
        _write(tmp_path / "report.txt", "本文です"), title="報告書")
    _setup_env(monkeypatch, tmp_path)
    assert cli.main(["outputs"]) == 0
    out = capsys.readouterr().out
    assert "報告書" in out and "report.txt" in out


def test_cli_outputs_empty(monkeypatch, tmp_path: Path, capsys):
    _setup_env(monkeypatch, tmp_path)
    assert cli.main(["outputs"]) == 0
    assert "成果物はありません" in capsys.readouterr().out


def test_cli_outputs_scoped_by_tenant_and_user(
        monkeypatch, tmp_path: Path, capsys):
    bob_dir = tmp_path / "tenants" / "acme" / "users" / "bob"
    OutputBoard(bob_dir).add(_write(tmp_path / "bob.txt"), title="bobの成果物")
    OutputBoard(tmp_path).add(
        _write(tmp_path / "default.txt"), title="既定の成果物")
    _setup_env(monkeypatch, tmp_path)
    assert cli.main(["outputs", "--tenant", "acme", "--user", "bob"]) == 0
    out = capsys.readouterr().out
    assert "bobの成果物" in out and "既定の成果物" not in out
