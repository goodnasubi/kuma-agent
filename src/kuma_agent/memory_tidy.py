"""長期記憶（MEMORY.md）の整理。

育った長期記憶を読みやすく保つための 2 段の整理を用意する
（詳細は docs/DESIGN.md の「長期記憶の整理」節）:

1. 機械的な重複除去（モデル不要・情報損失なし）:
   memory.split_memory_segments で項目単位に割り、空白や前後の
   記号を正規化して「実質同じ項目」が複数ある場合は最初の 1 つ
   だけを残す。見出し行そのものは対象にしない
2. モデルによる統合（任意実行）: 設定済みの頭脳へ 1 回だけ
   問い合わせ（道具なしの単発 chat）、現在の MEMORY.md 全文を
   渡して、重複をまとめ・関連する項目を統合し・古い言い回しを
   そろえた全文を返させる。応答が空・極端に短い（元の 2 割
   未満）場合は失敗として扱い、ファイルは変更しない

適用時は書き換える前に必ず MEMORY.md.bak-<日時> へバック
アップを残す。入口は CLI（kuma-agent memory-tidy）、道具
（tidy_memory）、能動発信の巡回での自動の機械的除去の 3 つで、
モデル統合を自動で行うことはしない（自動は機械的除去だけ）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .llm import ChatModel, Message
from .memory import Memory, split_memory_segments

# 自動の機械的除去の状態ファイル（ユーザーの置き場に置く。
# proactive_state.json と同じ「日付を JSON で記録する」流儀）
AUTO_STATE_FILENAME = "memory_tidy_state.json"

# 自動の機械的除去が働く大きさの目安: 全文注入の上限
# （KUMA_MEMORY_FULL_INJECT_CHARS / Config.memory_full_inject_chars）
# の何倍を超えたら対象にするか
AUTO_TIDY_THRESHOLD_FACTOR = 3

# モデル統合の応答がこれ未満（元の文字数に対する割合）なら
# 失敗として扱う（事実を落としすぎた応答を適用しないため）
_MIN_CONSOLIDATED_RATIO = 0.2

# 項目の先頭に付く箇条書き・番号の記号
_BULLET_RE = re.compile(r"^(?:[-*+•・]\s*|\d+[.)]\s*)")

# 実質判定のキーを作るときに前後から落とす記号類
_EDGE_SYMBOLS = "-*+•・# >\"'“”‘’「」『』（）()［］[]【】<>:：;；、。，．・…‥!！?？~〜―—–-_=|｜/／\\"

# モデル統合用の指示。事実の保持と形式の維持を強く求める
CONSOLIDATION_SYSTEM = (
    "あなたは長期記憶（MEMORY.md）の整理者です。渡された長期記憶の"
    "全文を整理し、整理後の全文だけを返してください。やることは、"
    "重複している項目をまとめ、関連する項目を統合し、古い言い回しを"
    "そろえることです。次の決まりを必ず守ってください:"
    "事実・日付・固有名詞を落とさないこと。"
    "書かれていないことを補わないこと（推測で項目を足さないこと）。"
    "見出し（#）と箇条書き（- ）の形式を保つこと。"
    "説明や前置きは付けず、整理後の全文だけを返すこと。"
)


def line_count(text: str) -> int:
    """行数を数える（報告表示用）。"""
    return len(text.splitlines())


def _segment_key(text: str) -> str:
    """項目の「実質」を表す照合キー。

    行頭の箇条書き記号を取り、空白（全角を含む）をすべて除き、
    前後の記号を落とす。空白の入れ方や箇条書き記号・末尾の
    句読点だけが違う項目は同じキーになる。
    """
    lines = []
    for raw in text.splitlines():
        line = _BULLET_RE.sub("", raw.strip(), count=1)
        lines.append(line)
    key = re.sub(r"\s+", "", "".join(lines))
    return key.strip(_EDGE_SYMBOLS)


def _is_heading_segment(text: str) -> bool:
    """見出し行そのものの項目かどうか（重複除去の対象外）。"""
    for raw in text.splitlines():
        if raw.strip():
            return raw.lstrip().startswith("#")
    return False


def dedupe_memory_text(text: str) -> tuple[str, int]:
    """機械的な重複除去を行い、(整理後の本文, 除去件数) を返す。

    memory.split_memory_segments の項目単位で判定し、実質同じ
    項目が複数ある場合は最初の 1 つだけを残す（順序は保つ）。
    見出し行そのものの項目は対象にしない。項目の途中で切る
    ことはなく、異なる項目の本文はそのまま残るため、情報の
    損失はない。空行による区切りは再構成時に畳まれる。
    """
    segments = split_memory_segments(text)
    if not segments:
        return text, 0
    seen: set[str] = set()
    kept: list[str] = []
    removed = 0
    for seg in segments:
        if _is_heading_segment(seg.text):
            kept.append(seg.text)
            continue
        key = _segment_key(seg.text)
        if not key:
            kept.append(seg.text)
            continue  # 記号だけの項目は判定せず常に残す
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        kept.append(seg.text)
    if removed == 0:
        return text, 0
    return "\n".join(kept) + "\n", removed


def consolidate_memory_text(model: ChatModel, text: str) -> str | None:
    """モデルに長期記憶の全文を統合させ、整理後の全文を返す。

    道具なしの単発 chat で問い合わせる。応答が空、または元の
    2 割未満の長さしかない場合は失敗として None を返す
    （呼び出し側はファイルを変更しないこと）。モデルの例外は
    そのまま伝える（呼び出し側で失敗として扱う）。
    """
    if not text.strip():
        return None
    reply = model.chat(CONSOLIDATION_SYSTEM,
                       [Message("user", text)], [])
    result = (reply.content or "").strip()
    if not result:
        return None
    if len(result) < len(text.strip()) * _MIN_CONSOLIDATED_RATIO:
        return None
    return result + "\n"


def backup_memory_file(memory_file: Path, original_text: str,
                       now: datetime | None = None) -> Path:
    """書き換え前の本文を MEMORY.md.bak-<日時> へ残し、そのパスを返す。

    同じ日時のバックアップが既にある場合は連番を付けて上書きを
    避ける（短時間に連続して整理しても履歴が消えないように）。
    """
    now = now or datetime.now()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    candidate = memory_file.parent / f"{memory_file.name}.bak-{stamp}"
    counter = 1
    while candidate.exists():
        candidate = memory_file.parent / f"{memory_file.name}.bak-{stamp}-{counter}"
        counter += 1
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(original_text, encoding="utf-8")
    return candidate


@dataclass
class TidyResult:
    """整理 1 回分の結果。

    applied は本体（MEMORY.md）を書き換えたかどうか。reason は
    書き換えなかった理由（"" / "empty" / "model_failed" /
    "no_change"）で、proposed_path は --dry-run で提案を書き
    出した場合のそのパス。
    """
    applied: bool
    before_lines: int
    after_lines: int
    removed_count: int = 0
    backup_path: Path | None = None
    final_text: str = ""
    reason: str = ""
    proposed_path: Path | None = None


def _build_final_text(memory: Memory, model: ChatModel) -> TidyResult:
    """機械的除去→モデル統合の順で最終本文を作る（まだ書き込まない）。

    モデル統合が失敗した場合は、機械的除去の分も含めてファイルは
    変更しない方針のため、final_text は元の本文のまま reason が
    "model_failed" になる。
    """
    original = memory.load_long_term()
    before = line_count(original)
    if not original.strip():
        return TidyResult(False, before, before, final_text=original,
                          reason="empty")
    deduped, removed = dedupe_memory_text(original)
    try:
        consolidated = consolidate_memory_text(model, deduped)
    except Exception:
        consolidated = None
    if consolidated is None:
        return TidyResult(False, before, before, removed_count=removed,
                          final_text=original, reason="model_failed")
    return TidyResult(False, before, line_count(consolidated),
                      removed_count=removed, final_text=consolidated)


def _apply(memory: Memory, result: TidyResult,
           now: datetime | None = None) -> TidyResult:
    """最終本文を本体へ書き込む（バックアップを残してから）。

    元の本文と変わらない場合は書き込まず、バックアップも作らない。
    """
    original = memory.load_long_term()
    if result.final_text == original:
        result.reason = "no_change"
        return result
    result.backup_path = backup_memory_file(
        memory.memory_file, original, now=now)
    memory.memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory.memory_file.write_text(result.final_text, encoding="utf-8")
    result.applied = True
    return result


def tidy_memory_with_model(memory: Memory, model: ChatModel,
                           now: datetime | None = None) -> TidyResult:
    """機械的除去→モデル統合の順で整理し、本体へ適用する。

    記憶が無い・空の場合は何もしない（エラーにしない）。モデル
    統合が失敗した場合はファイルを変更しない。
    """
    result = _build_final_text(memory, model)
    if result.reason:
        return result
    return _apply(memory, result, now=now)


def propose_memory_tidy(memory: Memory, model: ChatModel) -> TidyResult:
    """整理の提案だけを作り、MEMORY.md.proposed へ書き出す。

    本体（MEMORY.md）は変更せず、バックアップも作らない。
    モデル統合が失敗した場合は提案ファイルも作らない。
    """
    result = _build_final_text(memory, model)
    if result.reason:
        return result
    proposed = memory.memory_file.parent / f"{memory.memory_file.name}.proposed"
    proposed.parent.mkdir(parents=True, exist_ok=True)
    proposed.write_text(result.final_text, encoding="utf-8")
    result.proposed_path = proposed
    return result


def apply_mechanical_tidy(memory: Memory,
                          now: datetime | None = None) -> TidyResult:
    """機械的な重複除去だけを本体へ適用する（モデルは使わない）。

    除去できる項目が無い場合は何もしない（バックアップも作らない）。
    """
    original = memory.load_long_term()
    before = line_count(original)
    if not original.strip():
        return TidyResult(False, before, before, final_text=original,
                          reason="empty")
    deduped, removed = dedupe_memory_text(original)
    result = TidyResult(False, before, line_count(deduped),
                        removed_count=removed, final_text=deduped)
    if removed == 0:
        result.reason = "no_change"
        return result
    return _apply(memory, result, now=now)


# ---------------------------------------------------------- 自動実行

def _last_auto_tidy_on(user_data_dir: Path) -> str | None:
    """最後に自動の機械的除去をチェックした日（YYYY-MM-DD）。"""
    try:
        raw = json.loads(
            (user_data_dir / AUTO_STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict) and isinstance(raw.get("last_auto_tidy_on"), str):
        return raw["last_auto_tidy_on"]
    return None


def _record_auto_tidy(user_data_dir: Path, day: str) -> None:
    """自動の機械的除去をチェックした日を記録する。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"last_auto_tidy_on": day}, ensure_ascii=False)
    (user_data_dir / AUTO_STATE_FILENAME).write_text(text + "\n",
                                                     encoding="utf-8")


def auto_tidy_memory(user_data_dir: Path, full_inject_chars: int,
                     now: datetime | None = None) -> TidyResult | None:
    """巡回用の自動の機械的除去。条件を満たさなければ None（何もしない）。

    条件はすべて満たす場合だけ実行する:
    - そのユーザーの MEMORY.md が大きい（全文注入の上限
      full_inject_chars の AUTO_TIDY_THRESHOLD_FACTOR 倍を超える）
    - 本日まだチェックしていない（1 日 1 回まで。除去できる
      項目が無かった日もチェック済みとして記録する）

    モデル統合は自動では行わない（裏側で勝手にモデルの費用を
    かけて本文を書き換えることはしない）。適用しても会話への
    投稿はしない。記憶が無い・小さい場合も何もしない。
    """
    now = now or datetime.now()
    memory = Memory(user_data_dir)
    text = memory.load_long_term()
    if not text.strip():
        return None
    if len(text) <= full_inject_chars * AUTO_TIDY_THRESHOLD_FACTOR:
        return None  # まだ小さいので対象外
    today = now.date().isoformat()
    if _last_auto_tidy_on(user_data_dir) == today:
        return None  # 本日はチェック済み
    _record_auto_tidy(user_data_dir, today)
    return apply_mechanical_tidy(memory, now=now)
