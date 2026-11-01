"""モード切替（部屋で分ける / 用事で分ける / 部屋と用事で分ける）のテスト。

内部の識別子は both / wall / ledger のままで、表示名は
config.MODE_LABELS に集約する。併用モードでは台帳は部屋ごとに持つ。
"""
import json
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kuma_agent.config import MODES, MODE_LABELS, Config, mode_label, normalize_mode
from kuma_agent.llm import Message
from kuma_agent.memory import cleanup_expired, tasks_file_for
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
    assert normalize_mode("both") == "both"
    assert normalize_mode(" WALL ") == "wall"
    assert normalize_mode("ledger") == "ledger"
    assert normalize_mode("わからない") == "both"
    assert normalize_mode("") == "both"
    assert normalize_mode(None) == "both"


def test_config_mode_default_env_and_invalid(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KUMA_MODE", raising=False)
    assert Config().mode == "both"
    monkeypatch.setenv("KUMA_MODE", "ledger")
    assert Config().mode == "ledger"
    monkeypatch.setenv("KUMA_MODE", "変な値")
    assert Config().mode == "both"  # 不正な環境変数は既定に倒す
    # 直接指定した場合も __post_init__ で正規化される
    assert Config(mode="wall").mode == "wall"
    assert Config(mode="おかしい").mode == "both"


def test_mode_labels_cover_all_modes():
    assert set(MODE_LABELS) == set(MODES)
    assert MODE_LABELS["wall"] == "部屋で分ける"
    assert MODE_LABELS["ledger"] == "用事で分ける"
    assert MODE_LABELS["both"] == "部屋と用事で分ける"
    assert mode_label("ledger") == "用事で分ける"
    assert mode_label("不正な値") == "部屋と用事で分ける"


# ---------------------------------------------------------------- 部屋で分ける（wall）

def test_wall_mode_disables_task_tools_and_injection(tmp_path: Path):
    # 既定の部屋の台帳に進行中の用事があっても、道具にも注入にも出ない
    TaskBoard(tmp_path).add("既にある用事", "メモ")
    session = Session(Config(data_dir=tmp_path, mode="wall"), _EchoModel())
    assert _tool_names(session).isdisjoint(TASK_TOOLS)
    assert "# 進行中の用事" not in session.system_prompt
    assert "既にある用事" not in session.system_prompt
    # 台帳以外の道具は従来どおり使える
    assert "read_file" in _tool_names(session)


# ---------------------------------------------------------------- 用事で分ける（ledger）

def test_ledger_mode_fixes_session_to_main(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, mode="ledger")
    session = Session(cfg, _EchoModel(), session_id="仕事")
    assert session.session_id == "main"
    session.say("こんにちは")
    assert (tmp_path / "conversations" / "main.jsonl").exists()
    assert not (tmp_path / "conversations" / "仕事.jsonl").exists()
    # 台帳は有効で、台帳ファイルは従来の tasks.json になる
    assert TASK_TOOLS <= _tool_names(session)
    session.tools.call("create_task", {"title": "台帳の用事"})
    assert (tmp_path / "tasks.json").exists()
    assert not (tmp_path / "tasks").exists()


# ---------------------------------------------------------------- 部屋と用事で分ける（both）

def test_both_mode_is_default_and_keeps_both(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel(), "main")
    assert session.mode == "both"
    assert TASK_TOOLS <= _tool_names(session)


def test_tasks_file_for_paths(tmp_path: Path):
    assert tasks_file_for(tmp_path, "main") == tmp_path / "tasks.json"
    assert tasks_file_for(tmp_path, None) == tmp_path / "tasks.json"
    assert tasks_file_for(tmp_path, "仕事") == tmp_path / "tasks" / "仕事.json"


def test_both_mode_task_boards_are_per_room(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, mode="both")
    main_room = Session(cfg, _EchoModel(), "main")
    main_room.tools.call("create_task", {"title": "main の用事", "memo": "main用"})
    work_room = Session(cfg, _EchoModel(), "work")
    work_room.tools.call("create_task", {"title": "work の用事", "memo": "work用"})

    # 台帳ファイルは部屋ごとに分かれる（main は従来パス）
    assert (tmp_path / "tasks.json").exists()
    assert (tmp_path / "tasks" / "work.json").exists()

    # 道具経由では相手の部屋の用事が見えない
    assert "main の用事" in main_room.tools.call("list_tasks", {})
    assert "work の用事" not in main_room.tools.call("list_tasks", {})
    assert "work の用事" in work_room.tools.call("list_tasks", {})
    assert "main の用事" not in work_room.tools.call("list_tasks", {})

    # 注入も自分の部屋の用事だけ（作り直した Session で確認する）
    main_again = Session(cfg, _EchoModel(), "main")
    assert "main の用事" in main_again.system_prompt
    assert "work の用事" not in main_again.system_prompt
    work_again = Session(cfg, _EchoModel(), "work")
    assert "work の用事" in work_again.system_prompt
    assert "main の用事" not in work_again.system_prompt


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
    base = _server("both")
    meta = _get_json(base + "/api/meta")
    assert meta["mode"] == "both"
    assert meta["mode_label"] == "部屋と用事で分ける"
    assert meta["tenant"] == "default"
    assert meta["user"] == "default"


def test_api_meta_ledger(_server):
    base = _server("ledger")
    meta = _get_json(base + "/api/meta")
    assert meta["mode"] == "ledger"
    assert meta["mode_label"] == "用事で分ける"


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
