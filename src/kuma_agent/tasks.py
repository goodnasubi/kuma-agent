"""用事の台帳（タスクボード）。

1 つの会話の中に複数の用事が並行しても取り違えないための仕組み。
会話の中で話題が切り替わるたび、その用事の状態（題名・状態・最新の
メモ）を台帳に持ち、対話の冒頭で進行中の用事を思い出せるようにする。

台帳は部屋ごとに 1 ファイルで保存する。既定の部屋（main）は
データ置き場の tasks.json、それ以外の部屋は tasks/<部屋名>.json
（置き場の解決は memory.tasks_file_for）。壊れた JSON のときは
空の台帳として始める（例外で落とさない）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def _parse_time(raw: str) -> datetime | None:
    """ISO 形式の時刻文字列を読む。読めなければ None（対象から外す）。"""
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None

STATUS_OPEN = "open"
STATUS_DONE = "done"
_STATUSES = (STATUS_OPEN, STATUS_DONE)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Task:
    """台帳の 1 件の用事。"""
    id: str
    title: str
    status: str = STATUS_OPEN
    memo: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "memo": self.memo,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, record: object) -> Task | None:
        """辞書から復元する。形が不正なら None（読み飛ばす）。"""
        if not isinstance(record, dict):
            return None
        task_id = record.get("id")
        title = record.get("title")
        if not isinstance(task_id, str) or not task_id:
            return None
        if not isinstance(title, str) or not title:
            return None
        status = record.get("status")
        if status not in _STATUSES:
            status = STATUS_OPEN
        return cls(
            id=task_id,
            title=title,
            status=status,
            memo=str(record.get("memo") or ""),
            created_at=str(record.get("created_at") or _now()),
            updated_at=str(record.get("updated_at") or _now()),
        )


class TaskBoard:
    """用事の台帳。台帳ファイルへの保存・読み込みを持つ。"""

    def __init__(self, data_dir: Path,
                 tasks_file: Path | None = None) -> None:
        self.data_dir = data_dir
        # 既定はユーザー置き場直下の tasks.json（既定の部屋の台帳）。
        # 部屋ごとの台帳では呼び出し側がファイルを指定する
        self.tasks_file = (tasks_file if tasks_file is not None
                           else data_dir / "tasks.json")
        self._tasks: list[Task] = self._load()

    # ------------------------------------------------------------ 永続化

    def _load(self) -> list[Task]:
        if not self.tasks_file.exists():
            return []
        try:
            raw = json.loads(self.tasks_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []  # 壊れた台帳は空として始める
        if not isinstance(raw, list):
            return []
        tasks = [task for record in raw
                 if (task := Task.from_dict(record)) is not None]
        return tasks

    def _save(self) -> None:
        self.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps([t.to_dict() for t in self._tasks],
                          ensure_ascii=False, indent=2)
        self.tasks_file.write_text(text + "\n", encoding="utf-8")

    # ------------------------------------------------------------ 操作

    def _next_id(self) -> str:
        used = {t.id for t in self._tasks}
        n = 1
        while str(n) in used:
            n += 1
        return str(n)

    def add(self, title: str, memo: str = "") -> Task:
        """用事を追加して保存する。"""
        self._tasks = self._load()  # 他のセッションの追加を消さないよう読み直す
        task = Task(id=self._next_id(), title=title, memo=memo)
        self._tasks.append(task)
        self._save()
        return task

    def get(self, task_id: str) -> Task | None:
        for task in self._tasks:
            if task.id == task_id:
                return task
        return None

    def update(self, task_id: str, memo: str | None = None,
               status: str | None = None) -> Task | None:
        """メモや状態を更新して保存する。対象がなければ None。"""
        self._tasks = self._load()  # 追加と同じく、書き込み前に読み直す
        task = self.get(task_id)
        if task is None:
            return None
        if memo is not None:
            task.memo = memo
        if status is not None:
            if status not in _STATUSES:
                raise ValueError(f"状態は {' / '.join(_STATUSES)} のいずれかです")
            task.status = status
        task.updated_at = _now()
        self._save()
        return task

    def list(self, status: str | None = None) -> list[Task]:
        """用事の一覧。status を渡すとその状態だけを返す。"""
        if status is None:
            return list(self._tasks)
        return [t for t in self._tasks if t.status == status]

    def open_tasks(self) -> list[Task]:
        """進行中（open）の用事の一覧。"""
        return self.list(STATUS_OPEN)

    def prune_done_before(self, cutoff: datetime, *,
                          dry_run: bool = False) -> int:
        """完了済みで更新時刻が cutoff より古い用事を取り除く。

        保持期限のクリーンアップ用。進行中の用事と、更新時刻が
        読めない用事は残す。取り除いた（dry_run では取り除くはずの）
        件数を返す。dry_run でなければ台帳を保存し直す。
        """
        self._tasks = self._load()  # 他のセッションの変更を消さないよう読み直す
        kept: list[Task] = []
        removed = 0
        for task in self._tasks:
            updated = _parse_time(task.updated_at)
            if (task.status == STATUS_DONE and updated is not None
                    and updated < cutoff):
                removed += 1
            else:
                kept.append(task)
        if removed and not dry_run:
            self._tasks = kept
            self._save()
        return removed
