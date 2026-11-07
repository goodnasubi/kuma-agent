"""手順書の自動学習（会話 → skills/）。

手順書（スキル）は従来、運用者が置くか、会話の中で利用者に
教わって save_skill 道具で保存するものだった。ここでは裏側で
も、新しい会話から「再利用できる手順・やり方」を自動で抜き
出して新しい手順書として保存する（長期記憶の自動抽出
memory_extract の手順書版。詳細は docs/DESIGN.md の
「手順書の自動学習」節）:

- 入口は 2 つ: 能動発信の巡回（proactive.check_and_post）での
  自動学習（1 日 1 回まで）と、CLI `kuma-agent skill-learn`
  での手動実行（--dry-run で候補だけ表示）。自動学習は
  環境変数 KUMA_SKILL_LEARN=0 で無効化できる
- 対象は前回の学習以降に増えた会話の発話だけ。材料の集め方
  （対象の会話ファイル、能動発信・要約・委任の子の除外、
  上限での切り方）は memory_extract と同じで、収集部品を
  共有する。処理済みの位置はユーザーの置き場の
  skill_learn_state.json に持ち、memory_extract の状態とは
  独立に進める
- モデルへは道具なしの単発 chat で、新しい会話の抜粋と既存の
  手順書の目次（名前と説明）を渡し、候補を決まった形の
  JSON で返させる。形が壊れている・例外の場合は何もしない
- 安全のための線引き: 自動で保存するのは「新しい手順書」
  だけ。既存の手順書（運用者や会話で保存したもの）と名前が
  重なる候補は捨て、上書きは決してしない。1 回に保存する
  のは最大 3 件まで。本文にも上限を設ける
- 保存したら、その利用者のフィードに「手順書を覚えた」
  ことを 1 件残す（学習が目に見えるように。会話への投稿は
  しない）。失敗した日は、その日のうちは再試行しない
  （試行日だけを記録し、位置は進めない）
- モデルが要る処理なので、モデルなしの一時 Session の巡回
  では実行せず、試行の記録も残さない（memory_extract と同じ）
- 巡回での実行は、活動の記録（activity の background_model、
  成功・失敗と保存件数だけ）と使用量（usage、セッション ID は
  background:skill_learn）に残す（歯止めの漏れ点検 L3 の
  直し、0.88.0。memory_extract と同じ作法）。CLI の明示実行
  （learn_skills の直接呼び出し）は記録しない。記録の失敗で
  巡回は落とさない
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .activity import record_background_model
from .feed import KIND_PROACTIVE, append_feed_entry
from .llm import ChatModel, Message, Usage, model_name_of
from .memory_extract import _collect_new_material
from .skills import is_valid_skill_name, load_skills, save_skill
from .usage import record_usage

# 使用量の記録で使う、裏側の学習呼び出しのセッション ID
BACKGROUND_SESSION_ID = "background:skill_learn"

# 状態ファイル（ユーザーの置き場に置く。memory_extract_state.json
# と同じ「日付と進捗を JSON で記録する」流儀）
STATE_FILENAME = "skill_learn_state.json"

# 無効化の環境変数（"0" で無効、それ以外・未設定は有効）
LEARN_ENV = "KUMA_SKILL_LEARN"

# 1 回の学習で保存する候補の上限と、本文・説明の上限
MAX_SKILLS_PER_RUN = 3
MAX_BODY_CHARS = 4000
MAX_DESCRIPTION_CHARS = 120

# 学習用の指示。既存の手順書と重ねないこと、長期記憶の領分
# （事実・好み）を持ち込まないことを強く求める
LEARNING_SYSTEM = (
    "あなたは手順書（スキル）の学習者です。渡された新しい会話から、"
    "次回も同じ進め方が役に立つ「再利用できる手順・やり方」があれば、"
    "新しい手順書の候補として抜き出してください。"
    "対象は、利用者が教えた手順、うまくいった段取り、訂正された"
    "やり方など、手順として書き残す価値があるものだけです。"
    "利用者についての事実・好み・約束は長期記憶の領分なので対象外です。"
    "次の決まりを必ず守ってください:"
    "すでに目次にある手順書と重なる内容は返さないこと（既存の手順書の"
    "更新はしないこと）。"
    "会話に書かれていない手順を補わないこと（推測で作らないこと）。"
    "返すのは JSON だけにし、説明や前置きは付けないこと。"
    "形式は {\"skills\": [{\"name\": \"名前\", \"description\": \"短い説明\", "
    "\"body\": \"手順の本文（Markdown）\"}]} とし、名前は英数・日本語・"
    "ハイフン・アンダースコアだけを使うこと。"
    "候補が無ければ {\"skills\": []} を返すこと。"
)


def learn_enabled() -> bool:
    """自動学習が有効かどうか（KUMA_SKILL_LEARN=0 で無効化）。"""
    return os.environ.get(LEARN_ENV, "") != "0"


# ---------------------------------------------------------- 状態

def _load_state(user_data_dir: Path) -> dict:
    """skill_learn_state.json を読む。無い・壊れていたら空の辞書。"""
    try:
        raw = json.loads(
            (user_data_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(user_data_dir: Path, state: dict) -> None:
    """skill_learn_state.json を書き直す（他の項目は保持する）。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False)
    (user_data_dir / STATE_FILENAME).write_text(text + "\n", encoding="utf-8")


def _positions_from(state: dict) -> dict[str, int]:
    """状態から会話ファイルごとの処理済み位置を取り出す。

    壊れた項目（数値でない・負数）は無視し、0 から読み直す
    （再学習は名前の重なりの検査で抑えられる）。
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
    """最後に自動学習を試行した日（YYYY-MM-DD）。無ければ None。"""
    value = state.get("last_attempt_on")
    return value if isinstance(value, str) else None


# ---------------------------------------------------------- モデルの応答

def _parse_candidates(text: str) -> list[dict] | None:
    """モデルの応答を候補の列として読む。

    JSON（``` の囲みは許容）で、{"skills": [...]} の形だけを
    受ける。各候補は name / description / body の文字列を
    そろえた辞書にする。形が壊れていれば None（失敗）。
    候補が 1 件も無い正しい応答は空のリスト。
    """
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        raw = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    items = raw.get("skills")
    if not isinstance(items, list):
        return None
    candidates: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        name = item.get("name")
        description = item.get("description")
        body = item.get("body")
        if not (isinstance(name, str) and isinstance(description, str)
                and isinstance(body, str)):
            return None
        candidates.append({"name": name.strip(),
                           "description": " ".join(description.split()),
                           "body": body.strip()})
    return candidates


def build_learning_prompt(catalog_text: str, material: str) -> str:
    """学習の単発呼び出しでモデルへ渡す本文を組み立てる。"""
    return (f"# 既存の手順書の目次\n{catalog_text}\n\n"
            f"# 新しい会話\n{material}")


def _catalog_text(data_dir: Path) -> str:
    """既存の手順書の目次（名前と説明）をモデルへ渡す形にする。"""
    skills = load_skills(data_dir)
    if not skills:
        return "（まだ手順書はありません）"
    return "\n".join(f"- {s.name}: {s.description}" for s in skills)


# ---------------------------------------------------------- 学習の実行

@dataclass
class LearnResult:
    """学習 1 回分の結果。

    learned は保存した手順書の名前の列（適用した場合）。
    candidates はモデルが返した候補のうち検証を通ったもの
    （dry-run の表示に使う）。reason は保存しなかった理由
    （"" / "no_new_turns" / "nothing" / "model_failed"）。usage
    はモデルが応答に含めていた使用量（応答が返った場合だけ。
    例外で応答が無い場合と、材料が無くてモデルを呼ばなかった
    場合は None）。巡回側が使用量の記録へ流すために持つ。
    """
    learned: list[str] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    reason: str = ""
    usage: Usage | None = None


def learn_skills(data_dir: Path, user_data_dir: Path, model: ChatModel,
                 now: datetime | None = None,
                 dry_run: bool = False) -> LearnResult:
    """新しい会話から手順書を学習し、新しい手順書として保存する。

    処理済み位置より後の発話だけを材料にする。モデルの候補の
    うち、名前が安全な形で、既存の手順書と重ならず、説明と
    本文が上限に収まるものだけを保存する（最大
    MAX_SKILLS_PER_RUN 件）。既存の手順書は決して上書き
    しない。成功・候補なしの場合は処理済み位置を進めるが、
    失敗（例外・形の壊れた応答）の場合は進めない（翌日に
    同じ分から再挑戦できる）。

    dry_run ではモデルへの問い合わせまで行い、手順書も状態も
    一切変更しない（検証を通った候補だけを結果に載せる）。
    試行日の記録（1 日 1 回の上限）は呼び出し側（自動実行）の
    役目で、ここでは扱わない。
    """
    now = now or datetime.now()
    state = _load_state(user_data_dir)
    collected = _collect_new_material(user_data_dir,
                                      _positions_from(state))
    if collected is None:
        return LearnResult(reason="no_new_turns")
    prompt = build_learning_prompt(_catalog_text(data_dir), collected.text)
    try:
        reply = model.chat(LEARNING_SYSTEM, [Message("user", prompt)], [])
    except Exception:
        return LearnResult(reason="model_failed")
    parsed = _parse_candidates(reply.content or "")
    if parsed is None:
        return LearnResult(reason="model_failed", usage=reply.usage)

    existing = {s.name for s in load_skills(data_dir)}
    accepted: list[dict] = []
    seen: set[str] = set(existing)
    for candidate in parsed:
        name = candidate["name"]
        if name in seen or not is_valid_skill_name(name):
            continue  # 既存と重なる・名前が不正な候補は捨てる
        if not candidate["description"] or not candidate["body"]:
            continue
        if len(candidate["description"]) > MAX_DESCRIPTION_CHARS:
            continue
        if len(candidate["body"]) > MAX_BODY_CHARS:
            continue
        seen.add(name)
        accepted.append(candidate)
        if len(accepted) >= MAX_SKILLS_PER_RUN:
            break

    if not dry_run:
        # 候補なし・全滅でも、材料は見たものとして位置を進める
        state["positions"] = collected.positions
        _save_state(user_data_dir, state)
    if not accepted:
        return LearnResult(reason="nothing", usage=reply.usage)
    if dry_run:
        return LearnResult(candidates=accepted, usage=reply.usage)

    learned: list[str] = []
    for candidate in accepted:
        try:
            save_skill(data_dir, candidate["name"],
                       candidate["description"], candidate["body"])
        except (OSError, ValueError):
            continue  # 保存に失敗した候補は飛ばす（位置は進めたまま）
        learned.append(candidate["name"])
    if learned:
        names = "、".join(f"「{name}」" for name in learned)
        append_feed_entry(user_data_dir, KIND_PROACTIVE,
                          f"手順書 {names} を新しく覚えました")
    return LearnResult(learned=learned, candidates=accepted,
                       usage=reply.usage)


def _record_background_run(user_data_dir: Path, result: LearnResult,
                           model: ChatModel,
                           provider_of: Callable[[], str] | None,
                           now: datetime) -> None:
    """巡回の自動学習の実行を、活動の記録と使用量へ残す（L3 の直し）。

    活動の記録には成功・失敗と保存件数だけを残し（プロンプトや
    会話の全文は残さない）、使用量には応答に含まれる usage を
    流す。使用量は、モデルから応答が返った場合だけ記録する
    （例外で応答が無い場合は、Session の記録と同じく残さない。
    応答は返ったが形が壊れていた場合は usage が読めた分だけ
    残る）。provider_of は使用量に載せる頭脳名を引く関数で、
    モデルを引いた後にだけ呼ばれる。記録の失敗で巡回を落とさ
    ないよう、例外は外へ出さない。
    """
    failed = result.reason == "model_failed"
    if failed:
        summary = "モデル呼び出しに失敗"
    elif result.learned:
        names = "、".join(f"「{name}」" for name in result.learned)
        summary = f"保存 {len(result.learned)} 件（{names}）"
    else:
        summary = "保存 0 件"
    try:
        record_background_model(
            user_data_dir, "skill_learn", summary,
            ok=not failed, result=result.reason or "learned", now=now)
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


def auto_learn_skills(
        data_dir: Path, user_data_dir: Path,
        model_provider: Callable[[], ChatModel | None],
        now: datetime | None = None,
        provider_of: Callable[[], str] | None = None) -> LearnResult | None:
    """巡回用の自動学習。条件を満たさなければ None（何もしない）。

    条件はすべて満たす場合だけ実行する:
    - 自動学習が有効（KUMA_SKILL_LEARN=0 でない）
    - 本日まだ試行していない（1 日 1 回まで。失敗した日も
      試行として記録し、その日は再試行しない）
    - 前回の学習以降に増えた会話の発話（材料）がある
    - モデルが引ける（model_provider が None を返したら、
      モデルなし経路として試行の記録も残さず何もしない）

    試行日はモデルを呼ぶ前に記録する（失敗してもその日は
    再試行しないため）。処理済み位置は learn_skills が
    成功・候補なしのときだけ進める。適用しても会話への
    投稿はしない（フィードにだけ残す）。実行したら、活動の
    記録と使用量へ残す（巡回分だけ。provider_of は使用量に
    載せる頭脳名を引く関数）。
    """
    now = now or datetime.now()
    if not learn_enabled():
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
    result = learn_skills(data_dir, user_data_dir, model, now=now)
    if result.reason != "no_new_turns":
        # 材料があることを確認してから呼んでいるため通常は
        # 起きないが、モデルを呼んでいない実行は記録しない
        _record_background_run(user_data_dir, result, model,
                               provider_of, now)
    return result
