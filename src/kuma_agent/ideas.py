"""アイデア（クマからの提案カード）の保存・読み出しと生成。

アイデアタブに並べる提案カードの置き場。カードはユーザーの置き場の
ideas.json に配列で保存する（用事の tasks.json と同じ流儀）。
フィード（feed.jsonl）のような追記専用の形式では、「やってみる／
やめておく」に伴う状態の変更を持てないため、状態を持つカードは
配列の書き直しで持つ。読み出しは壊れたデータに強くする（壊れた
ファイルは空として始め、形の不正な要素は読み飛ばす）。

カードの状態は次の 3 つ:
- new: 未対応（これから判断する提案）
- accepted: 「やってみる」で用事の一覧へ登録済み
- dismissed: 「やめておく」で一覧から外した（記録は残る）

提案の生成は、利用者がアイデアタブの「提案をもらう」ボタンを
押したときだけ行う（オンデマンド）。能動発信の巡回や会話の中では
生まない。生成は朝のまとめと同じく委任の仕組み
（delegate.run_delegated_task）で子にモデルを回させ、結果は
JSON の構造化出力（題名・本文・きっかけ）として受け取る。材料は
その利用者の進行中の用事・最近のフィード・プロフィール
（USER.md / MEMORY.md）の、既存の仕組みから取れる範囲に限る。
子の応答が「提案なし」や壊れた出力のときはカードを作らず、
生成自体が失敗しても既存のカードは壊れない。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .delegate import run_delegated_task
from .feed import load_feed_entries
from .profile_files import read_profile_file
from .tasks import Task, TaskBoard

if TYPE_CHECKING:
    from .session import Session

IDEAS_FILE = "ideas.json"

STATUS_NEW = "new"
STATUS_ACCEPTED = "accepted"
STATUS_DISMISSED = "dismissed"
_STATUSES = (STATUS_NEW, STATUS_ACCEPTED, STATUS_DISMISSED)
# 一覧に出す状態（やめておいたカードは既定で出さない）
VISIBLE_STATUSES = (STATUS_NEW, STATUS_ACCEPTED)

# 1 回の生成で作るカードの上限
MAX_IDEAS_PER_GENERATION = 3
# 生成の材料に含めるフィードの件数と、プロフィールの抜粋の長さ
_CONTEXT_FEED_LIMIT = 10
_CONTEXT_PROFILE_CHARS = 1200

# 生成できなかったときに利用者へ伝える文面
GENERATION_FAILED_MESSAGE = (
    "うまく提案を考えられませんでした。時間をおいてもう一度お試しください。")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Idea:
    """アイデアカード 1 件。"""
    id: str
    title: str
    body: str
    reason: str = ""
    status: str = STATUS_NEW
    created_at: str = field(default_factory=_now)
    # 「やってみる」で登録した用事の ID（二重登録を防ぐために持つ）
    task_id: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "reason": self.reason,
            "status": self.status,
            "created_at": self.created_at,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, record: object) -> Idea | None:
        """辞書から復元する。形が不正なら None（読み飛ばす）。"""
        if not isinstance(record, dict):
            return None
        idea_id = record.get("id")
        title = record.get("title")
        body = record.get("body")
        if not isinstance(idea_id, str) or not idea_id:
            return None
        if not isinstance(title, str) or not title:
            return None
        if not isinstance(body, str) or not body:
            return None
        status = record.get("status")
        if status not in _STATUSES:
            status = STATUS_NEW
        return cls(
            id=idea_id,
            title=title,
            body=body,
            reason=str(record.get("reason") or ""),
            status=status,
            created_at=str(record.get("created_at") or _now()),
            task_id=str(record.get("task_id") or ""),
        )


class IdeaBoard:
    """アイデアカードの一覧。ideas.json への保存・読み込みを持つ。"""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.ideas_file = data_dir / IDEAS_FILE
        self._ideas: list[Idea] = self._load()

    # ------------------------------------------------------------ 永続化

    @staticmethod
    def _read_ideas_file(path: Path) -> list[Idea] | None:
        """カードファイルを読み、カードの並びを返す。

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
        return [idea for record in raw
                if (idea := Idea.from_dict(record)) is not None]

    def _load(self) -> list[Idea]:
        ideas = self._read_ideas_file(self.ideas_file)
        # 壊れたファイルは空として始める
        return ideas if ideas is not None else []

    def _save(self) -> None:
        self.ideas_file.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps([i.to_dict() for i in self._ideas],
                          ensure_ascii=False, indent=2)
        self.ideas_file.write_text(text + "\n", encoding="utf-8")

    # ------------------------------------------------------------ 操作

    def _next_id(self) -> str:
        used = {i.id for i in self._ideas}
        n = 1
        while str(n) in used:
            n += 1
        return str(n)

    def add(self, title: str, body: str, reason: str = "",
            now: datetime | None = None) -> Idea:
        """カードを 1 件追加して保存する。"""
        return self.add_many([(title, body, reason)], now=now)[0]

    def add_many(self, proposals: list[tuple[str, str, str]],
                 now: datetime | None = None) -> list[Idea]:
        """提案（題名・本文・きっかけ）をまとめてカードにして保存する。

        書き込み前にファイルを読み直し、他の経路で追加されたカードを
        消さないようにする（用事の一覧と同じ扱い）。
        """
        self._ideas = self._load()
        created_at = (now or datetime.now()).isoformat(timespec="seconds")
        added: list[Idea] = []
        for title, body, reason in proposals:
            idea = Idea(id=self._next_id(), title=title, body=body,
                        reason=reason, created_at=created_at)
            self._ideas.append(idea)
            added.append(idea)
        if added:
            self._save()
        return added

    def get(self, idea_id: str) -> Idea | None:
        for idea in self._ideas:
            if idea.id == idea_id:
                return idea
        return None

    def list(self, statuses: tuple[str, ...] | None = None) -> list[Idea]:
        """カードの一覧を新しい順に返す。statuses を渡すと絞る。

        カードはユーザーごとに共有される 1 つなので、他の経路で
        追加されたカードも見えるよう、返す前にファイルを読み直す。
        """
        self._ideas = self._load()
        ideas = list(self._ideas)
        if statuses is not None:
            ideas = [i for i in ideas if i.status in statuses]
        ideas.reverse()  # 追記順（古い順）→ 新しい順
        return ideas

    def set_status(self, idea_id: str, status: str) -> Idea | None:
        """カードの状態を変更して保存する。対象がなければ None。"""
        if status not in _STATUSES:
            raise ValueError(f"状態は {' / '.join(_STATUSES)} のいずれかです")
        self._ideas = self._load()
        idea = self.get(idea_id)
        if idea is None:
            return None
        idea.status = status
        self._save()
        return idea

    def mark_accepted(self, idea_id: str, task_id: str) -> Idea | None:
        """「やってみる」で登録した用事の ID とともに accepted にする。"""
        self._ideas = self._load()
        idea = self.get(idea_id)
        if idea is None:
            return None
        idea.status = STATUS_ACCEPTED
        idea.task_id = task_id
        self._save()
        return idea


def accept_idea(data_dir: Path, idea_id: str) -> tuple[Idea, Task | None] | None:
    """カードを「やってみる」にする。用事の一覧へ登録し accepted にする。

    すでに accepted のカードは二重に登録せず、登録済みの用事を
    そのまま返す（用事がもう無ければ用事は None）。対象のカードが
    無ければ None。やめておいた（dismissed）カードは対象にしない。
    """
    board = IdeaBoard(data_dir)
    idea = board.get(idea_id)
    if idea is None or idea.status == STATUS_DISMISSED:
        return None
    tasks = TaskBoard(data_dir)
    if idea.status == STATUS_ACCEPTED:
        task = tasks.get(idea.task_id) if idea.task_id else None
        return idea, task
    memo = idea.body
    if idea.reason:
        memo += f"\nきっかけ: {idea.reason}"
    task = tasks.add(idea.title, memo=memo)
    accepted = board.mark_accepted(idea_id, task.id)
    return (accepted if accepted is not None else idea), task


def dismiss_idea(data_dir: Path, idea_id: str) -> Idea | None:
    """カードを「やめておく」にする（記録は残し、一覧から外す）。

    すでに用事にした（accepted）カードは対象にしない。対象が
    無ければ None。
    """
    board = IdeaBoard(data_dir)
    idea = board.get(idea_id)
    if idea is None or idea.status == STATUS_ACCEPTED:
        return None
    return board.set_status(idea_id, STATUS_DISMISSED)


# ------------------------------------------------------------ 生成

def build_idea_prompt(open_tasks: list[Task], feed_bodies: list[str],
                      profile_text: str) -> str:
    """提案を子へ頼む指示文を組み立てる。

    材料（進行中の用事・最近のフィード・プロフィールの抜粋）を含め、
    結果は JSON の構造化出力だけを返すよう指定する。材料が無い
    項目はその旨を明記する。
    """
    lines = [
        "利用者への提案（アイデア）を考えてください。",
        "",
        "材料:",
    ]
    if open_tasks:
        lines.append("[進行中の用事]")
        for task in open_tasks:
            memo = f"（{task.memo}）" if task.memo else ""
            lines.append(f"・{task.title}{memo}")
    else:
        lines.append("進行中の用事はありません。")
    if feed_bodies:
        lines.append("[最近の動き]")
        for body in feed_bodies:
            lines.append(f"・{body}")
    if profile_text:
        lines.append("[利用者について]")
        lines.append(profile_text)
    lines += [
        "",
        "やること:",
        f"- 材料から、利用者の役に立ちそうな具体的な提案を最大 "
        f"{MAX_IDEAS_PER_GENERATION} 件まで考えてください。",
        "- すでに進行中の用事そのものの繰り返しは避けてください。",
        "- 提案が無ければ、ideas を空にしてください。",
        "- 返答は次の JSON だけにしてください（前後の文章や"
        "コードフェンスは付けないでください）:",
        '{"ideas": [{"title": "提案の題名", '
        '"body": "提案の内容（何をすると良いか）", '
        '"reason": "なぜ今これか（短く）"}]}',
    ]
    return "\n".join(lines)


def parse_idea_proposals(result: str) -> list[tuple[str, str, str]] | None:
    """子の応答から提案（題名・本文・きっかけ）を取り出す。

    JSON として読めなければ None（壊れた出力）。前後の文章や
    コードフェンスが混ざっても、最初の { から最後の } までを
    JSON として読む。形の合わない要素は読み飛ばし、題名と本文が
    空でないものだけを最大 MAX_IDEAS_PER_GENERATION 件まで返す。
    """
    start = result.find("{")
    end = result.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(result[start:end + 1])
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    items = raw.get("ideas")
    if not isinstance(items, list):
        return None
    proposals: list[tuple[str, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        body = item.get("body")
        if not isinstance(title, str) or not title.strip():
            continue
        if not isinstance(body, str) or not body.strip():
            continue
        reason = item.get("reason")
        proposals.append((title.strip(), body.strip(),
                          reason.strip() if isinstance(reason, str) else ""))
        if len(proposals) >= MAX_IDEAS_PER_GENERATION:
            break
    return proposals


@dataclass
class IdeaGeneration:
    """生成 1 回の結果。outcome は created / none / failed。"""
    ideas: list[Idea]
    outcome: str
    message: str


def _profile_excerpt(user_data_dir: Path) -> str:
    """プロフィール（USER.md / MEMORY.md）の抜粋を材料用に取る。

    読めない・無いファイルは飛ばす。各ファイルは先頭から
    _CONTEXT_PROFILE_CHARS 文字までを使う。
    """
    parts: list[str] = []
    for name in ("USER.md", "MEMORY.md"):
        try:
            content = read_profile_file(user_data_dir, name)
        except (OSError, ValueError):
            continue
        content = content.strip()
        if content:
            parts.append(content[:_CONTEXT_PROFILE_CHARS])
    return "\n".join(parts)


def generate_ideas(session: Session, user_data_dir: Path,
                   executor: Callable[[Session, str], str] | None = None,
                   now: datetime | None = None) -> IdeaGeneration:
    """利用者の文脈から提案を生み、カードとして保存する。

    executor は親 Session と指示文を受けて子の最終応答を返す関数で、
    既定は委任の仕組み（delegate.run_delegated_task）。朝のまとめと
    同じ差し替え口（テスト用）。
    結果の outcome は:
    - created: 提案をカードにした（ideas に今回のカード）
    - none: 子が「提案なし」と判断した（カードは作らない）
    - failed: 子の失敗・壊れた出力（カードは作らず、既存の
      カードにも触れない）
    """
    open_tasks = TaskBoard(user_data_dir).open_tasks()
    feed_bodies = [entry.body for entry in
                   load_feed_entries(user_data_dir,
                                     limit=_CONTEXT_FEED_LIMIT)]
    prompt = build_idea_prompt(open_tasks, feed_bodies,
                               _profile_excerpt(user_data_dir))
    run = executor if executor is not None else run_delegated_task
    try:
        result = run(session, prompt)
    except Exception as e:  # 子の失敗で既存のカードを壊さない
        result = f"error: {e}"
    if not isinstance(result, str):
        result = str(result)
    result = result.strip()
    if not result or result.startswith("error:"):
        return IdeaGeneration([], "failed", GENERATION_FAILED_MESSAGE)
    proposals = parse_idea_proposals(result)
    if proposals is None:
        return IdeaGeneration([], "failed", GENERATION_FAILED_MESSAGE)
    if not proposals:
        return IdeaGeneration([], "none",
                              "今は新しい提案がありません。")
    created = IdeaBoard(user_data_dir).add_many(proposals, now=now)
    return IdeaGeneration(
        created, "created",
        f"提案を {len(created)} 件出しました。")
