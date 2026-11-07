"""長期記憶の自動抽出（会話 → MEMORY.md）。

会話の中で道具（remember）を使って書き留める以外に、裏側でも
新しい会話から「長期記憶に残すべきこと」を自動で抜き出して
MEMORY.md へ足す仕組み（詳細は docs/DESIGN.md の「長期記憶の
自動抽出」節）:

- 入口は 2 つ: 能動発信の巡回（proactive.check_and_post）での
  自動抽出（1 日 1 回まで）と、CLI `kuma-agent memory-extract`
  での手動実行（--dry-run で提案だけ表示）。自動抽出は
  環境変数 KUMA_MEMORY_EXTRACT=0 で無効化できる
- 対象は前回の抽出以降に増えた会話の発話だけ。処理済みの
  位置（会話ファイルごとの読み終えたレコード数）はユーザーの
  置き場の memory_extract_state.json に持つ。能動発信の投稿
  （proactive の印つき）・要約（role=summary）・委任の子
  （delegate-* のセッション）は材料に入れない
- モデルへは道具なしの単発 chat で、新しい会話の抜粋と現在の
  MEMORY.md を渡し、箇条書きだけを返させる。「なし」・空・
  箇条書きでない応答・例外の場合は MEMORY.md を変更しない
- 期限のある一時的な情報は、項目の末尾に（期限: YYYY-MM-DD）
  の形で期限を添えて返させる（AM-2）。期限の印は MEMORY.md
  には書かず、項目の属性（memory_meta.json）へ振り分ける。
  印が無い従来形式の箇条書きもそのまま受け付ける（倒し込み）
- 適用するときは、書き込む前に必ず MEMORY.md.bak-<日時> へ
  控えを取り（memory_tidy と同じ作法。MEMORY.md がまだ無い
  場合は控えは作らない）、末尾へ足してから機械的な重複除去
  （memory_tidy.dedupe_memory_text）を通す
- 材料が大きい場合は上限（MAX_INPUT_CHARS）で切り、処理した
  分だけ処理済み位置を進める。失敗した日は、その日のうちは
  再試行しない（試行日だけを記録し、位置は進めない）
- モデルが要る処理なので、モデルなしの一時 Session の巡回
  （CLI の `kuma-agent proactive` 経路）では実行せず、試行の
  記録も残さない。自動抽出は会話への投稿をしない
- 巡回での実行は、活動の記録（activity の background_model、
  成功・失敗と追加件数だけ）と使用量（usage、セッション ID は
  background:memory_extract）に残す（歯止めの漏れ点検 L3 の
  直し、0.88.0）。CLI の明示実行（extract_memory の直接呼び
  出し）は記録しない。記録の失敗で巡回は落とさない
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .activity import record_background_model
from .llm import ChatModel, Message, Usage, model_name_of
from .memory import Memory, split_memory_segments
from .memory_meta import record_memory_items
from .memory_tidy import _segment_key, backup_memory_file, dedupe_memory_text
from .usage import record_usage

# 使用量の記録で使う、裏側の抽出呼び出しのセッション ID
BACKGROUND_SESSION_ID = "background:memory_extract"

# 状態ファイル（ユーザーの置き場に置く。memory_tidy_state.json
# と同じ「日付と進捗を JSON で記録する」流儀）
STATE_FILENAME = "memory_extract_state.json"

# 無効化の環境変数（"0" で無効、それ以外・未設定は有効）。
# transcribe.py の KUMA_TRANSCRIBE と同じ流儀で、モジュール側で
# 呼ぶ時点に読む
EXTRACT_ENV = "KUMA_MEMORY_EXTRACT"

# モデルへ渡す新しい会話の抜粋の上限（文字数）。超える分は
# 次回に回し、処理した分だけ処理済み位置を進める
MAX_INPUT_CHARS = 12000

# 委任の子の会話ファイルの接頭辞（材料から除く）
_DELEGATE_PREFIX = "delegate-"

# 箇条書きの先頭記号（行頭）。MEMORY.md の文体は「- 」だが、
# モデルが返しがちな * / + / ・も受け付けて「- 」にそろえる
_BULLET_START_RE = re.compile(r"^(?:[-*+]\s+|・\s*)")

# 項目の末尾に付けられる期限の印（AM-2）。期限のある一時的な
# 情報だけに付けさせ、本文（MEMORY.md）には残さず属性へ回す
_UNTIL_SUFFIX_RE = re.compile(
    r"[（(]\s*期限\s*[:：]\s*(\d{4}-\d{2}-\d{2})\s*[）)]\s*$")


# 抽出用の指示。事実の保持と、すでにある内容を繰り返さないことを
# 強く求める（memory_tidy の統合用指示と同じ考え方）
EXTRACTION_SYSTEM = (
    "あなたは長期記憶（MEMORY.md）の抽出者です。渡された新しい会話"
    "から、利用者についての事実・好み・決めたこと・約束のうち、"
    "長期記憶に残す価値があるものを抜き出してください。"
    "次の決まりを必ず守ってください:"
    "今の長期記憶の文体に合わせた箇条書き（- ）で、1 件 1 行で返すこと。"
    "すでに長期記憶に書いてある内容は繰り返さないこと。"
    "会話に書かれていないことを補わないこと（推測で項目を足さないこと）。"
    "残す価値があるものが無ければ「なし」とだけ返すこと。"
    "説明や前置きは付けず、箇条書き（または「なし」）だけを返すこと。"
    "期限のある一時的な情報（「今週末まで」「来月いっぱい」など）に"
    "ついては、その項目の末尾に（期限: YYYY-MM-DD）の形で西暦の"
    "期限を添えること。期限はプロンプトに示す今日の日付を基準に"
    "計算すること。期限が読み取れない項目や、長く残す情報には"
    "期限を付けないこと。"
)


def extraction_enabled() -> bool:
    """自動抽出が有効かどうか（KUMA_MEMORY_EXTRACT=0 で無効化）。"""
    return os.environ.get(EXTRACT_ENV, "") != "0"


# ---------------------------------------------------------- 状態

def _load_state(user_data_dir: Path) -> dict:
    """memory_extract_state.json を読む。無い・壊れていたら空の辞書。"""
    try:
        raw = json.loads(
            (user_data_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(user_data_dir: Path, state: dict) -> None:
    """memory_extract_state.json を書き直す（他の項目は保持する）。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False)
    (user_data_dir / STATE_FILENAME).write_text(text + "\n", encoding="utf-8")


def _positions_from(state: dict) -> dict[str, int]:
    """状態から会話ファイルごとの処理済み位置を取り出す。

    壊れた項目（数値でない・負数）は無視し、0 から読み直す
    （再抽出は重複除去とモデルの指示で抑えられる）。
    """
    raw = state.get("positions")
    if not isinstance(raw, dict):
        return {}
    positions: dict[str, int] = {}
    for name, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        positions[str(name)] = value
    return positions


def _last_attempt_on(state: dict) -> str | None:
    """最後に自動抽出を試行した日（YYYY-MM-DD）。無ければ None。"""
    value = state.get("last_attempt_on")
    return value if isinstance(value, str) else None


# ---------------------------------------------------------- 材料の収集

def _read_records(path: Path) -> list[dict]:
    """会話記録ファイルのレコードを順に読む（壊れた行は読み飛ばす）。

    記録は追記のみなので、レコードの並び順は安定している。
    処理済み位置はこの並びでの件数として数える。
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and "role" in record:
            records.append(record)
    return records


def _is_material(record: dict) -> bool:
    """抽出の材料になる発話かどうか。

    利用者とエージェントの発話だけを対象にし、要約
    （role=summary）や道具の結果は role で外れ、能動発信の投稿
    （proactive の印つき）も外す（エージェント自身の声かけを
    「会話で分かったこと」として覚え直さないため）。
    """
    if record.get("role") not in ("user", "assistant"):
        return False
    if record.get("proactive"):
        return False
    content = record.get("content")
    return isinstance(content, str) and bool(content.strip())


def _format_record(record: dict) -> str:
    """発話をモデルへ渡す 1 行にする（要約の整形と同じ話者表記）。"""
    speaker = "利用者" if record.get("role") == "user" else "クマ"
    return f"{speaker}: {record.get('content', '')}"


@dataclass
class _Collected:
    """新しい会話から集めた材料と、消費後の処理済み位置。"""
    text: str
    positions: dict[str, int]


def _collect_new_material(user_data_dir: Path,
                          positions: dict[str, int]) -> _Collected | None:
    """処理済み位置より後の発話から、モデルへ渡す材料を集める。

    会話ファイル（delegate-* を除く）を名前順に走査し、材料に
    なる発話を上限（MAX_INPUT_CHARS）に収まる範囲で取り込む。
    位置は「最後まで読み終えたところ」まで進める: 上限で止めた
    ファイルでは、その発話の手前まで。材料にならない発話
    （要約・能動発信など）は読み飛ばしとして消費する。材料が
    1 件も無ければ None を返す。1 件だけで上限を超える発話は、
    詰まらないよう切って取り込み、消費したものとして扱う。
    """
    conv = user_data_dir / "conversations"
    if not conv.is_dir():
        return None
    new_positions = dict(positions)
    lines: list[str] = []
    total = 0
    for path in sorted(conv.glob("*.jsonl")):
        stem = path.stem
        if stem.startswith(_DELEGATE_PREFIX):
            continue
        records = _read_records(path)
        idx = min(positions.get(stem, 0), len(records))
        stopped = False
        while idx < len(records):
            record = records[idx]
            if not _is_material(record):
                idx += 1  # 材料にならない発話は消費して読み飛ばす
                continue
            line = _format_record(record)
            cost = len(line) + 1  # 結合時の改行分
            if total + cost > MAX_INPUT_CHARS:
                if not lines:
                    # 1 件だけで上限を超える場合は切って取り込む
                    lines.append(line[:MAX_INPUT_CHARS])
                    idx += 1
                stopped = True
                break
            lines.append(line)
            total += cost
            idx += 1
        new_positions[stem] = idx
        if stopped:
            break
    if not lines:
        return None
    return _Collected("\n".join(lines), new_positions)


# ---------------------------------------------------------- モデルの応答

def _split_until(body: str) -> tuple[str, str | None]:
    """項目本文の末尾にある期限の印（AM-2）を (本文, 期限) に分ける。

    印が無い、または日付として読めない場合は、本文はそのまま
    で期限は None（従来の箇条書きと同じ扱いに倒す）。
    """
    match = _UNTIL_SUFFIX_RE.search(body)
    if match is None:
        return body, None
    until = match.group(1)
    try:
        parsed = datetime.strptime(until, "%Y-%m-%d")
    except ValueError:
        return body, None
    if parsed.strftime("%Y-%m-%d") != until:
        return body, None
    return body[:match.start()].rstrip(), until


def _parse_items(text: str) -> list[tuple[str, str | None]] | None:
    """モデルの応答を (箇条書き, 期限) の項目列として読む。

    「なし」（末尾の「。」は許容）なら空のリスト、箇条書きだけの
    応答なら「- 」にそろえた項目列を返す。期限の印が付いた項目
    は、印を本文から外して期限と組にする（印が無い項目は期限
    None で、従来形式の応答もそのまま受け付ける）。空の応答や、
    箇条書きでない行を含む応答は失敗として None を返す（変な
    文章をそのまま長期記憶へ足さないため）。
    """
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.rstrip("。") == "なし":
        return []
    items: list[tuple[str, str | None]] = []
    for raw in stripped.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _BULLET_START_RE.match(line)
        if match is None:
            return None
        body = line[match.end():].strip()
        if not body:
            return None
        body, until = _split_until(body)
        if not body:
            return None
        items.append((f"- {body}", until))
    return items or None


def _parse_bullets(text: str) -> list[str] | None:
    """モデルの応答を箇条書きの項目列として読む（期限の印は外す）。"""
    items = _parse_items(text)
    if items is None:
        return None
    return [bullet for bullet, _until in items]


def build_extraction_prompt(memory_text: str, material: str,
                            today: str = "") -> str:
    """抽出の単発呼び出しでモデルへ渡す本文を組み立てる。

    today（YYYY-MM-DD）があれば先頭に今日の日付を示す（期限の
    印を西暦で計算させるための基準）。
    """
    current = memory_text if memory_text.strip() else "（まだ何も書かれていません）"
    parts: list[str] = []
    if today:
        parts.append(f"# 今日の日付\n{today}")
    parts.append(f"# 現在の長期記憶（MEMORY.md）\n{current}")
    parts.append(f"# 新しい会話\n{material}")
    return "\n\n".join(parts)


# ---------------------------------------------------------- 抽出の実行

@dataclass
class ExtractResult:
    """抽出 1 回分の結果。

    applied は本体（MEMORY.md）を書き換えたかどうか。bullets は
    モデルが返した箇条書き（dry-run の表示と件数の報告に使う）。
    items は bullets に期限（AM-2）を組にしたもの。reason は
    書き換えなかった理由（"" / "no_new_turns" / "nothing" /
    "no_change" / "model_failed"）。usage はモデルが応答に
    含めていた使用量（応答が返った場合だけ。例外で応答が無い
    場合と、材料が無くてモデルを呼ばなかった場合は None）。
    巡回側が使用量の記録へ流すために持つ。
    """
    applied: bool
    bullets: list[str] = field(default_factory=list)
    backup_path: Path | None = None
    reason: str = ""
    items: list[tuple[str, str | None]] = field(default_factory=list)
    usage: Usage | None = None

    @property
    def added_count(self) -> int:
        return len(self.bullets)


def extract_memory(memory: Memory, model: ChatModel,
                   now: datetime | None = None,
                   dry_run: bool = False) -> ExtractResult:
    """新しい会話から長期記憶を抽出し、MEMORY.md へ足す。

    処理済み位置より後の発話だけを材料にする。モデルが箇条書き
    を返したら、控えを取って末尾へ足し、機械的な重複除去を
    通してから書き込む。足した結果が元の本文と変わらない場合
    （すべて重複だった場合）は書き込まず、控えも作らない
    （memory_tidy の適用と同じ作法）。「なし」の場合も本文は
    変えない。成功・「なし」・変更なしの場合は処理済み位置を
    進めるが、失敗（例外・空・箇条書きでない応答）の場合は
    進めない（翌日に同じ分から再挑戦できる）。

    dry_run ではモデルへの問い合わせまで行い、本文も状態も
    一切変更しない（提案の箇条書きだけを結果に載せる）。
    試行日の記録（1 日 1 回の上限）は呼び出し側（自動実行）の
    役目で、ここでは扱わない。
    """
    now = now or datetime.now()
    user_data_dir = memory.data_dir
    state = _load_state(user_data_dir)
    collected = _collect_new_material(user_data_dir,
                                      _positions_from(state))
    if collected is None:
        return ExtractResult(False, reason="no_new_turns")
    memory_text = memory.load_long_term()
    prompt = build_extraction_prompt(memory_text, collected.text,
                                     today=now.date().isoformat())
    try:
        reply = model.chat(EXTRACTION_SYSTEM, [Message("user", prompt)], [])
    except Exception:
        return ExtractResult(False, reason="model_failed")
    items = _parse_items(reply.content or "")
    if items is None:
        return ExtractResult(False, reason="model_failed",
                             usage=reply.usage)
    bullets = [bullet for bullet, _until in items]
    if not bullets:
        if not dry_run:
            state["positions"] = collected.positions
            _save_state(user_data_dir, state)
        return ExtractResult(False, reason="nothing", usage=reply.usage)
    if dry_run:
        return ExtractResult(False, bullets=bullets, items=items,
                             usage=reply.usage)
    # 末尾へ足してから機械的な重複除去を通す
    base = memory_text
    if base and not base.endswith("\n"):
        base += "\n"
    appended = base + "\n".join(bullets) + "\n"
    final, _removed = dedupe_memory_text(appended)
    # 処理済み位置は、書き込みの有無にかかわらず進める
    state["positions"] = collected.positions
    _save_state(user_data_dir, state)
    # 項目の属性（AM-2）: 書き込み後の本文に残っている項目に
    # ついて、覚えた日と期限（印があった項目だけ）を記録する。
    # 重複除去で残らなかった項目には付けない。すでに属性がある
    # 項目が会話で改めて触れられた場合は、覚えた日が今日に
    # 更新される（確認済みの印を兼ねる）。本文が変わらなかった
    # 場合（すべて重複だった場合）も、残っている項目への記録は
    # 同じに行う
    present = {_segment_key(seg.text)
               for seg in split_memory_segments(final)}
    record_memory_items(
        user_data_dir,
        [(bullet, until) for bullet, until in items
         if _segment_key(bullet) in present],
        now=now)
    if final == memory_text:
        return ExtractResult(False, bullets=bullets, reason="no_change",
                             items=items, usage=reply.usage)
    backup_path = None
    if memory.memory_file.exists():
        backup_path = backup_memory_file(memory.memory_file, memory_text,
                                         now=now)
    memory.memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory.memory_file.write_text(final, encoding="utf-8")
    return ExtractResult(True, bullets=bullets, backup_path=backup_path,
                         items=items, usage=reply.usage)


def _record_background_run(user_data_dir: Path, result: ExtractResult,
                           model: ChatModel,
                           provider_of: Callable[[], str] | None,
                           now: datetime) -> None:
    """巡回の自動抽出の実行を、活動の記録と使用量へ残す（L3 の直し）。

    活動の記録には成功・失敗と追加件数だけを残し（プロンプトや
    会話の全文は残さない）、使用量には応答に含まれる usage を
    流す。使用量は、モデルから応答が返った場合だけ記録する
    （例外で応答が無い場合は、Session の記録と同じく残さない。
    応答は返ったが形が壊れていた場合は usage が読めた分だけ
    残る）。provider_of は使用量に載せる頭脳名を引く関数で、
    モデルを引いた後にだけ呼ばれる。記録の失敗で巡回を落とさ
    ないよう、例外は外へ出さない。
    """
    failed = result.reason == "model_failed"
    try:
        record_background_model(
            user_data_dir, "memory_extract",
            "モデル呼び出しに失敗" if failed
            else f"追加 {result.added_count} 件",
            ok=not failed, result=result.reason or "applied", now=now)
    except Exception:
        pass  # 記録の失敗で巡回全体は落とさない
    if failed and result.usage is None:
        return  # 応答が返らなかった呼び出しは使用量に残さない
    try:
        provider = provider_of() if provider_of is not None else ""
        record_usage(user_data_dir, provider, model_name_of(model),
                     result.usage, BACKGROUND_SESSION_ID, now=now)
    except Exception:
        pass  # 記録の失敗で巡回全体は落とさない


def auto_extract_memory(
        user_data_dir: Path,
        model_provider: Callable[[], ChatModel | None],
        now: datetime | None = None,
        provider_of: Callable[[], str] | None = None) -> ExtractResult | None:
    """巡回用の自動抽出。条件を満たさなければ None（何もしない）。

    条件はすべて満たす場合だけ実行する:
    - 自動抽出が有効（KUMA_MEMORY_EXTRACT=0 でない）
    - 本日まだ試行していない（1 日 1 回まで。失敗した日も
      試行として記録し、その日は再試行しない）
    - 前回の抽出以降に増えた会話の発話（材料）がある
    - モデルが引ける（model_provider が None を返したら、
      モデルなし経路として試行の記録も残さず何もしない）

    試行日はモデルを呼ぶ前に記録する（失敗してもその日は
    再試行しないため）。処理済み位置は extract_memory が
    成功・「なし」のときだけ進める。適用しても会話への投稿は
    しない。実行したら、活動の記録と使用量へ残す（巡回分だけ。
    provider_of は使用量に載せる頭脳名を引く関数）。
    """
    now = now or datetime.now()
    if not extraction_enabled():
        return None
    state = _load_state(user_data_dir)
    today = now.date().isoformat()
    if _last_attempt_on(state) == today:
        return None  # 本日は試行済み
    if _collect_new_material(user_data_dir,
                             _positions_from(state)) is None:
        return None  # 新しい材料が無い（試行として記録しない）
    model = model_provider()
    if model is None:
        return None  # モデルなし経路では試行も記録しない
    state["last_attempt_on"] = today
    _save_state(user_data_dir, state)
    result = extract_memory(Memory(user_data_dir), model, now=now)
    if result.reason != "no_new_turns":
        # 材料があることを確認してから呼んでいるため通常は
        # 起きないが、モデルを呼んでいない実行は記録しない
        _record_background_run(user_data_dir, result, model,
                               provider_of, now)
    return result
