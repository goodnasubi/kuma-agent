"""作業の手順（エージェント自身の作業メモ）。

用事の一覧（利用者の頼み事）とは別に、エージェントが自分の作業の
進め方を書き留めるための仕組み。手順が複数ある作業の前に手順を
書き出し、進むたびに状態を更新することで、取りこぼしを防ぎ、
会話が切り替わっても続きから再開できるようにする。

保存先は会話（セッション）ごと: ユーザーのデータ置き場の
work-steps/<セッション ID>.json。相棒モードではセッションが
実質 1 つ（main）に固定されるため、利用者ごとの 1 冊になる。
スレッドモードではスレッドごとに分かれる。壊れた JSON のときは
空の手順として始める（例外で落とさない）。

各項目は updated_at（最後に状態が変わった日時、ISO 形式）も
持つ（AF-3: 朝のまとめで「何日止まっているか」を伝えるため）。
旧データに項目が無ければ空文字列として読み、次に保存し直した
ときに日時が付く。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .memory import DEFAULT_SESSION_ID, normalize_session_id

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_BLOCKED = "blocked"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
_STATUSES = (STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_BLOCKED,
             STATUS_COMPLETED, STATUS_CANCELLED)

# 状態の表示名（モデル向けの整形と画面表示で共有する）
STATUS_LABELS: dict[str, str] = {
    STATUS_PENDING: "未着手",
    STATUS_IN_PROGRESS: "進行中",
    STATUS_BLOCKED: "停滞",
    STATUS_COMPLETED: "完了",
    STATUS_CANCELLED: "中止",
}

# 終わっていない手順があるかどうか（プロンプトへの注入の判断に使う）
_OPEN_STATUSES = (STATUS_PENDING, STATUS_IN_PROGRESS, STATUS_BLOCKED)

MAX_STEPS = 25

_STEPS_DIR = "work-steps"


@dataclass
class Step:
    """作業の手順の 1 件。

    updated_at は最後に状態が変わった日時（ISO 形式）。旧データに
    無ければ空文字列で、その項目については「何日止まっているか」
    は分からないものとして扱う。
    """
    text: str
    status: str = STATUS_PENDING
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {"text": self.text, "status": self.status,
                "updated_at": self.updated_at}

    @classmethod
    def from_dict(cls, record: object) -> Step | None:
        """辞書から復元する。形が不正なら None（読み飛ばす）。"""
        if not isinstance(record, dict):
            return None
        text = record.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        status = record.get("status")
        if status not in _STATUSES:
            status = STATUS_PENDING
        updated_at = record.get("updated_at")
        if not isinstance(updated_at, str):
            updated_at = ""
        return cls(text=text, status=status, updated_at=updated_at)


def format_steps(steps: list[Step]) -> str:
    """手順の一覧を、モデルにも利用者にも読める形の文字列にする。"""
    if not steps:
        return "（作業の手順はありません）"
    lines = []
    for i, step in enumerate(steps, 1):
        label = STATUS_LABELS.get(step.status, step.status)
        lines.append(f"{i}. [{label}] {step.text}")
    return "\n".join(lines)


class WorkSteps:
    """作業の手順。会話ごとのファイルへの保存・読み込みを持つ。"""

    def __init__(self, data_dir: Path,
                 scope: str = DEFAULT_SESSION_ID) -> None:
        self.data_dir = data_dir
        self.scope = normalize_session_id(scope)
        self.steps_file = data_dir / _STEPS_DIR / f"{self.scope}.json"

    # ------------------------------------------------------------ 永続化

    def _load(self) -> list[Step]:
        if not self.steps_file.exists():
            return []
        try:
            raw = json.loads(self.steps_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []  # 壊れたファイルは空として始める
        if not isinstance(raw, list):
            return []
        return [step for record in raw
                if (step := Step.from_dict(record)) is not None]

    def _save(self, steps: list[Step]) -> None:
        self.steps_file.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps([s.to_dict() for s in steps],
                          ensure_ascii=False, indent=2)
        self.steps_file.write_text(text + "\n", encoding="utf-8")

    # ------------------------------------------------------------ 操作

    def items(self) -> list[Step]:
        """現在の手順の一覧。ほかのセッションの更新も見えるよう、
        返す前にファイルを読み直す。"""
        return self._load()

    def set_items(self, pairs: list[tuple[str, str]],
                  now: datetime | None = None) -> list[Step]:
        """手順を丸ごと置き換えて保存する。

        pairs は (文面, 状態) の並び。文面が空・状態が不正・上限
        （MAX_STEPS 件）超過・「進行中」が複数ある場合は ValueError
        で、手順は変更しない。

        updated_at は、文面と状態が前回から変わっていない項目に
        ついては前回の値を引き継ぎ、新しい項目と状態が変わった
        項目には now（無ければ現在時刻）を付ける。
        """
        if len(pairs) > MAX_STEPS:
            raise ValueError(f"手順は {MAX_STEPS} 件までです")
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        previous = {(s.text, s.status): s.updated_at
                    for s in self._load() if s.updated_at}
        steps: list[Step] = []
        in_progress = 0
        for text, status in pairs:
            text = str(text or "").strip()
            if not text:
                raise ValueError("手順の文面が空です")
            if status not in _STATUSES:
                raise ValueError(
                    f"状態は {' / '.join(_STATUSES)} のいずれかです")
            if status == STATUS_IN_PROGRESS:
                in_progress += 1
            steps.append(Step(
                text=text, status=status,
                updated_at=previous.get((text, status), stamp)))
        if in_progress > 1:
            raise ValueError("「進行中」の手順は同時に 1 つだけにしてください")
        self._save(steps)
        return steps

    def has_open(self) -> bool:
        """終わっていない手順が残っているかどうか。"""
        return any(s.status in _OPEN_STATUSES for s in self._load())
