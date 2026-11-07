"""目標の進み具合の観測（goal_progress、AF-5）のテスト。

割合が変わったらフィード（種別 task）に 1 件残ること、変わら
なければ残らないこと、初回の観測では残らないこと、達成への
変化が分かる形で残ることを押さえる。観測は 1 日 1 回まで。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from kuma_agent.feed import KIND_TASK, load_feed_entries
from kuma_agent.goal_progress import observe_goal_progress
from kuma_agent.goals import GoalBoard
from kuma_agent.tasks import STATUS_DONE, TaskBoard

DAY1 = datetime(2026, 11, 7, 9, 0)
DAY2 = datetime(2026, 11, 8, 9, 0)
DAY3 = datetime(2026, 11, 9, 9, 0)


def _setup(tmp_path: Path, total: int = 2, done: int = 0) -> str:
    """目標 1 件と、紐づいた用事 total 件（うち done 件完了）を作る。"""
    goal = GoalBoard(tmp_path).add("語学の資格を取る")
    board = TaskBoard(tmp_path)
    for i in range(total):
        task = board.add(f"用事{i + 1}")
        board.update(task.id, goal_id=goal.id)
        if i < done:
            board.update(task.id, status=STATUS_DONE)
    return goal.id


def _goal_feed_bodies(tmp_path: Path) -> list[str]:
    """フィードのうち目標の進み具合の記録だけの本文を返す。"""
    return [e.body for e in load_feed_entries(tmp_path, kind=KIND_TASK)
            if e.body.startswith("目標「")]


def _complete_one(tmp_path: Path) -> None:
    board = TaskBoard(tmp_path)
    for task in board.list():
        if task.status != STATUS_DONE:
            board.update(task.id, status=STATUS_DONE)
            return
    raise AssertionError("完了できる用事が無い")


def test_records_when_ratio_changes(tmp_path: Path):
    _setup(tmp_path, total=2, done=0)
    assert observe_goal_progress(tmp_path, now=DAY1) == []  # 基準
    _complete_one(tmp_path)
    recorded = observe_goal_progress(tmp_path, now=DAY2)
    assert len(recorded) == 1
    assert "目標「語学の資格を取る」" in recorded[0]
    assert "2件中0件完了 → 2件中1件完了" in recorded[0]
    assert _goal_feed_bodies(tmp_path) == recorded


def test_no_record_when_unchanged(tmp_path: Path):
    _setup(tmp_path, total=2, done=1)
    assert observe_goal_progress(tmp_path, now=DAY1) == []
    assert observe_goal_progress(tmp_path, now=DAY2) == []
    assert _goal_feed_bodies(tmp_path) == []


def test_first_observation_takes_baseline_only(tmp_path: Path):
    # 初回の観測の時点で既に進んでいても、記録はしない
    _setup(tmp_path, total=3, done=2)
    assert observe_goal_progress(tmp_path, now=DAY1) == []
    assert _goal_feed_bodies(tmp_path) == []
    # 基準が取れたので、その後の変化は翌日に記録される
    _complete_one(tmp_path)
    recorded = observe_goal_progress(tmp_path, now=DAY2)
    assert len(recorded) == 1


def test_achievement_is_recorded_as_completion(tmp_path: Path):
    _setup(tmp_path, total=2, done=1)
    assert observe_goal_progress(tmp_path, now=DAY1) == []
    _complete_one(tmp_path)
    recorded = observe_goal_progress(tmp_path, now=DAY2)
    assert len(recorded) == 1
    assert "全て完了しました" in recorded[0]
    assert "2件中2件完了" in recorded[0]
    # 達成のままでは翌日も記録しない
    assert observe_goal_progress(tmp_path, now=DAY3) == []


def test_at_most_once_a_day(tmp_path: Path):
    _setup(tmp_path, total=2, done=0)
    assert observe_goal_progress(tmp_path, now=DAY1) == []
    _complete_one(tmp_path)
    # 同じ日の 2 回目は観測自体をしない（変化は翌日に回る）
    assert observe_goal_progress(
        tmp_path, now=datetime(2026, 11, 7, 18, 0)) == []
    assert len(observe_goal_progress(tmp_path, now=DAY2)) == 1


def test_goal_without_tasks_becomes_baseline_when_linked(tmp_path: Path):
    goal = GoalBoard(tmp_path).add("本を書く")
    assert observe_goal_progress(tmp_path, now=DAY1) == []
    board = TaskBoard(tmp_path)
    task = board.add("目次を作る")
    board.update(task.id, goal_id=goal.id)
    board.update(task.id, status=STATUS_DONE)
    # 用事が付いた最初の観測がその目標の基準になる（記録しない）
    assert observe_goal_progress(tmp_path, now=DAY2) == []
    assert _goal_feed_bodies(tmp_path) == []
