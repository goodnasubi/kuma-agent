"""道具（ツール）の登録と実行。

LLM が呼べる道具を dict のスキーマと Python 関数の組で管理する。
実行範囲は作業ディレクトリ内に制限する。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from .tasks import Task, TaskBoard


# 道具名 → 「今なにをしているか」の表示名（UI の作業中表示用）。
# 状態表示は頭文字ではなく実際の作業に応じた文言にするため、道具ごとに
# 利用者向けの言い回しを用意する。
_ACTIVITY_LABELS: dict[str, str] = {
    "read_file": "ファイルを読んでいます",
    "write_file": "ファイルを書いています",
    "edit_file": "ファイルを編集中",
    "edit": "ファイルを編集中",
    "run_shell": "コマンドを実行中",
    "now": "時刻を確認中",
    "list_tasks": "台帳を確認中",
    "create_task": "台帳を更新中",
    "update_task": "台帳を更新中",
    "remember": "記憶に書き留め中",
    "write_memory": "記憶に書き留め中",
    "save_memory": "記憶に書き留め中",
}


def activity_label(name: str, args: dict | None = None) -> str:
    """道具の呼び出しを「今なにをしているか」の表示文言にする。

    対応表にある道具は専用の文言を返し、ファイル名や台帳の題名など
    安全な手がかりが引数にあれば括弧で添える。対応表にない道具は
    名前を含む汎用表示にする。
    """
    label = _ACTIVITY_LABELS.get(name)
    if label is None:
        return f"道具を実行中（{name}）"
    hint: object = None
    if args:
        if name in ("read_file", "write_file", "edit_file", "edit"):
            hint = args.get("path")
        elif name == "create_task":
            hint = args.get("title")
    if isinstance(hint, str) and hint.strip():
        return f"{label}（{hint.strip()}）"
    return label


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., str]


class ToolBox:
    def __init__(self, work_dir: Path, task_board: TaskBoard | None = None) -> None:
        self.work_dir = work_dir.resolve()
        self.task_board = task_board
        self._tools: dict[str, Tool] = {}
        self._register_defaults()
        if task_board is not None:
            self._register_task_tools()

    def _register_defaults(self) -> None:
        self.register(Tool(
            "now", "現在の日時を返す", {"type": "object", "properties": {}},
            lambda: datetime.now().isoformat(timespec="seconds"),
        ))
        self.register(Tool(
            "read_file", "作業ディレクトリ内のファイルを読む",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            self._read_file,
        ))
        self.register(Tool(
            "write_file", "作業ディレクトリ内のファイルに書く",
            {"type": "object", "properties": {
                "path": {"type": "string"}, "content": {"type": "string"}},
             "required": ["path", "content"]},
            self._write_file,
        ))
        self.register(Tool(
            "run_shell", "シェルコマンドを実行する（作業ディレクトリ内）",
            {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
            self._run_shell,
        ))

    def _register_task_tools(self) -> None:
        guidance = (
            "会話の中で話題が切り替わったら、その用事を台帳に登録・更新して、"
            "用事ごとの状態を分けて保つこと。"
        )
        self.register(Tool(
            "list_tasks", f"進行中の用事の一覧を返す。{guidance}",
            {"type": "object", "properties": {}},
            self._list_tasks,
        ))
        self.register(Tool(
            "create_task", f"用事を台帳に登録する。{guidance}",
            {"type": "object", "properties": {
                "title": {"type": "string"},
                "memo": {"type": "string"}},
             "required": ["title"]},
            self._create_task,
        ))
        self.register(Tool(
            "update_task",
            f"台帳の用事を更新する（メモの付け替え、終わったら status を done にする）。{guidance}",
            {"type": "object", "properties": {
                "id": {"type": "string"},
                "memo": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "done"]}},
             "required": ["id"]},
            self._update_task,
        ))

    def _format_tasks(self, tasks: list[Task]) -> str:
        if not tasks:
            return "（進行中の用事はありません）"
        lines = []
        for t in tasks:
            line = f"#{t.id} {t.title}"
            if t.memo:
                line += f": {t.memo}"
            lines.append(line)
        return "\n".join(lines)

    def _list_tasks(self) -> str:
        assert self.task_board is not None
        return self._format_tasks(self.task_board.open_tasks())

    def _create_task(self, title: str, memo: str = "") -> str:
        assert self.task_board is not None
        task = self.task_board.add(title, memo)
        return f"登録しました: #{task.id} {task.title}"

    def _update_task(self, id: str, memo: str | None = None,
                     status: str | None = None) -> str:
        assert self.task_board is not None
        task = self.task_board.update(id, memo=memo, status=status)
        if task is None:
            return f"error: 用事 #{id} が見つかりません"
        return f"更新しました: #{task.id} {task.title}（{task.status}）"

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict]:
        return [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in self._tools.values()
        ]

    def call(self, name: str, args: dict) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"error: unknown tool {name}"
        try:
            return str(tool.func(**args))
        except Exception as e:  # 道具の失敗は LLM に返して対処させる
            return f"error: {e}"

    def _resolve(self, path: str) -> Path:
        p = (self.work_dir / path).resolve()
        if not str(p).startswith(str(self.work_dir)):
            raise ValueError("作業ディレクトリの外は触れません")
        return p

    def _read_file(self, path: str) -> str:
        return self._resolve(path).read_text(encoding="utf-8")

    def _write_file(self, path: str, content: str) -> str:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"wrote {path} ({len(content)} chars)"

    def _run_shell(self, command: str) -> str:
        proc = subprocess.run(command, shell=True, cwd=self.work_dir,
                              capture_output=True, text=True, timeout=120)
        out = (proc.stdout + proc.stderr).strip()
        return out[-4000:] if out else f"(exit {proc.returncode})"
