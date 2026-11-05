"""目標（長期の目標と進捗）のテスト。

保存と読み出し、進み具合の算出（紐づいた用事の完了割合: 0件・
一部完了・全完了）、進捗メモの追加、用事との紐づけと解除、目標を
消しても用事が残ること、既存の用事データ（goal_id なし）が
そのまま読めること、API 経由の一連の流れを確認する。紐づけだけの
変更がフィードに残らないことも確かめる。
"""
import json
import threading
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.feed import load_feed_entries
from kuma_agent.goals import (
    GOALS_FILE,
    STATUS_ACTIVE,
    STATUS_DONE,
    GoalBoard,
    delete_goal,
    goal_view,
    link_task,
    progress_of,
    unlink_task,
)
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.tasks import TaskBoard
from kuma_agent.web import build_server, render_page

T1 = datetime(2026, 11, 5, 9, 0, 0)
T2 = datetime(2026, 11, 5, 10, 30, 0)


# ------------------------------------------------------------ 保存と読み出し

def test_add_and_list_newest_first(tmp_path: Path):
    board = GoalBoard(tmp_path)
    board.add("一つ目", now=T1)
    board.add("二つ目", description="説明2", now=T2)

    goals = GoalBoard(tmp_path).list()
    assert [g.title for g in goals] == ["二つ目", "一つ目"]
    assert goals[0].description == "説明2"
    assert goals[0].status == STATUS_ACTIVE
    assert goals[0].created_at == "2026-11-05T10:30:00"
    assert goals[1].description == ""
    assert goals[1].notes == []


def test_add_requires_title(tmp_path: Path):
    board = GoalBoard(tmp_path)
    for bad in ("", "  "):
        try:
            board.add(bad)
        except ValueError:
            pass
        else:
            raise AssertionError("空のタイトルが通ってしまった")
    assert GoalBoard(tmp_path).list() == []


def test_update_fields_and_status(tmp_path: Path):
    board = GoalBoard(tmp_path)
    goal = board.add("旧タイトル", now=T1)

    updated = board.update(goal.id, title="新タイトル",
                           description="新しい説明", status=STATUS_DONE)
    assert updated is not None
    assert updated.title == "新タイトル"
    assert updated.description == "新しい説明"
    assert updated.status == STATUS_DONE
    # 保存にも残る。None の項目は変わらない
    reloaded = GoalBoard(tmp_path).get(goal.id)
    assert reloaded is not None
    assert reloaded.title == "新タイトル"
    assert board.update(goal.id, status=STATUS_ACTIVE) is not None
    assert GoalBoard(tmp_path).get(goal.id).title == "新タイトル"
    # 対象なし・不正な状態
    assert board.update("ない", title="x") is None
    for kwargs in ({"status": "???"}, {"title": " "}):
        try:
            board.update(goal.id, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"不正な更新が通ってしまった: {kwargs}")


def test_load_skips_broken_data(tmp_path: Path):
    path = tmp_path / GOALS_FILE
    path.write_text(json.dumps([
        {"id": "1", "title": "有効", "status": "done",
         "notes": [{"ts": "2026-11-05T09:00:00", "body": "メモ"},
                   {"ts": "x"}, "辞書でない"]},
        {"id": "2", "title": "", "description": "題名が空"},
        {"id": "3", "description": "題名が無い"},
        "辞書でない",
        {"id": "4", "title": "状態が変", "status": "???"},
    ], ensure_ascii=False), encoding="utf-8")
    goals = GoalBoard(tmp_path).list()
    assert [g.title for g in goals] == ["状態が変", "有効"]
    # 変な状態は active として読み直し、不正なメモは読み飛ばす
    assert goals[0].status == STATUS_ACTIVE
    valid = goals[1]
    assert valid.status == STATUS_DONE
    assert [n.body for n in valid.notes] == ["メモ"]
    # ファイル自体が壊れていれば空として始める
    path.write_text("{壊れた JSON", encoding="utf-8")
    assert GoalBoard(tmp_path).list() == []


# ------------------------------------------------------------ 進捗メモ

def test_notes_append_and_newest_first_in_view(tmp_path: Path):
    board = GoalBoard(tmp_path)
    goal = board.add("目標", now=T1)
    board.add_note(goal.id, "一歩目", now=T1)
    board.add_note(goal.id, "二歩目", now=T2)

    reloaded = GoalBoard(tmp_path).get(goal.id)
    assert reloaded is not None
    assert [n.body for n in reloaded.notes] == ["一歩目", "二歩目"]
    assert reloaded.notes[0].ts == "2026-11-05T09:00:00"
    # API 向け表現では新しい順
    view = goal_view(reloaded, [])
    assert [n["body"] for n in view["notes"]] == ["二歩目", "一歩目"]
    # 対象なし・空の本文
    assert board.add_note("ない", "メモ") is None
    try:
        board.add_note(goal.id, "  ")
    except ValueError:
        pass
    else:
        raise AssertionError("空のメモが通ってしまった")


# ------------------------------------------------------------ 進み具合

def _linked_board(tmp_path: Path, statuses: list[str]):
    """目標と、指定の状態の用事を紐づけて返す（目標, 用事の並び）。"""
    goal = GoalBoard(tmp_path).add("目標", now=T1)
    tasks = TaskBoard(tmp_path)
    linked = []
    for i, status in enumerate(statuses):
        task = tasks.add(f"用事{i + 1}")
        if status == "done":
            tasks.update(task.id, status="done")
        assert link_task(tmp_path, goal.id, task.id) is not None
        linked.append(task)
    return goal, linked


def test_progress_no_tasks(tmp_path: Path):
    goal = GoalBoard(tmp_path).add("目標", now=T1)
    progress = progress_of(TaskBoard(tmp_path).list_for_goal(goal.id))
    assert progress.total == 0
    assert progress.done == 0
    assert progress.ratio is None  # 用事が無ければ割合なし
    view = goal_view(GoalBoard(tmp_path).get(goal.id),
                     TaskBoard(tmp_path).list())
    assert view["progress"] == {"total": 0, "done": 0, "ratio": None}
    assert view["tasks"] == []


def test_progress_partial_and_full(tmp_path: Path):
    goal, linked = _linked_board(tmp_path, ["done", "open", "open"])
    view = goal_view(GoalBoard(tmp_path).get(goal.id),
                     TaskBoard(tmp_path).list())
    assert view["progress"]["total"] == 3
    assert view["progress"]["done"] == 1
    assert abs(view["progress"]["ratio"] - 1 / 3) < 1e-9
    assert [t["title"] for t in view["tasks"]] == ["用事1", "用事2", "用事3"]
    # 紐づいていない用事は数に入らない
    TaskBoard(tmp_path).add("関係ない用事")
    view = goal_view(GoalBoard(tmp_path).get(goal.id),
                     TaskBoard(tmp_path).list())
    assert view["progress"]["total"] == 3
    # 残りを完了させると全完了になる
    tasks = TaskBoard(tmp_path)
    for task in linked:
        tasks.update(task.id, status="done")
    view = goal_view(GoalBoard(tmp_path).get(goal.id),
                     TaskBoard(tmp_path).list())
    assert view["progress"] == {"total": 3, "done": 3, "ratio": 1.0}


# ------------------------------------------------------------ 紐づけと削除

def test_link_and_unlink(tmp_path: Path):
    goal = GoalBoard(tmp_path).add("目標", now=T1)
    task = TaskBoard(tmp_path).add("用事")

    linked_goal, linked_task = link_task(tmp_path, goal.id, task.id)
    assert linked_goal.id == goal.id
    assert linked_task.goal_id == goal.id
    # 保存にも残る
    assert TaskBoard(tmp_path).get(task.id).goal_id == goal.id

    unlinked = unlink_task(tmp_path, task.id)
    assert unlinked is not None
    assert unlinked.goal_id == ""
    assert TaskBoard(tmp_path).get(task.id).goal_id == ""
    # 対象が無ければ None
    assert link_task(tmp_path, "ない", task.id) is None
    assert link_task(tmp_path, goal.id, "ない") is None
    assert unlink_task(tmp_path, "ない") is None


def test_link_moves_between_goals(tmp_path: Path):
    goals = GoalBoard(tmp_path)
    first = goals.add("目標1", now=T1)
    second = goals.add("目標2", now=T1)
    task = TaskBoard(tmp_path).add("用事")
    link_task(tmp_path, first.id, task.id)
    # 別の目標へ紐づけ直すと付け替わる（goal_id は 1 つだけ）
    link_task(tmp_path, second.id, task.id)
    assert TaskBoard(tmp_path).get(task.id).goal_id == second.id
    assert TaskBoard(tmp_path).list_for_goal(first.id) == []
    assert len(TaskBoard(tmp_path).list_for_goal(second.id)) == 1


def test_delete_goal_keeps_tasks(tmp_path: Path):
    goal, linked = _linked_board(tmp_path, ["open", "done"])
    assert delete_goal(tmp_path, goal.id) is True
    assert GoalBoard(tmp_path).list() == []
    tasks = TaskBoard(tmp_path).list()
    # 用事自体は残り、紐づけだけが外れる
    assert len(tasks) == 2
    assert all(t.goal_id == "" for t in tasks)
    assert delete_goal(tmp_path, goal.id) is False


def test_linking_does_not_write_feed(tmp_path: Path):
    # 紐づけだけの変更は用事の進み具合ではないので、フィードに
    # 「用事の進み具合」の記録を増やさない（追加時の 1 件だけ）
    goal = GoalBoard(tmp_path).add("目標", now=T1)
    task = TaskBoard(tmp_path).add("用事")
    before = load_feed_entries(tmp_path, limit=100)
    assert len(before) == 1
    link_task(tmp_path, goal.id, task.id)
    unlink_task(tmp_path, task.id)
    delete_goal(tmp_path, goal.id)
    after = load_feed_entries(tmp_path, limit=100)
    assert len(after) == 1


# ------------------------------------------------------------ 後方互換

def test_legacy_tasks_without_goal_id(tmp_path: Path):
    # goal_id の無い既存の用事データがそのまま読める
    (tmp_path / "tasks.json").write_text(json.dumps([
        {"id": "1", "title": "昔の用事", "status": "open",
         "memo": "", "created_at": "2026-11-01T09:00:00",
         "updated_at": "2026-11-01T09:00:00", "remind_at": "",
         "reminded_at": "", "room": "main"},
    ], ensure_ascii=False), encoding="utf-8")
    tasks = TaskBoard(tmp_path).list()
    assert len(tasks) == 1
    assert tasks[0].goal_id == ""
    # 更新して保存し直しても壊れない
    TaskBoard(tmp_path).update("1", memo="メモ")
    reloaded = TaskBoard(tmp_path).get("1")
    assert reloaded is not None
    assert reloaded.memo == "メモ"
    assert reloaded.goal_id == ""


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
        with urllib.request.urlopen(base + path) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_goals_over_http(tmp_path: Path):
    # 用事は画面の API が無いので、ボードから直接用意する
    board = TaskBoard(tmp_path)
    task_a = board.add("用事A")
    task_b = board.add("用事B")
    board.update(task_b.id, status="done")

    with _running(tmp_path) as base:
        # 最初は空。用事は選択肢として返る
        status, data = _request(base, "/api/goals")
        assert status == 200
        assert data["tenant"] == "default"
        assert data["user"] == "default"
        assert data["goals"] == []
        assert {t["id"] for t in data["tasks"]} == {task_a.id, task_b.id}

        # 目標を追加
        status, data = _request(base, "/api/goals/act", {
            "action": "create", "title": "健康になる",
            "description": "毎朝歩く"})
        assert status == 200
        assert len(data["goals"]) == 1
        goal = data["goals"][0]
        assert goal["title"] == "健康になる"
        assert goal["description"] == "毎朝歩く"
        assert goal["status"] == "active"
        assert goal["progress"] == {"total": 0, "done": 0, "ratio": None}

        # 用事を 2 件紐づけると進み具合が出る（1/2 完了）
        for task in (task_a, task_b):
            status, data = _request(base, "/api/goals/act", {
                "action": "link", "id": goal["id"], "task_id": task.id})
            assert status == 200
        goal = data["goals"][0]
        assert goal["progress"] == {"total": 2, "done": 1, "ratio": 0.5}
        assert {t["id"] for t in goal["tasks"]} == {task_a.id, task_b.id}
        listed = {t["id"]: t for t in data["tasks"]}
        assert listed[task_a.id]["goal_id"] == goal["id"]

        # 進捗メモを追加（新しい順に返る）
        _request(base, "/api/goals/act", {
            "action": "note", "id": goal["id"], "body": "歩き始めた"})
        status, data = _request(base, "/api/goals/act", {
            "action": "note", "id": goal["id"], "body": "1週間続いた"})
        assert status == 200
        assert [n["body"] for n in data["goals"][0]["notes"]] == [
            "1週間続いた", "歩き始めた"]

        # 編集（状態を達成に）
        status, data = _request(base, "/api/goals/act", {
            "action": "update", "id": goal["id"], "status": "done",
            "title": "健康になった"})
        assert status == 200
        assert data["goals"][0]["status"] == "done"
        assert data["goals"][0]["title"] == "健康になった"

        # 紐づけを外す
        status, data = _request(base, "/api/goals/act", {
            "action": "unlink", "task_id": task_a.id})
        assert status == 200
        assert data["goals"][0]["progress"]["total"] == 1

        # 削除すると目標は消え、用事は紐づけだけ外れて残る
        status, data = _request(base, "/api/goals/act", {
            "action": "delete", "id": goal["id"]})
        assert status == 200
        assert data["goals"] == []
        tasks = TaskBoard(tmp_path).list()
        assert len(tasks) == 2
        assert all(t.goal_id == "" for t in tasks)

        # 不正な操作
        status, _ = _request(base, "/api/goals/act", {"action": "???"})
        assert status == 400
        status, _ = _request(base, "/api/goals/act", {
            "action": "create", "title": "  "})
        assert status == 400
        status, _ = _request(base, "/api/goals/act", {
            "action": "update", "id": "ない", "title": "x"})
        assert status == 404
        status, _ = _request(base, "/api/goals/act", {
            "action": "link", "id": "ない", "task_id": task_a.id})
        assert status == 404
        status, _ = _request(base, "/api/goals/act", {
            "action": "note", "id": "ない", "body": "メモ"})
        assert status == 404


def test_render_page_has_goals_ui():
    page = render_page("muse")
    assert 'id="goalsPanel"' in page
    assert 'id="goalRows"' in page
    assert 'id="goalCreate"' in page
    assert "目標はまだありません" in page
    assert "メモを残す" in page
    assert "この用事を紐づける" in page
    assert "紐づけを外す" in page
    assert "/api/goals" in page
    assert "loadGoals" in page
    # 目標の「準備中」表示は無くなっている
    assert "準備中です。これから、長期の目標と進捗をここに残せるようにします" \
        not in page
