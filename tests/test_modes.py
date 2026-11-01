"""モード切替（相棒モード / スレッドモード）のテスト。

内部の識別子は wall / ledger で、表示名は config.MODE_LABELS に
集約する。既定は相棒モード（ledger）で、旧値 both は後方互換の
ため ledger に読み替える。用事の一覧はユーザーごとに 1 つで持つ。
"""
import json
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kuma_agent.config import MODES, MODE_LABELS, Config, mode_label, normalize_mode
from kuma_agent.llm import Message
from kuma_agent.memory import cleanup_expired
from kuma_agent.session import Session
from kuma_agent.tasks import TaskBoard
from kuma_agent.web import build_server, render_page

TASK_TOOLS = {"list_tasks", "create_task", "update_task"}


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


def _tool_names(session: Session) -> set[str]:
    return {s["name"] for s in session.tools.schemas()}


# ---------------------------------------------------------------- 設定と表示名

def test_normalize_mode():
    assert normalize_mode("ledger") == "ledger"
    assert normalize_mode(" WALL ") == "wall"
    # 旧値 both は後方互換のため ledger に読み替える
    assert normalize_mode("both") == "ledger"
    assert normalize_mode("BOTH") == "ledger"
    assert normalize_mode("わからない") == "ledger"
    assert normalize_mode("") == "ledger"
    assert normalize_mode(None) == "ledger"


def test_config_mode_default_env_and_invalid(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KUMA_MODE", raising=False)
    assert Config().mode == "ledger"  # 既定は相棒モード
    monkeypatch.setenv("KUMA_MODE", "wall")
    assert Config().mode == "wall"
    monkeypatch.setenv("KUMA_MODE", "both")
    assert Config().mode == "ledger"  # 旧値は ledger に読み替える
    monkeypatch.setenv("KUMA_MODE", "変な値")
    assert Config().mode == "ledger"  # 不正な環境変数は既定に倒す
    # 直接指定した場合も __post_init__ で正規化される
    assert Config(mode="wall").mode == "wall"
    assert Config(mode="both").mode == "ledger"
    assert Config(mode="おかしい").mode == "ledger"


def test_mode_labels_cover_all_modes():
    assert set(MODE_LABELS) == set(MODES) == {"wall", "ledger"}
    assert MODE_LABELS["wall"] == "スレッドモード"
    assert MODE_LABELS["ledger"] == "相棒モード"
    assert mode_label("wall") == "スレッドモード"
    assert mode_label("both") == "相棒モード"  # 旧値も読み替え先の表示名
    assert mode_label("不正な値") == "相棒モード"


# ---------------------------------------------------------------- スレッドモード（wall）

def test_wall_mode_disables_task_tools_and_injection(tmp_path: Path):
    # 既定の部屋の用事の一覧に進行中の用事があっても、道具にも注入にも出ない
    TaskBoard(tmp_path).add("既にある用事", "メモ")
    session = Session(Config(data_dir=tmp_path, mode="wall"), _EchoModel())
    assert _tool_names(session).isdisjoint(TASK_TOOLS)
    assert "# 進行中の用事" not in session.system_prompt
    assert "既にある用事" not in session.system_prompt
    # 用事系以外の道具は従来どおり使える
    assert "read_file" in _tool_names(session)


def test_wall_mode_keeps_named_rooms(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, mode="wall")
    Session(cfg, _EchoModel(), "work").say("仕事の相談")
    Session(cfg, _EchoModel(), "private").say("内緒の相談")
    work = Session(cfg, _EchoModel(), "work")
    contents = [m.content for m in work.history]
    assert any("仕事の相談" in c for c in contents)
    assert all("内緒" not in c for c in contents)


# ---------------------------------------------------------------- 相棒モード（ledger）

def test_ledger_mode_is_default(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel(), "main")
    assert session.mode == "ledger"
    assert TASK_TOOLS <= _tool_names(session)


def test_ledger_mode_fixes_session_to_main(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, mode="ledger")
    session = Session(cfg, _EchoModel(), session_id="仕事")
    assert session.session_id == "main"
    session.say("こんにちは")
    assert (tmp_path / "conversations" / "main.jsonl").exists()
    assert not (tmp_path / "conversations" / "仕事.jsonl").exists()
    # 用事の一覧は有効で、用事ファイルは従来の tasks.json になる
    assert TASK_TOOLS <= _tool_names(session)
    session.tools.call("create_task", {"title": "テストの用事"})
    assert (tmp_path / "tasks.json").exists()
    assert not (tmp_path / "tasks").exists()


def test_ledger_mode_injects_all_open_tasks(tmp_path: Path):
    board = TaskBoard(tmp_path)
    board.add("一つ目の用事", "メモ1")
    board.add("二つ目の用事")
    finished = board.add("終わった用事")
    board.update(finished.id, status="done")
    session = Session(Config(data_dir=tmp_path, mode="ledger"),
                      _EchoModel(), "work")
    assert session.session_id == "main"
    assert "一つ目の用事" in session.system_prompt
    assert "二つ目の用事" in session.system_prompt
    assert "終わった用事" not in session.system_prompt  # done は注入しない


# ---------------------------------------------------------------- cleanup の追随

def test_cleanup_prunes_room_task_boards(tmp_path: Path):
    now = datetime(2026, 11, 2, 12, 0, 0)

    def stamp(dt: datetime) -> str:
        return dt.isoformat(timespec="seconds")

    old = now - timedelta(days=100)
    records = [
        {"id": "1", "title": "古い完了", "status": "done", "memo": "",
         "created_at": stamp(old), "updated_at": stamp(old)},
        {"id": "2", "title": "新しい完了", "status": "done", "memo": "",
         "created_at": stamp(now), "updated_at": stamp(now)},
        {"id": "3", "title": "古い進行中", "status": "open", "memo": "",
         "created_at": stamp(old), "updated_at": stamp(old)},
    ]
    board_path = tmp_path / "tasks" / "work.json"
    board_path.parent.mkdir(parents=True)
    board_path.write_text(json.dumps(records, ensure_ascii=False),
                          encoding="utf-8")

    result = cleanup_expired(tmp_path, 90, now=now)
    assert result.tasks_removed == 1
    assert board_path.exists()  # ファイル自体は残す
    remaining = {t.id for t in TaskBoard(tmp_path, board_path).list()}
    assert remaining == {"2", "3"}


# ---------------------------------------------------------------- Web

@pytest.fixture()
def _server(tmp_path: Path):
    servers = []

    def start(mode: str) -> str:
        session = Session(Config(data_dir=tmp_path, mode=mode), _EchoModel())
        httpd = build_server(session, host="127.0.0.1", port=0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_api_meta_returns_mode_and_label(_server):
    base = _server("ledger")
    meta = _get_json(base + "/api/meta")
    assert meta["mode"] == "ledger"
    assert meta["mode_label"] == "相棒モード"
    assert meta["tenant"] == "default"
    assert meta["user"] == "default"


def test_api_meta_wall(_server):
    base = _server("wall")
    meta = _get_json(base + "/api/meta")
    assert meta["mode"] == "wall"
    assert meta["mode_label"] == "スレッドモード"


def test_page_hides_room_ui_in_ledger_mode():
    page = render_page("muse")
    # 部屋ブロックは id 付きで存在し、/api/meta の結果で隠す仕組みがある
    assert 'id="sessionBox"' in page
    assert ".sessions[hidden]" in page
    assert "/api/meta" in page
    assert "sessionBox.hidden = true" in page
    # モードの表示名をヘッダーに出す要素と処理がある
    assert 'id="modeName"' in page
    assert "mode_label" in page
