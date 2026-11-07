"""目標の進み具合の観測と、変化のフィードへの記録（AF-5）。

進行中の目標について、紐づいた用事の完了割合（goals.progress_of）
を観測し、前回の観測から変わっていたらフィードへ 1 件残す
（種別は用事の記録と同じ task）。会話へは投稿せず、モデルも
使わない。

観測は巡回（proactive.check_and_post）から 1 日 1 回まで呼ばれる。
置き場を巡回にしたのは、割合の変化が用事の状態変更の経路のどこ
でも起き得る（画面・会話・紐づけの付け替え）ため、変更のたびに
目標単位で集計し直すより、長期記憶の棚卸し（memory_audit）と同じ
「1 日 1 回まとめて観測する」形の方が既存の仕組みに自然で、
記録の頻度も穏やかになるからである。

前回の観測結果はユーザーの置き場の goal_progress_state.json に
持つ（memory_audit_state.json と同じ流儀）。初回の観測では記録
せず、基準だけを取る。紐づいた用事が 1 件も無い目標は割合が
無いため観測の対象にしない（用事が付いた時点の観測がその目標の
基準になる）。目標が消えた・達成済みになった・紐づけが全て
外れた場合は、状態から静かに落とす（割合の変化ではない）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .feed import KIND_TASK, append_feed_entry
from .goals import STATUS_ACTIVE, GoalBoard, progress_of
from .tasks import TaskBoard

STATE_FILENAME = "goal_progress_state.json"


def _load_state(user_data_dir: Path) -> dict:
    """goal_progress_state.json を読む。無い・壊れていたら空の辞書。"""
    try:
        raw = json.loads(
            (user_data_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(user_data_dir: Path, state: dict) -> None:
    """goal_progress_state.json を書き直す（他の項目は保持する）。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False)
    (user_data_dir / STATE_FILENAME).write_text(
        text + "\n", encoding="utf-8")


def _observed_progress(user_data_dir: Path) -> dict[str, tuple[int, int]]:
    """進行中の目標ごとの現在の進み具合（総数, 完了数）を返す。

    紐づいた用事が 1 件も無い目標は含めない。
    """
    tasks = TaskBoard(user_data_dir).list()
    observed: dict[str, tuple[int, int]] = {}
    for goal in GoalBoard(user_data_dir).list():
        if goal.status != STATUS_ACTIVE:
            continue
        linked = [t for t in tasks if t.goal_id == goal.id]
        progress = progress_of(linked)
        if progress.total:
            observed[goal.id] = (progress.total, progress.done)
    return observed


def _describe_change(title: str, prev: tuple[int, int],
                     current: tuple[int, int]) -> str:
    """進み具合の変化をフィードへ残す短い文面。

    紐づいた用事が全て完了した変化は、達成と分かる形にする。
    """
    total, done = current
    if done == total:
        return (f"目標「{title}」に紐づいた用事が全て完了しました"
                f"（{total}件中{total}件完了）")
    prev_total, prev_done = prev
    return (f"目標「{title}」の進み具合が変わりました"
            f"（{prev_total}件中{prev_done}件完了 → "
            f"{total}件中{done}件完了）")


def observe_goal_progress(user_data_dir: Path,
                          now: datetime | None = None) -> list[str]:
    """目標の進み具合を観測し、変化があればフィードへ残す。

    1 日 1 回まで（同じ日の 2 回目以降は何もしない）。記録した
    文面の並びを返す。初回の観測（目標ごとの基準が無い場合を
    含む）は記録せず、基準だけを取る。
    """
    now = now or datetime.now()
    today = now.date().isoformat()
    state = _load_state(user_data_dir)
    if state.get("last_observed_on") == today:
        return []
    raw_prev = state.get("progress")
    prev: dict[str, tuple[int, int]] = {}
    if isinstance(raw_prev, dict):
        for goal_id, pair in raw_prev.items():
            if (isinstance(pair, dict)
                    and isinstance(pair.get("total"), int)
                    and isinstance(pair.get("done"), int)):
                prev[str(goal_id)] = (pair["total"], pair["done"])

    goals = {g.id: g for g in GoalBoard(user_data_dir).list()}
    recorded: list[str] = []
    current = _observed_progress(user_data_dir)
    for goal_id, pair in current.items():
        before = prev.get(goal_id)
        if before is None or before == pair:
            continue  # 基準が無い（初回）か、割合が変わっていない
        body = _describe_change(goals[goal_id].title, before, pair)
        append_feed_entry(user_data_dir, KIND_TASK, body, now=now)
        recorded.append(body)

    state["last_observed_on"] = today
    state["progress"] = {
        goal_id: {"total": total, "done": done}
        for goal_id, (total, done) in current.items()}
    _save_state(user_data_dir, state)
    return recorded
