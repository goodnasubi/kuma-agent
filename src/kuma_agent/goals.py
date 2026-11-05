"""目標（長期の目標と進捗）の保存・読み出しと、用事との紐づけ。

目標タブに並べる長期の目標の置き場。目標はユーザーの置き場の
goals.json に配列で保存する（アイデアの ideas.json・用事の
tasks.json と同じ流儀）。読み出しは壊れたデータに強くする（壊れた
ファイルは空として始め、形の不正な要素は読み飛ばす）。

目標の項目は、タイトル・ひとこと説明・作成時刻・状態・進捗メモの
列。状態は次の 2 つ:
- active: 進行中
- done: 達成

進捗メモは {時刻, 本文} の列で、目標ごとに時系列で残す。

目標の進み具合は、紐づいた用事の完了割合で表す。用事（tasks.py）
は任意で goal_id を持ち、紐づけ・解除は目標タブの画面からだけ
行う（会話から用事に目標を付ける操作はしない）。目標を消すときは
用事自体は消さず、紐づけだけを外す。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .tasks import STATUS_DONE, Task, TaskBoard

GOALS_FILE = "goals.json"

STATUS_ACTIVE = "active"
STATUS_DONE = "done"
_STATUSES = (STATUS_ACTIVE, STATUS_DONE)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class GoalNote:
    """進捗メモ 1 件。"""
    ts: str
    body: str

    def to_dict(self) -> dict:
        return {"ts": self.ts, "body": self.body}

    @classmethod
    def from_dict(cls, record: object) -> GoalNote | None:
        """辞書から復元する。形が不正なら None（読み飛ばす）。"""
        if not isinstance(record, dict):
            return None
        body = record.get("body")
        if not isinstance(body, str) or not body:
            return None
        return cls(ts=str(record.get("ts") or ""), body=body)


@dataclass
class Goal:
    """長期の目標 1 件。"""
    id: str
    title: str
    description: str = ""
    status: str = STATUS_ACTIVE
    created_at: str = field(default_factory=_now)
    notes: list[GoalNote] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "created_at": self.created_at,
            "notes": [n.to_dict() for n in self.notes],
        }

    @classmethod
    def from_dict(cls, record: object) -> Goal | None:
        """辞書から復元する。形が不正なら None（読み飛ばす）。"""
        if not isinstance(record, dict):
            return None
        goal_id = record.get("id")
        title = record.get("title")
        if not isinstance(goal_id, str) or not goal_id:
            return None
        if not isinstance(title, str) or not title:
            return None
        status = record.get("status")
        if status not in _STATUSES:
            status = STATUS_ACTIVE
        raw_notes = record.get("notes")
        notes: list[GoalNote] = []
        if isinstance(raw_notes, list):
            notes = [note for item in raw_notes
                     if (note := GoalNote.from_dict(item)) is not None]
        return cls(
            id=goal_id,
            title=title,
            description=str(record.get("description") or ""),
            status=status,
            created_at=str(record.get("created_at") or _now()),
            notes=notes,
        )


@dataclass
class GoalProgress:
    """目標の進み具合。紐づいた用事の完了割合で表す。

    total は紐づいた用事の総数、done はそのうち完了した数。
    ratio は done / total で、用事が 1 件も無いときは None
    （割合なし）。
    """
    total: int
    done: int
    ratio: float | None

    def to_dict(self) -> dict:
        return {"total": self.total, "done": self.done,
                "ratio": self.ratio}


def progress_of(tasks: list[Task]) -> GoalProgress:
    """用事の並びから進み具合を算出する。"""
    total = len(tasks)
    done = sum(1 for t in tasks if t.status == STATUS_DONE)
    return GoalProgress(total=total, done=done,
                        ratio=(done / total) if total else None)


class GoalBoard:
    """目標の一覧。goals.json への保存・読み込みを持つ。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.goals_file = data_dir / GOALS_FILE
        self._goals: list[Goal] = self._load()

    # ------------------------------------------------------------ 永続化

    @staticmethod
    def _read_goals_file(path: Path) -> list[Goal] | None:
        """目標ファイルを読み、目標の並びを返す。

        ファイルが無い場合は空の並び、JSON として読めない・配列で
        ない場合は None を返す（壊れたファイルの扱いに使う）。
        """
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, list):
            return None
        return [goal for record in raw
                if (goal := Goal.from_dict(record)) is not None]

    def _load(self) -> list[Goal]:
        goals = self._read_goals_file(self.goals_file)
        # 壊れたファイルは空として始める
        return goals if goals is not None else []

    def _save(self) -> None:
        self.goals_file.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps([g.to_dict() for g in self._goals],
                          ensure_ascii=False, indent=2)
        self.goals_file.write_text(text + "\n", encoding="utf-8")

    # ------------------------------------------------------------ 操作

    def _next_id(self) -> str:
        used = {g.id for g in self._goals}
        n = 1
        while str(n) in used:
            n += 1
        return str(n)

    def add(self, title: str, description: str = "",
            now: datetime | None = None) -> Goal:
        """目標を 1 件追加して保存する。

        書き込み前にファイルを読み直し、他の経路で追加された目標を
        消さないようにする（アイデア・用事と同じ扱い）。
        """
        title = title.strip()
        if not title:
            raise ValueError("タイトルを指定してください")
        self._goals = self._load()
        created_at = (now or datetime.now()).isoformat(timespec="seconds")
        goal = Goal(id=self._next_id(), title=title,
                    description=description.strip(),
                    created_at=created_at)
        self._goals.append(goal)
        self._save()
        return goal

    def get(self, goal_id: str) -> Goal | None:
        for goal in self._goals:
            if goal.id == goal_id:
                return goal
        return None

    def list(self) -> list[Goal]:
        """目標の一覧を新しい順に返す。

        目標はユーザーごとに共有される 1 つなので、他の経路で
        追加された目標も見えるよう、返す前にファイルを読み直す。
        """
        self._goals = self._load()
        goals = list(self._goals)
        goals.reverse()  # 追記順（古い順）→ 新しい順
        return goals

    def update(self, goal_id: str, title: str | None = None,
               description: str | None = None,
               status: str | None = None) -> Goal | None:
        """タイトル・説明・状態を更新して保存する。対象がなければ None。

        None の項目は変更しない。タイトルは空にできない。
        """
        if status is not None and status not in _STATUSES:
            raise ValueError(
                f"状態は {' / '.join(_STATUSES)} のいずれかです")
        if title is not None and not title.strip():
            raise ValueError("タイトルを指定してください")
        self._goals = self._load()
        goal = self.get(goal_id)
        if goal is None:
            return None
        if title is not None:
            goal.title = title.strip()
        if description is not None:
            goal.description = description.strip()
        if status is not None:
            goal.status = status
        self._save()
        return goal

    def delete(self, goal_id: str) -> bool:
        """目標を消す。消せたら True、対象が無ければ False。

        紐づいた用事の扱いは呼び出し側（delete_goal）が担う。
        """
        self._goals = self._load()
        goal = self.get(goal_id)
        if goal is None:
            return False
        self._goals = [g for g in self._goals if g.id != goal_id]
        self._save()
        return True

    def add_note(self, goal_id: str, body: str,
                 now: datetime | None = None) -> Goal | None:
        """進捗メモを 1 件追加して保存する。対象がなければ None。"""
        body = body.strip()
        if not body:
            raise ValueError("メモの本文を指定してください")
        self._goals = self._load()
        goal = self.get(goal_id)
        if goal is None:
            return None
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        goal.notes.append(GoalNote(ts=ts, body=body))
        self._save()
        return goal


# ------------------------------------------------------------ 用事との紐づけ

def goal_view(goal: Goal, tasks: list[Task]) -> dict:
    """目標 1 件の API 向け表現（進み具合と紐づいた用事つき）。

    tasks はその利用者の用事すべて。ここから goal_id が一致する
    ものを紐づいた用事として並べ、進み具合を算出する。進捗メモは
    新しい順に返す。
    """
    linked = [t for t in tasks if t.goal_id == goal.id]
    view = goal.to_dict()
    view["notes"] = [n.to_dict() for n in reversed(goal.notes)]
    view["progress"] = progress_of(linked).to_dict()
    view["tasks"] = [
        {"id": t.id, "title": t.title, "status": t.status}
        for t in linked]
    return view


def link_task(data_dir: Path, goal_id: str,
              task_id: str) -> tuple[Goal, Task] | None:
    """用事を目標へ紐づける。目標か用事が無ければ None。

    すでに別の目標に紐づいている用事は、その目標への紐づけを
    付け替える（用事が持つ goal_id は 1 つだけ）。
    """
    goal = GoalBoard(data_dir).get(goal_id)
    if goal is None:
        return None
    task = TaskBoard(data_dir).update(task_id, goal_id=goal_id)
    if task is None:
        return None
    return goal, task


def unlink_task(data_dir: Path, task_id: str) -> Task | None:
    """用事の目標への紐づけを外す。対象の用事が無ければ None。"""
    return TaskBoard(data_dir).update(task_id, goal_id="")


def delete_goal(data_dir: Path, goal_id: str) -> bool:
    """目標を消す。用事自体は消さず、紐づけだけを外す。

    対象の目標が無ければ False。
    """
    board = TaskBoard(data_dir)
    for task in board.list_for_goal(goal_id):
        board.update(task.id, goal_id="")
    return GoalBoard(data_dir).delete(goal_id)
