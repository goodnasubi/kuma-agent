"""用事の一覧（TaskBoard・道具・Session への注入）のテスト。"""
import json
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.tasks import TaskBoard
from kuma_agent.tools import ToolBox


# ---------------------------------------------------------------- 用事の一覧

def test_board_add_and_list(tmp_path: Path):
    board = TaskBoard(tmp_path)
    first = board.add("買い物", "牛乳と卵")
    second = board.add("原稿")
    assert first.id != second.id
    assert first.status == "open"
    assert [t.title for t in board.open_tasks()] == ["買い物", "原稿"]
    assert board.get(second.id).title == "原稿"
    assert board.get("999") is None


def test_board_update_memo_and_status(tmp_path: Path):
    board = TaskBoard(tmp_path)
    task = board.add("調べもの")
    updated = board.update(task.id, memo="半分まで進んだ")
    assert updated.memo == "半分まで進んだ"
    done = board.update(task.id, status="done")
    assert done.status == "done"
    assert board.open_tasks() == []
    assert [t.title for t in board.list()] == ["調べもの"]
    assert [t.title for t in board.list("done")] == ["調べもの"]
    assert board.update("999", memo="ない") is None


def test_board_persistence_roundtrip(tmp_path: Path):
    board = TaskBoard(tmp_path)
    task = board.add("長続きする用事", "メモ1")
    board.update(task.id, memo="メモ2")
    assert (tmp_path / "tasks.json").exists()

    reloaded = TaskBoard(tmp_path)
    restored = reloaded.get(task.id)
    assert restored is not None
    assert restored.title == "長続きする用事"
    assert restored.memo == "メモ2"
    assert restored.created_at == task.created_at


def test_board_starts_empty_on_broken_json(tmp_path: Path):
    (tmp_path / "tasks.json").write_text("{壊れた JSON", encoding="utf-8")
    board = TaskBoard(tmp_path)  # 例外で落ちない
    assert board.list() == []
    task = board.add("復旧後の用事")  # そのまま使えて保存もできる
    assert TaskBoard(tmp_path).get(task.id) is not None

    (tmp_path / "tasks.json").write_text('{"not": "a list"}', encoding="utf-8")
    assert TaskBoard(tmp_path).list() == []


def test_board_ignores_leftover_room_key_in_records(tmp_path: Path):
    # 旧データに room キーが残っていても、無視して読み込める（落とさない）
    (tmp_path / "tasks.json").write_text(
        json.dumps([{"id": "9", "title": "旧形式", "status": "open",
                     "memo": "", "room": "work"}], ensure_ascii=False),
        encoding="utf-8")
    old = TaskBoard(tmp_path).get("9")
    assert old is not None
    assert old.title == "旧形式"
    # 保存し直すと room キーは付かない
    board = TaskBoard(tmp_path)
    board.add("新しい用事")
    records = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert all("room" not in r for r in records)


def test_board_imports_legacy_room_boards(tmp_path: Path):
    board = TaskBoard(tmp_path)
    board.add("本体の用事")  # ID は "1"
    legacy = tmp_path / "tasks" / "work.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps([
        {"id": "1", "title": "衝突する用事", "status": "open", "memo": ""},
        {"id": "5", "title": "仕事部屋の用事", "status": "open", "memo": "",
         "room": "別の値"},
    ], ensure_ascii=False), encoding="utf-8")

    imported = TaskBoard(tmp_path)
    by_title = {t.title: t for t in imported.list()}
    assert by_title["本体の用事"].id == "1"
    assert by_title["衝突する用事"].id == "2"  # 衝突したら振り直す
    assert by_title["仕事部屋の用事"].id == "5"
    assert not legacy.exists()  # 取り込み済みの旧ファイルは削除する
    assert TaskBoard(tmp_path).get("2").title == "衝突する用事"


def test_board_import_skips_broken_legacy_boards(tmp_path: Path):
    legacy_dir = tmp_path / "tasks"
    legacy_dir.mkdir()
    broken = legacy_dir / "broken.json"
    broken.write_text("{壊れた JSON", encoding="utf-8")
    readable = legacy_dir / "play.json"
    readable.write_text(json.dumps([
        {"id": "1", "title": "遊び部屋の用事", "status": "open", "memo": ""},
    ], ensure_ascii=False), encoding="utf-8")

    board = TaskBoard(tmp_path)  # 壊れた旧ファイルがあっても落とさない
    assert [t.title for t in board.list()] == ["遊び部屋の用事"]
    assert broken.exists()  # 読めなかった旧ファイルは残す
    assert not readable.exists()


def test_board_skips_broken_records(tmp_path: Path):
    records = [
        {"id": "1", "title": "まともな用事", "status": "open", "memo": ""},
        {"id": "2"},  # 題名がない
        "not a dict",
        {"id": "3", "title": "状態が変な用事", "status": "変"},
    ]
    (tmp_path / "tasks.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")
    board = TaskBoard(tmp_path)
    assert [t.title for t in board.list()] == ["まともな用事", "状態が変な用事"]
    assert board.get("3").status == "open"  # 不正な状態は open に倒す


# ---------------------------------------------------------------- 道具

def test_task_tools_registered_only_with_board(tmp_path: Path):
    plain = ToolBox(tmp_path)
    plain_names = {s["name"] for s in plain.schemas()}
    assert "list_tasks" not in plain_names

    board = TaskBoard(tmp_path)
    box = ToolBox(tmp_path, task_board=board)
    names = {s["name"] for s in box.schemas()}
    assert {"list_tasks", "create_task", "update_task"} <= names


def test_task_tools_roundtrip(tmp_path: Path):
    board = TaskBoard(tmp_path)
    box = ToolBox(tmp_path, task_board=board)
    assert "用事はありません" in box.call("list_tasks", {})

    created = box.call("create_task", {"title": "旅行の計画", "memo": "宿を取る"})
    assert "登録しました" in created
    task = board.open_tasks()[0]
    assert task.title == "旅行の計画"

    listing = box.call("list_tasks", {})
    assert "旅行の計画" in listing and "宿を取る" in listing

    updated = box.call("update_task", {"id": task.id, "memo": "宿は取れた"})
    assert "更新しました" in updated
    assert TaskBoard(tmp_path).get(task.id).memo == "宿は取れた"

    box.call("update_task", {"id": task.id, "status": "done"})
    assert board.open_tasks() == []
    assert "見つかりません" in box.call("update_task", {"id": "999", "memo": "x"})


def test_task_tools_update_by_id(tmp_path: Path):
    board = TaskBoard(tmp_path)
    other = board.add("もう一つの用事")
    box = ToolBox(tmp_path, task_board=board)

    # create_task の引数に room はない（用事に部屋の印は付けない）
    create_schema = next(s for s in box.schemas() if s["name"] == "create_task")
    assert "room" not in create_schema["parameters"]["properties"]
    box.call("create_task", {"title": "新しい用事"})
    created = next(t for t in board.list() if t.title == "新しい用事")
    assert created.title == "新しい用事"

    # update_task は ID で更新できる
    updated = box.call("update_task", {"id": other.id, "status": "done"})
    assert "更新しました" in updated
    assert TaskBoard(tmp_path).get(other.id).status == "done"
    # list_tasks は完了済みも含め、状態付きで全用事を返す
    listing = box.call("list_tasks", {})
    assert "もう一つの用事" in listing and "（状態: done）" in listing


# ---------------------------------------------------------------- Session

class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


def test_session_prompt_includes_open_tasks(tmp_path: Path):
    board = TaskBoard(tmp_path)
    board.add("進行中の用事", "ここまで進んだ")
    finished = board.add("終わった用事")
    board.update(finished.id, status="done")

    session = Session(Config(data_dir=tmp_path), _EchoModel())
    assert "# 進行中の用事" in session.system_prompt
    assert "進行中の用事" in session.system_prompt
    assert "ここまで進んだ" in session.system_prompt
    assert "終わった用事" not in session.system_prompt  # done は注入しない


def test_session_prompt_has_no_tasks_section_when_empty(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    assert "# 進行中の用事" not in session.system_prompt


def test_session_can_use_task_tools(tmp_path: Path):
    """道具の循環経由で用事の一覧に登録できる（Session が board を共有する）。"""
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    result = session.tools.call("create_task", {"title": "道具経由の用事"})
    assert "登録しました" in result
    assert session.task_board.open_tasks()[0].title == "道具経由の用事"
    assert TaskBoard(tmp_path).open_tasks()[0].title == "道具経由の用事"
