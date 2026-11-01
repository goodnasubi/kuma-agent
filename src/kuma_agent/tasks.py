"""用事の台帳（タスクボード）。

1 つの会話の中に複数の用事が並行しても取り違えないための仕組み。
会話の中で話題が切り替わるたび、その用事の状態（題名・状態・最新の
メモ）を台帳に持ち、対話の冒頭で進行中の用事を思い出せるようにする。

台帳は用事の持ち主であるユーザーごとに 1 冊だけを持ち、データ
置き場の tasks.json に保存する。旧仕様で部屋ごとに分かれていた
tasks/<部屋名>.json が残っている場合は、本体の読み込み時に
そのまま tasks.json へ取り込む。壊れた JSON のときは空の台帳
として始める（例外で落とさない）。
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
        """辞書から復元する。形が不正なら None（読み飛ばす）。

        旧データに残っている余分なキー（かつての room など）は
        無視して読み込む。
        """
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
                 tasks_file: Path | None = None, *,
                 migrate_legacy: bool | None = None) -> None:
        self.data_dir = data_dir
        # 通常の台帳はユーザー置き場直下の tasks.json の 1 冊だけ。
        # tasks_file の明示は、保持期限の掃除が移行前の旧ファイルを
        # 個別に処理する場合などに限る。
        self.tasks_file = (tasks_file if tasks_file is not None
                           else data_dir / "tasks.json")
        # 本体の読み込み時だけ、旧い部屋ごと台帳を取り込む。掃除側が
        # dry-run で本体を覗く場合などは、読み込みの副作用を止められる。
        self._migrate_legacy = (
            tasks_file is None if migrate_legacy is None else migrate_legacy)
        self._tasks: list[Task] = self._load()
        if self._migrate_legacy:
            self._import_legacy_boards()

    # ------------------------------------------------------------ 永続化

    @staticmethod
    def _read_tasks_file(path: Path) -> list[Task] | None:
        """台帳ファイルを読み、用事の並びを返す。

        ファイルが無い場合は空の並び、JSON として読めない・配列で
        ない場合は None を返す（移行元として読めたかどうかの区別に使う）。
        """
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # JSONDecodeError と UnicodeDecodeError は ValueError の仲間
            return None
        if not isinstance(raw, list):
            return None
        return [task for record in raw
                if (task := Task.from_dict(record)) is not None]

    def _load(self) -> list[Task]:
        tasks = self._read_tasks_file(self.tasks_file)
        # 壊れた台帳は空として始める
        return tasks if tasks is not None else []

    def _import_legacy_boards(self) -> None:
        """旧仕様の部屋ごと台帳（tasks/*.json）を本体へ取り込む。

        ID が本体の用事と衝突したら使われていない番号へ振り直す。
        読めたファイルだけを取り込み対象とし、本体への保存が済んだ
        ファイルから削除する。壊れたファイルや保存・削除の失敗では
        落とさず、そのファイルは残して次回に回す。
        """
        legacy_dir = self.data_dir / "tasks"
        if not legacy_dir.is_dir():
            return
        try:
            paths = sorted(legacy_dir.glob("*.json"))
        except OSError:
            return
        imported: list[Task] = []
        readable: list[Path] = []
        used = {t.id for t in self._tasks}
        for path in paths:
            tasks = self._read_tasks_file(path)
            if tasks is None:
                continue  # 読めないファイルは残す
            readable.append(path)
            for task in tasks:
                if task.id in used:
                    n = 1
                    while str(n) in used:
                        n += 1
                    task.id = str(n)
                used.add(task.id)
                imported.append(task)
        if not readable:
            return
        if imported:
            original = self._tasks
            self._tasks = original + imported
            try:
                self._save()
            except OSError:
                self._tasks = original
                return  # 本体に残せなかったので旧ファイルも残す
        for path in readable:
            try:
                path.unlink()
            except OSError:
                continue  # 消せなかったファイルは残るが、処理は続ける

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
        """用事の一覧。status を渡すとその状態だけを返す。

        台帳はユーザーごとに共有される 1 冊なので、他のセッションが
        追加した用事も見えるよう、返す前にファイルを読み直す。
        """
        self._tasks = self._load()
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
