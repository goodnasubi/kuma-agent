"""道具（ツール）の登録と実行。

LLM が呼べる道具を dict のスキーマと Python 関数の組で管理する。
実行範囲は作業ディレクトリ内に制限する。
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .tasks import format_remind_at

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
    "list_tasks": "用事を確認中",
    "create_task": "用事を記録中",
    "update_task": "用事を記録中",
    "remember": "記憶に書き留め中",
    "write_memory": "記憶に書き留め中",
    "save_memory": "記憶に書き留め中",
}


def activity_label(name: str, args: dict | None = None) -> str:
    """道具の呼び出しを「今なにをしているか」の表示文言にする。

    対応表にある道具は専用の文言を返し、ファイル名や用事の題名など
    安全な手がかりが引数にあれば括弧で添える。対応表にない道具は
    名前を含む汎用表示にし、MCP 道具（<サーバー名>__<道具名>）は
    サーバー名を添えた汎用表示にする。
    """
    label = _ACTIVITY_LABELS.get(name)
    if label is None:
        if "__" in name:
            # MCP 道具は <サーバー名>__<道具名> の形で登録される。
            # 汎用の表示としてサーバー名を添える
            return f"道具を使っています（{name.split('__', 1)[0]}）"
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


# 実行前に利用者の承認が要る道具（組み込み分）。MCP 道具は
# 名前に区切り（__）を含むことで対象に含める: 外部サーバーが
# 実体を持つため、組み込み以上に素性が読みにくく、一律で対象
# にする。ファイル読み書き・用事・記憶などの通常の道具は対象外
APPROVAL_TARGET_NAMES = frozenset({"run_shell"})

# 承認が下りなかったときに道具の結果として返す文言。例外ではなく
# 結果文字列にすることで、モデルが受けて会話を続けられる
APPROVAL_DENIED_RESULT = "error: 承認されませんでした"


def requires_approval(name: str) -> bool:
    """その道具の実行前に利用者の承認が要るかどうか。

    run_shell（任意のコマンド実行）と、MCP 道具（名前に __ を含む
    <サーバー名>__<道具名>）だけが対象。それ以外は承認なしで実行
    する。
    """
    return name in APPROVAL_TARGET_NAMES or "__" in name


def summarize_tool_args(name: str, args: dict, limit: int = 200) -> str:
    """承認の判断に要る引数の要約を 1 行で返す。

    run_shell はコマンド文字列そのものが判断材料になるため、その
    まま出す。それ以外は引数全体を JSON にして出す。改行などは
    空白に潰し、長すぎる場合は切り詰める。
    """
    if name == "run_shell":
        text = str(args.get("command", ""))
    elif args:
        try:
            text = json.dumps(args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(args)
    else:
        text = ""
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


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
        # 承認フック: 承認対象の道具を実行する前に呼ばれる
        # （道具名, 引数）→ 真偽 の callable。None の間は承認を
        # 挟まない（従来動作）。Session がテナント設定に応じて
        # 差し込み、承認の要否の判断自体はフック側に寄せる
        # （フックが無い・例外・False のいずれも実行しない側に倒す
        # 配線は Session の仕事）
        self.approval_check: Callable[[str, dict], bool] | None = None
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
            "会話の中で話題が切り替わったら、その用事を用事の一覧に登録・更新して、"
            "用事ごとの状態を分けて保つこと。"
        )
        self.register(Tool(
            "list_tasks",
            f"記録した用事を、状態付きですべて返す。{guidance}",
            {"type": "object", "properties": {}},
            self._list_tasks,
        ))
        remind_guidance = (
            "利用者が「○時に知らせて」のように時刻を指定したら、remind_at に"
            "その絶対時刻を ISO 8601 形式（例: 2026-11-03T09:00:00）で入れること。"
            "現在日時はシステムプロンプトに示される。時刻の指定が無ければ入れない。"
        )
        self.register(Tool(
            "create_task",
            f"用事を用事の一覧に登録する。{guidance}{remind_guidance}",
            {"type": "object", "properties": {
                "title": {"type": "string"},
                "memo": {"type": "string"},
                "remind_at": {"type": "string",
                               "description": "通知時刻（ISO 8601、例: 2026-11-03T09:00:00）。知らせる時刻の指定があるときだけ入れる"}},
             "required": ["title"]},
            self._create_task,
        ))
        self.register(Tool(
            "update_task",
            f"記録した用事を更新する（メモの付け替え、終わったら status を done にする、通知時刻の付け替えは remind_at、取り消しは remind_at に空文字列）。{guidance}{remind_guidance}",
            {"type": "object", "properties": {
                "id": {"type": "string"},
                "memo": {"type": "string"},
                "status": {"type": "string", "enum": ["open", "done"]},
                "remind_at": {"type": "string",
                               "description": "通知時刻（ISO 8601）。省略すると変更しない。空文字列で取り消し"}},
             "required": ["id"]},
            self._update_task,
        ))

    def _format_tasks(self, tasks: list[Task]) -> str:
        if not tasks:
            return "（用事はありません）"
        lines = []
        for t in tasks:
            line = f"#{t.id} {t.title}"
            if t.memo:
                line += f": {t.memo}"
            line += f"（状態: {t.status}）"
            if t.remind_at:
                line += f"（{format_remind_at(t.remind_at)} に通知）"
            lines.append(line)
        return "\n".join(lines)

    def _list_tasks(self) -> str:
        assert self.task_board is not None
        return self._format_tasks(self.task_board.list())

    def _create_task(self, title: str, memo: str = "",
                     remind_at: str = "") -> str:
        assert self.task_board is not None
        task = self.task_board.add(title, memo, remind_at=remind_at)
        return f"登録しました: #{task.id} {task.title}"

    def _update_task(self, id: str, memo: str | None = None,
                     status: str | None = None,
                     remind_at: str | None = None) -> str:
        assert self.task_board is not None
        task = self.task_board.update(id, memo=memo, status=status,
                                      remind_at=remind_at)
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
        if self.approval_check is not None and requires_approval(name):
            # 承認対象の道具は、フックが True を返すまで実行しない。
            # フックの例外も却下として扱う（安全側に倒す）
            try:
                approved = bool(self.approval_check(name, dict(args)))
            except Exception:
                approved = False
            if not approved:
                return APPROVAL_DENIED_RESULT
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
