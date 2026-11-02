"""リマインダー（時刻指定の通知）のテスト。

用事の remind_at / reminded_at の保存と旧形式との互換、道具経由の
設定・取り消し、能動発信の巡回で時刻到来した用事だけが 1 回だけ
通知されること、日報の上限と独立していることを確認する。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.proactive import check_and_post
from kuma_agent.session import Session, current_time_text
from kuma_agent.settings import set_proactive_enabled
from kuma_agent.tasks import TaskBoard
from kuma_agent.tools import ToolBox

NOW = datetime(2026, 11, 2, 9, 0, 0)
DUE = "2026-11-02T08:30:00"       # NOW より前（到来済み）
FUTURE = "2026-11-03T09:00:00"    # NOW より後（まだ）


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


def _records(user_dir: Path) -> list[dict]:
    path = user_dir / "conversations" / "main.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- 項目の保存

def test_old_format_tasks_json_loads_without_remind_fields(tmp_path: Path):
    # リマインダー項目のない旧形式の tasks.json がそのまま読める
    (tmp_path / "tasks.json").write_text(json.dumps([
        {"id": "1", "title": "旧形式の用事", "status": "open", "memo": "メモ",
         "created_at": "2026-11-01T09:00:00",
         "updated_at": "2026-11-01T09:00:00"},
    ]), encoding="utf-8")
    board = TaskBoard(tmp_path)
    task = board.get("1")
    assert task is not None
    assert task.remind_at == ""      # 未設定として扱う
    assert task.reminded_at == ""    # 未通知として扱う


def test_remind_at_roundtrip(tmp_path: Path):
    board = TaskBoard(tmp_path)
    task = board.add("薬を飲む", remind_at=DUE)
    assert task.remind_at == DUE

    restored = TaskBoard(tmp_path).get(task.id)
    assert restored is not None
    assert restored.remind_at == DUE
    assert restored.reminded_at == ""

    board.mark_reminded(task.id)
    restored = TaskBoard(tmp_path).get(task.id)
    assert restored is not None
    assert restored.reminded_at != ""


def test_remind_at_invalid_format_rejected(tmp_path: Path):
    board = TaskBoard(tmp_path)
    for bad_call in (lambda: board.add("用事", remind_at="明日の朝"),
                     lambda: board.update(
                         board.add("用事").id, remind_at="9時")):
        try:
            bad_call()
        except ValueError:
            pass
        else:
            raise AssertionError("不正な通知時刻が通ってしまった")


# ---------------------------------------------------------------- 道具

def test_tools_set_and_clear_remind_at(tmp_path: Path):
    board = TaskBoard(tmp_path)
    box = ToolBox(tmp_path, task_board=board)

    out = box.call("create_task", {"title": "会議", "remind_at": FUTURE})
    assert out.startswith("登録しました")
    task = board.list()[0]
    assert task.remind_at == FUTURE

    # 表示にも通知時刻が含まれる
    listing = box.call("list_tasks", {})
    assert "会議" in listing
    assert "11/3 9:00 に通知" in listing

    # 付け替えると通知済みの印も消える
    board.mark_reminded(task.id)
    box.call("update_task", {"id": task.id, "remind_at": DUE})
    task = board.get(task.id)
    assert task is not None
    assert task.remind_at == DUE
    assert task.reminded_at == ""

    # 空文字列で取り消し
    box.call("update_task", {"id": task.id, "remind_at": ""})
    task = board.get(task.id)
    assert task is not None
    assert task.remind_at == ""

    # 不正な形式は error として返る（道具の失敗で会話は落とさない）
    out = box.call("create_task", {"title": "用事", "remind_at": "あした"})
    assert out.startswith("error:")


def test_prompt_injection_includes_remind_at(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    session.task_board.add("会議", remind_at=FUTURE)
    session2 = Session(cfg, _EchoModel())
    assert "会議" in session2.system_prompt
    assert "11/3 9:00 に通知" in session2.system_prompt


def test_current_time_text_has_weekday():
    # 2026-11-02 は月曜日
    text = current_time_text(datetime(2026, 11, 2, 14, 5, 0))
    assert "2026-11-02T14:05:00" in text
    assert "月" in text


def test_say_passes_current_datetime_to_model(tmp_path: Path):
    seen: list[str] = []

    class _Capture:
        def chat(self, system_prompt, messages, tools):
            seen.append(system_prompt)
            return Message("assistant", "はい")

    session = Session(Config(data_dir=tmp_path), _Capture())
    session.say("こんにちは")
    assert seen and "# 現在日時" in seen[0]
    assert "現在日時" in seen[0]


# ---------------------------------------------------------------- 巡回

def _board_with(tmp_path: Path, *specs: tuple[str, str, str]) -> TaskBoard:
    board = TaskBoard(tmp_path)
    for title, remind_at, status in specs:
        task = board.add(title, remind_at=remind_at)
        if status == "done":
            board.update(task.id, status="done")
    return board


def test_due_reminder_posts_once(tmp_path: Path):
    _board_with(tmp_path,
                ("薬を飲む", DUE, "open"),
                ("明日の会議", FUTURE, "open"))
    cfg = Config(data_dir=tmp_path)
    text = check_and_post(cfg, now=NOW)
    assert text is not None
    assert "薬を飲む" in text
    assert "通知の時刻になった用事が 1 件" in text

    # 通知済みが記録される
    board = TaskBoard(tmp_path)
    reminded = {t.title: t.reminded_at for t in board.list()}
    assert reminded["薬を飲む"] != ""
    assert reminded["明日の会議"] == ""

    records = _records(tmp_path)
    # リマインダーの発言が先に投稿される
    assert "通知の時刻になった用事" in records[0]["content"]

    # 翌日に巡回しても、「薬を飲む」のリマインダーは二度と出ない
    check_and_post(cfg, now=NOW + timedelta(days=1))
    reminder_posts = [r for r in _records(tmp_path)
                      if "通知の時刻になった用事" in r["content"]
                      and "薬を飲む" in r["content"]]
    assert len(reminder_posts) == 1


def test_future_reminder_not_posted_yet(tmp_path: Path):
    _board_with(tmp_path, ("明日の会議", FUTURE, "open"))
    cfg = Config(data_dir=tmp_path)
    # 日報は出るが、リマインダーとしてはまだ通知されない
    text = check_and_post(cfg, now=NOW)
    assert text is not None
    assert "通知の時刻になった用事" not in text
    assert TaskBoard(tmp_path).list()[0].reminded_at == ""


def test_multiple_due_reminders_in_one_message(tmp_path: Path):
    _board_with(tmp_path,
                ("薬を飲む", DUE, "open"),
                ("水やり", "2026-11-02T08:45:00", "open"),
                ("明日の会議", FUTURE, "open"))
    text = check_and_post(Config(data_dir=tmp_path), now=NOW)
    assert text is not None
    assert "通知の時刻になった用事が 2 件" in text
    assert "薬を飲む" in text and "水やり" in text
    # リマインダーは 1 件の発言にまとまる
    reminder_records = [r for r in _records(tmp_path)
                        if "通知の時刻になった用事" in r["content"]]
    assert len(reminder_records) == 1


def test_done_task_not_reminded(tmp_path: Path):
    _board_with(tmp_path, ("終わった用事", DUE, "done"))
    assert check_and_post(Config(data_dir=tmp_path), now=NOW) is None
    assert _records(tmp_path) == []
    assert TaskBoard(tmp_path).list()[0].reminded_at == ""


def test_reminder_not_posted_when_tenant_off(tmp_path: Path):
    user_dir = tmp_path / "tenants" / "acme"
    _board_with(user_dir, ("acmeの用事", DUE, "open"))
    set_proactive_enabled(tmp_path, "acme", False)
    cfg = Config(data_dir=tmp_path, tenant="acme")
    assert check_and_post(cfg, now=NOW) is None
    assert _records(user_dir) == []
    assert TaskBoard(user_dir).list()[0].reminded_at == ""


def test_reminder_independent_of_daily_limit(tmp_path: Path):
    _board_with(tmp_path,
                ("いつもの用事", "", "open"),
                ("薬を飲む", DUE, "open"))
    cfg = Config(data_dir=tmp_path)
    # まず日報だけを先に投稿済みの状態にする（リマインダー対象を除く）
    board = TaskBoard(tmp_path)
    drug = [t for t in board.list() if t.title == "薬を飲む"][0]
    board.update(drug.id, remind_at=FUTURE)  # 一旦未来にしておく
    text = check_and_post(cfg, now=NOW)
    assert text is not None and "通知の時刻になった用事" not in text
    assert len(_records(tmp_path)) == 1  # 日報のみ

    # 同じ日にリマインダーの時刻が来たら、日報の上限とは独立に発言する
    board.update(drug.id, remind_at=DUE)
    text = check_and_post(cfg, now=NOW + timedelta(hours=1))
    assert text is not None
    assert "通知の時刻になった用事" in text
    assert "薬を飲む" in text
    # 日報は同日に 2 回出ない（増えたのはリマインダーの 1 件だけ）
    assert len(_records(tmp_path)) == 2


def test_reminder_with_offset_interpreted_in_local_time(tmp_path: Path):
    # オフセット付きの時刻もローカル時刻に直して判定する
    local_due = (NOW - timedelta(minutes=30)).astimezone().isoformat(
        timespec="seconds")
    _board_with(tmp_path, ("薬を飲む", local_due, "open"))
    text = check_and_post(Config(data_dir=tmp_path), now=NOW)
    assert text is not None
    assert "通知の時刻になった用事" in text
