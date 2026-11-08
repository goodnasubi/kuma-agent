"""長期記憶（MEMORY.md）と会話履歴（JSONL）の読み書き。"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .memory_meta import record_memory_items
from .tasks import TaskBoard

DEFAULT_SESSION_ID = "main"
_MAX_SESSION_ID_LEN = 64

DEFAULT_TENANT = "default"
TENANTS_DIRNAME = "tenants"

DEFAULT_USER = "default"
USERS_DIRNAME = "users"


def _normalize_name(raw: str | None, fallback: str) -> str:
    """名前をファイル名に安全な形へ正規化する（共通処理）。"""
    if not raw:
        return fallback
    chars = []
    for ch in str(raw).strip():
        chars.append(ch if (ch.isalnum() or ch in "-_") else "-")
    name = "".join(chars).strip("-.")[:_MAX_SESSION_ID_LEN].strip("-.")
    return name or fallback


def normalize_session_id(raw: str | None) -> str:
    """セッション ID をファイル名に安全な形へ正規化する。

    英数字（日本語の文字も含む）と "-" "_" はそのまま残し、パス区切りや
    空白などファイル名に使えない文字は "-" に置き換える。空になった
    場合は既定の "main" にする。
    """
    return _normalize_name(raw, DEFAULT_SESSION_ID)


def normalize_tenant_name(raw: str | None) -> str:
    """テナント名をファイル名に安全な形へ正規化する。

    セッション ID と同じ正規化を通し、空になった場合は既定の
    "default" にする。
    """
    return _normalize_name(raw, DEFAULT_TENANT)


def tenant_dir(data_dir: Path, tenant: str | None) -> Path:
    """テナントの実効データ置き場を返す。

    既定テナント（"default"）は後方互換のため data_dir をそのまま
    使い、それ以外は data_dir / "tenants" / <テナント名> を使う。
    """
    name = normalize_tenant_name(tenant)
    if name == DEFAULT_TENANT:
        return data_dir
    return data_dir / TENANTS_DIRNAME / name


def normalize_user_name(raw: str | None) -> str:
    """ユーザー名をファイル名に安全な形へ正規化する。

    テナント名と同じ正規化を通し、空になった場合は既定の
    "default" にする。
    """
    return _normalize_name(raw, DEFAULT_USER)


def user_dir(tenant_data_dir: Path, user: str | None) -> Path:
    """テナントの置き場の中での、ユーザーの実効データ置き場を返す。

    既定ユーザー（"default"）は後方互換のためテナントの置き場を
    そのまま使い、それ以外は
    <テナント置き場> / "users" / <ユーザー名> を使う。
    """
    name = normalize_user_name(user)
    if name == DEFAULT_USER:
        return tenant_data_dir
    return tenant_data_dir / USERS_DIRNAME / name


def list_user_names(tenant_data_dir: Path) -> list[str]:
    """ユーザー名の一覧（既定ユーザー＋保存済みユーザーのディレクトリ名）。"""
    names = [DEFAULT_USER]
    root = tenant_data_dir / USERS_DIRNAME
    if root.is_dir():
        names += sorted(p.name for p in root.iterdir() if p.is_dir())
    return names


def list_tenant_names(data_dir: Path) -> list[str]:
    """テナント名の一覧（既定テナント＋保存済みテナントのディレクトリ名）。"""
    names = [DEFAULT_TENANT]
    root = data_dir / TENANTS_DIRNAME
    if root.is_dir():
        names += sorted(p.name for p in root.iterdir() if p.is_dir())
    return names


def list_session_ids(data_dir: Path) -> list[str]:
    """保存済みセッションの ID 一覧（conversations/*.jsonl のファイル名）。"""
    conv = data_dir / "conversations"
    if not conv.is_dir():
        return []
    return sorted(p.stem for p in conv.glob("*.jsonl"))


def delete_session_record(user_data_dir: Path, session_id: str) -> bool:
    """セッション（スレッド）の記録ファイルだけを削除する。

    対象は conversations/<session_id>.jsonl のみで、用事の一覧
    （tasks.json）や長期記憶（MEMORY.md）には触れない。記録が
    存在して消せたら True、無かった場合や消せなかった場合は
    False を返す（例外にはしない）。
    """
    path = (user_data_dir / "conversations"
            / f"{normalize_session_id(session_id)}.jsonl")
    try:
        path.unlink()
    except OSError:  # 無い・消せない場合は「消えなかった」として扱う
        return False
    return True


def iter_user_dirs(data_dir: Path,
                   tenant: str | None = None) -> Iterator[Path]:
    """テナント×ユーザーの実効データ置き場を列挙する。

    既定テナント×既定ユーザーの置き場（data_dir そのもの）から、
    保存済みのテナント・ユーザーの置き場までを返す。tenant を指定
    した場合はそのテナントの置き場（配下の全ユーザー）だけを返す。
    """
    tenants = ([normalize_tenant_name(tenant)] if tenant is not None
               else list_tenant_names(data_dir))
    for name in tenants:
        tdir = tenant_dir(data_dir, name)
        for user in list_user_names(tdir):
            yield user_dir(tdir, user)


RETENTION_FILENAME = "retention.json"


def load_retention_overrides(data_dir: Path) -> dict[str, int]:
    """テナントごとの保持期間の上書き（retention.json）を読み込む。

    データ置き場ルートの retention.json に
    {"tenants": {"<テナント名>": <日数>}} と書くと、そのテナント
    だけ保持期間を変えられる（0 でそのテナントだけ無期限）。
    テナント名は通常の正規化を通して照合する。値が負数や数値で
    ない項目は無視し、ファイルが無い・壊れている場合も例外に
    せず、空（＝上書きなし）として返す。
    """
    try:
        raw = json.loads(
            (data_dir / RETENTION_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("tenants"), dict):
        return {}
    overrides: dict[str, int] = {}
    for name, days in raw["tenants"].items():
        # bool は int の仲間だが日数としては意味を持たないので除く
        if isinstance(days, bool) or not isinstance(days, int) or days < 0:
            continue
        overrides[normalize_tenant_name(name)] = days
    return overrides


def retention_days_for(data_dir: Path, tenant: str | None,
                       default_days: int) -> int:
    """テナントの実効保持期間を解決する。

    retention.json に指定があればそれを使い、無ければ全体の
    既定（default_days）を返す。
    """
    overrides = load_retention_overrides(data_dir)
    return overrides.get(normalize_tenant_name(tenant), default_days)


def save_retention_override(data_dir: Path, tenant: str | None,
                            days: int | None) -> None:
    """テナントごとの保持期間の上書きを retention.json に保存する。

    days に日数を渡すとそのテナントの指定として書き、None を渡すと
    そのテナントの指定を取り除く（全体の既定に戻る）。他のテナントの
    指定はそのまま残す。既存ファイルが壊れている場合は、引き継げる
    指定が無いため、この変更だけを持つ内容として書き直す。
    """
    name = normalize_tenant_name(tenant)
    try:
        raw = json.loads(
            (data_dir / RETENTION_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    tenants = raw.get("tenants")
    if not isinstance(tenants, dict):
        tenants = {}
    # 既存のキーも正規化して引き継ぐ（読み込み側の照合にそろえる）
    merged: dict[str, object] = {
        normalize_tenant_name(key): value for key, value in tenants.items()}
    if days is None:
        merged.pop(name, None)
    else:
        merged[name] = days
    raw["tenants"] = merged
    data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(raw, ensure_ascii=False, indent=2)
    (data_dir / RETENTION_FILENAME).write_text(text + "\n", encoding="utf-8")


def iter_tasks_files(data_dir: Path) -> list[Path]:
    """掃除対象の用事ファイルを列挙する。

    現在の用事の一覧はユーザー置き場の tasks.json に置くが、移行前の
    部屋ごとの用事ファイル（tasks/*.json）が残っている場合も、保持期限の
    掃除では対象に含めるため、両方を返す。無いものは含めない。
    """
    files: list[Path] = []
    if (data_dir / "tasks.json").is_file():
        files.append(data_dir / "tasks.json")
    tasks_dir = data_dir / "tasks"
    if tasks_dir.is_dir():
        files.extend(sorted(tasks_dir.glob("*.json")))
    return files


@dataclass
class CleanupResult:
    """クリーンアップで削除した件数の内訳。"""
    sessions_removed: int = 0
    tasks_removed: int = 0

    @property
    def total(self) -> int:
        return self.sessions_removed + self.tasks_removed


def cleanup_expired(data_dir: Path, retention_days: int,
                    now: datetime | None = None,
                    dry_run: bool = False,
                    tenant: str | None = None,
                    override_days: int | None = None) -> CleanupResult:
    """保持期限を過ぎた記録をテナント×ユーザー置き場から掃除する。

    対象は次の 2 つだけで、長期記憶（MEMORY.md）と人格ファイルは
    消さない:
    - 部屋の記録ファイル（conversations/*.jsonl）のうち、最終更新が
      期限より古いもの（ファイルごと削除）
    - 用事の一覧（現在の tasks.json と、残っていれば移行前の tasks/*.json）
      のうち、完了（done）してから更新が期限より古い用事（レコードだけ
      除去し、ファイルは残す）

    tenant を指定した場合はそのテナントの置き場（配下の全ユーザー）
    だけを走査し、他のテナントには一切触れない。存在しないテナント
    名では対象 0 件になる。未指定なら全テナントが対象。

    保持期間はテナントごとに解決する: データ置き場ルートの
    retention.json に指定があるテナントはその期間を使い、無い
    テナントは retention_days（全体の既定）を使う。override_days を
    指定した場合は、その場限りの上書きとして全テナントにその日数を
    適用し、retention.json より優先する。解決した期間が 0 以下なら、
    そのテナントは無期限として何もしない。

    置き場が無かったり読めなかったりしても例外で落とさず、その分を
    飛ばす。dry_run では削除せず、対象の件数だけを数えて返す。
    """
    result = CleanupResult()
    now = now or datetime.now()
    overrides = ({} if override_days is not None
                 else load_retention_overrides(data_dir))
    tenants = ([normalize_tenant_name(tenant)] if tenant is not None
               else list_tenant_names(data_dir))
    for name in tenants:
        days = (override_days if override_days is not None
                else overrides.get(name, retention_days))
        if days <= 0:
            continue  # このテナントは無期限
        cutoff = now - timedelta(days=days)
        cutoff_ts = cutoff.timestamp()
        for udir in iter_user_dirs(data_dir, name):
            conv = udir / "conversations"
            if conv.is_dir():
                for path in sorted(conv.glob("*.jsonl")):
                    try:
                        if path.stat().st_mtime >= cutoff_ts:
                            continue
                        if not dry_run:
                            path.unlink()
                        result.sessions_removed += 1
                    except OSError:
                        continue  # 読めない・消せないファイルは飛ばす
            for board_file in iter_tasks_files(udir):
                try:
                    # 掃除では移行（本体への取り込みと旧ファイル削除）を
                    # 起こさず、各ファイルをそのまま個別に処理する
                    board = TaskBoard(udir, board_file, migrate_legacy=False)
                    result.tasks_removed += board.prune_done_before(
                        cutoff, dry_run=dry_run)
                except Exception:
                    continue  # 壊れた用事ファイルなどは飛ばす
    return result


@dataclass
class MemorySegment:
    """長期記憶（MEMORY.md）を項目単位に割った 1 断片。

    text は項目の本文（続き行を含む場合は複数行）、line は
    MEMORY.md での開始行（1 始まり）で、出どころの表示に使う。
    """
    text: str
    line: int


# 項目の始まりとみなす行: 見出し（#）と箇条書き（- * + / 番号つき）
_SEGMENT_START_RE = re.compile(r"^(#+\s|[-*+]\s|\d+[.)]\s)")


def split_memory_segments(text: str) -> list[MemorySegment]:
    """長期記憶の本文を項目単位の断片に割る。

    MEMORY.md の実際の書式（remember が 1 行 1 件の箇条書きで
    追記し、利用者が見出しを足すこともある）に合わせた分割:
    - 見出し行と箇条書き行が新しい項目の始まり
    - それ以外の行は直前の項目への続き行として取り込む
    - 空行は区切り（断片には含めない）

    各断片は開始行（1 始まり）を保持する。
    """
    segments: list[MemorySegment] = []
    current: list[str] = []
    current_line = 0

    def flush() -> None:
        nonlocal current
        if current:
            segments.append(MemorySegment("\n".join(current), current_line))
            current = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            flush()
            continue
        if current and _SEGMENT_START_RE.match(raw.lstrip()):
            flush()
        if not current:
            current_line = lineno
        current.append(raw)
    flush()
    return segments


# クエリの語の区切り（空白と和文・欧文の句読点、括弧類）
_QUERY_SPLIT_RE = re.compile(
    r"[\s、。，．・:：;；/／!?！？「」『』（）()\[\]【】\"'“”‘’|｜]+")


def _query_terms(query: str) -> list[str]:
    """検索クエリを語に分ける（重複は除き、順序は保つ）。"""
    terms: list[str] = []
    for term in _QUERY_SPLIT_RE.split(query.strip()):
        if term and term not in terms:
            terms.append(term)
    return terms


def search_memory_text(text: str, query: str,
                       limit: int = 5) -> list[MemorySegment]:
    """長期記憶の本文をキーワードで検索し、関連の強い断片を返す。

    クエリを空白・句読点で語に分け、各語の部分文字列一致で断片を
    点数付けする（形態素解析はしない。日本語は分かち書きされない
    ため、部分一致が前提）。点数は「一致した語の数」を主、
    「語の出現回数」を副とし、同点は行の早い順に並べる。
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    if limit < 1:
        limit = 1
    terms = _query_terms(query)
    if not terms:
        return []
    scored: list[tuple[int, int, int, MemorySegment]] = []
    for seg in split_memory_segments(text):
        matched = [t for t in terms if t in seg.text]
        if not matched:
            continue
        occurrences = sum(seg.text.count(t) for t in matched)
        scored.append((-len(matched), -occurrences, seg.line, seg))
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in scored[:limit]]


def memory_inject_text(text: str, full_limit: int) -> tuple[str, bool]:
    """プロンプト注入用に長期記憶を整え、(本文, 一部だけかどうか) を返す。

    全文が full_limit 文字以下なら (全文, False)。超える場合は
    新しい方（末尾側）の項目から、項目単位で full_limit に収まる
    分だけを取り、(部分, True) を返す。項目の途中で切ることは
    しないため、最後の 1 項目だけで制限を超える場合はその項目を
    丸ごと返す（結果として制限を超えることがある）。
    """
    if len(text) <= full_limit:
        return text, False
    picked: list[MemorySegment] = []
    total = 0
    for seg in reversed(split_memory_segments(text)):
        cost = len(seg.text) + (1 if picked else 0)  # 結合時の改行分
        if picked and total + cost > full_limit:
            break
        picked.append(seg)
        total += cost
    picked.reverse()
    return "\n".join(seg.text for seg in picked), True


# AM-1（関連する記憶の差し込み）の上限: 拾い直す件数と合計文字数
RELATED_MEMORY_MAX_ITEMS = 3
RELATED_MEMORY_MAX_CHARS = 800


def related_memory_text(text: str, query: str, full_limit: int,
                        limit: int = RELATED_MEMORY_MAX_ITEMS,
                        max_chars: int = RELATED_MEMORY_MAX_CHARS) -> str:
    """全文注入の上限で外れた項目から、発話に関連するものを拾い直す。

    記憶が full_limit 以下で全文が載る場合は、差し込む余地が無い
    ため空文字列を返す。超えている場合は、memory_inject_text が
    載せた項目を除いた残りの項目を対象に、発話文（query）で
    search_memory_text の点数付けをして、上位から最大 limit 件・
    合計 max_chars 文字までを返す。上限を超える項目は載せない。
    関連が無ければ空文字列。拾い直しはキーワードの部分一致頼みで、
    言い換えは拾えない（設計書 AM-1 の方式 (a)）。
    """
    injected, partial = memory_inject_text(text, full_limit)
    if not partial:
        return ""
    # 一括載せで載った項目（本文の多重集合）を差し引いた残りが対象。
    # 載った項目は再分割で同じ本文になるため、本文で照合できる
    loaded = [seg.text for seg in split_memory_segments(injected)]
    remaining: list[MemorySegment] = []
    for seg in split_memory_segments(text):
        if seg.text in loaded:
            loaded.remove(seg.text)
        else:
            remaining.append(seg)
    if not remaining:
        return ""
    remainder = "\n".join(seg.text for seg in remaining)
    hits = search_memory_text(remainder, query, limit=len(remaining))
    picked: list[str] = []
    total = 0
    for hit in hits:
        if len(picked) >= limit:
            break
        cost = len(hit.text) + (1 if picked else 0)  # 結合時の改行分
        if total + cost > max_chars:
            continue  # 上限を超える項目は載せない（次の候補は見る）
        picked.append(hit.text)
        total += cost
    return "\n".join(picked)


# 発話に付けられるリアクションの絵文字（画面と保存の両方でこの範囲に限る）
# 👎 は「次のアクション」の提案を見送る合図にも使う（0.92.0 で追加）
REACTION_EMOJIS: tuple[str, ...] = ("👍", "👎", "❤️", "😂", "😮", "🎉", "🙏")

# 「次のアクション」の提案（0.92.0）: 頭脳が返事の末尾に付ける専用
# マーカーと、提案の状態・合図に使う絵文字
NEXT_ACTION_MARKER = re.compile(r"\[\[next:\s*(?P<text>.*?)\s*\]\]",
                                re.DOTALL)
PROPOSAL_STATES: tuple[str, ...] = ("pending", "done", "dismissed")
PROPOSAL_APPROVE_EMOJI = "👍"
PROPOSAL_DISMISS_EMOJI = "👎"


def extract_next_action(content: str) -> tuple[str, str | None]:
    """返事の本文から「次のアクション」のマーカーを取り除き、提案を返す。

    マーカー（[[next: …]]）が無ければ本文はそのままで提案は None。
    提案が空のときと、取り除いた後の本文が空になるときはマーカー
    扱いしない（本文をそのまま残す）。提案は前後の空白を除いた
    自然文で返す。
    """
    match = NEXT_ACTION_MARKER.search(content)
    if match is None:
        return content, None
    text = match.group("text").strip()
    body = (content[:match.start()] + content[match.end():]).strip()
    if not text or not body:
        return content, None
    return body, text


def proposal_instruction(text: str) -> str:
    """👍 で承認された提案を実行させるための利用者発話の文面。"""
    return ("👍 リアクションで承認。提案された次のアクションを"
            f"実行してください: {text}")


class Memory:
    def __init__(self, data_dir: Path, session_id: str = DEFAULT_SESSION_ID) -> None:
        self.data_dir = data_dir
        self.session_id = normalize_session_id(session_id)
        self.memory_file = data_dir / "MEMORY.md"
        self.conversations_dir = data_dir / "conversations"

    @property
    def conversation_file(self) -> Path:
        """このセッションの会話記録ファイル。"""
        return self.conversations_dir / f"{self.session_id}.jsonl"

    @property
    def reactions_file(self) -> Path:
        """このセッションのリアクションの保存ファイル。

        会話記録（JSONL）とは別に置く。キーは記録での位置
        （load_turns が返す順の 0 始まり）で、値は絵文字の並び。
        """
        return self.conversations_dir / f"{self.session_id}.reactions.json"

    @property
    def agent_reactions_file(self) -> Path:
        """クマ（相棒）自身のリアクションの保存ファイル。

        利用者のリアクションとは別のファイルに置く。利用者側の
        ファイルだけを読む・直す処理の影響を受けず、混ざらない。
        キーは記録での位置で、値は絵文字1つ。
        """
        return self.conversations_dir / f"{self.session_id}.agent-reactions.json"

    def load_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def remember(self, fact: str, until: str | None = None,
                 now: datetime | None = None) -> None:
        """長期記憶に 1 行追記し、項目の属性（AM-2）を記録する。

        属性は本文とは別の memory_meta.json に持ち、覚えた日
        （今日）と、until が渡された場合だけ期限を付ける
        （memory_meta.record_memory_items）。MEMORY.md の形式
        自体は従来どおり「- 」の 1 行で変わらない。
        """
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        with self.memory_file.open("a", encoding="utf-8") as f:
            f.write(f"- {fact}\n")
        record_memory_items(self.data_dir, [(f"- {fact}", until)], now=now)

    def search(self, keyword: str) -> list[str]:
        """長期記憶からキーワードを含む行を返す（当面は単純一致）。"""
        text = self.load_long_term()
        return [line for line in text.splitlines() if keyword in line]

    def search_ranked(self, query: str,
                      limit: int = 5) -> list[MemorySegment]:
        """長期記憶をキーワードで検索し、関連の強い断片を行番号つきで返す。

        照合と並び順は search_memory_text の流儀（部分文字列一致、
        一致した語の数が多い断片が上位）。
        """
        return search_memory_text(self.load_long_term(), query, limit=limit)

    def search_conversations(self, query: str,
                             limit: int = 5) -> list[ConversationHit]:
        """このユーザーの全セッションの会話記録を横断検索する。

        対象は自分の置き場の conversations/*.jsonl すべて（現在の
        セッションに限らない）。照合と並び順は
        search_conversation_records の流儀。
        """
        return search_conversation_records(self.data_dir, query, limit=limit)

    def append_turn(self, role: str, content: str,
                    quote: str | None = None,
                    proactive: bool = False) -> int:
        """会話履歴に 1 発話を追記する（セッションごとの JSONL）。

        quote は引用返信の引用元の本文。無い発話ではキー自体を
        付けない（旧来の記録形式と互換に保つ）。proactive は能動発信
        （エージェントから先に送った発言）の印で、True のときだけ
        キーを付ける。読み込み側は role / content / quote 以外の
        キーを無視するので、印があっても復元は通常の発話として
        扱われる。

        戻り値は、追記したレコードの位置（load_turns が返す順の
        0 始まり）。リアクションの宛先に使う。
        """
        index = len(self.load_turns())
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now().isoformat(timespec="seconds"),
                  "session": self.session_id, "role": role, "content": content}
        if quote:
            record["quote"] = quote
        if proactive:
            record["proactive"] = True
        with self.conversation_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return index

    def load_reactions(self) -> dict[int, list[str]]:
        """このセッションのリアクションを {記録の位置: [絵文字]} で返す。

        壊れた内容や許可外の絵文字は読み飛ばす（表示側を壊さない）。
        """
        if not self.reactions_file.exists():
            return {}
        try:
            raw = json.loads(self.reactions_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        reactions: dict[int, list[str]] = {}
        if not isinstance(raw, dict):
            return reactions
        for key, value in raw.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(value, list):
                continue
            emojis = [e for e in value if e in REACTION_EMOJIS]
            if emojis:
                reactions[index] = emojis
        return reactions

    def load_agent_reactions(self) -> dict[int, str]:
        """クマのリアクションを {記録の位置: 絵文字} で返す。

        壊れた内容や許可外の絵文字は読み飛ばす（表示側を壊さない）。
        """
        if not self.agent_reactions_file.exists():
            return {}
        try:
            raw = json.loads(
                self.agent_reactions_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        reactions: dict[int, str] = {}
        if not isinstance(raw, dict):
            return reactions
        for key, value in raw.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if isinstance(value, str) and value in REACTION_EMOJIS:
                reactions[index] = value
        return reactions

    def _sync_agent_reaction(self, record_index: int,
                             current: list[str]) -> None:
        """クマのリアクションを利用者の並びに追従させる（リアクション返し）。

        クマの分は、その発話に利用者のリアクションが1つ以上ある
        間だけ存在し、内容は最後に付けられた利用者の絵文字と同じ
        ものにする。全部外れたらクマの分も消える。
        """
        agent = self.load_agent_reactions()
        if current:
            agent[record_index] = current[-1]
        else:
            agent.pop(record_index, None)
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        payload = {str(k): v for k, v in sorted(agent.items())}
        self.agent_reactions_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")

    def toggle_reaction(self, record_index: int, emoji: str) -> list[str]:
        """1 発話へのリアクションを付け外しし、その発話の現在値を返す。

        絵文字は REACTION_EMOJIS の範囲だけを受け付ける。宛先は
        user / assistant のレコードに限る。どちらも満たさなければ
        ValueError。保存はキー（位置の文字列）→絵文字の並びで、
        空になった発話のキーは残さない。あわせてクマのリアクション
        も追従させる（_sync_agent_reaction）。
        """
        if emoji not in REACTION_EMOJIS:
            raise ValueError("使えない絵文字です")
        turns = self.load_turns()
        if not 0 <= record_index < len(turns):
            raise ValueError("宛先の発話がありません")
        if turns[record_index].get("role") not in ("user", "assistant"):
            raise ValueError("宛先の発話がありません")
        reactions = self.load_reactions()
        current = reactions.get(record_index, [])
        if emoji in current:
            current = [e for e in current if e != emoji]
        else:
            current = current + [emoji]
        if current:
            reactions[record_index] = current
        else:
            reactions.pop(record_index, None)
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        payload = {str(k): v for k, v in sorted(reactions.items())}
        self.reactions_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")
        self._sync_agent_reaction(record_index, current)
        return current

    @property
    def proposals_file(self) -> Path:
        """「次のアクション」の提案の保存ファイル。

        会話記録（JSONL）とは別に置く。キーは記録での位置で、値は
        {"text": 提案の自然文, "state": 状態}。状態は pending
        （保留中）/ done（実行済み）/ dismissed（見送り）。
        """
        return self.conversations_dir / f"{self.session_id}.proposals.json"

    def load_proposals(self) -> dict[int, dict[str, str]]:
        """提案を {記録の位置: {"text": …, "state": …}} で返す。

        壊れた内容や未知の状態は読み飛ばす（表示側を壊さない）。
        """
        if not self.proposals_file.exists():
            return {}
        try:
            raw = json.loads(self.proposals_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        proposals: dict[int, dict[str, str]] = {}
        if not isinstance(raw, dict):
            return proposals
        for key, value in raw.items():
            try:
                index = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            text = value.get("text")
            state = value.get("state")
            if isinstance(text, str) and text and state in PROPOSAL_STATES:
                proposals[index] = {"text": text, "state": state}
        return proposals

    def _save_proposals(self, proposals: dict[int, dict[str, str]]) -> None:
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        payload = {str(k): v for k, v in sorted(proposals.items())}
        self.proposals_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")

    def set_proposal(self, record_index: int, text: str) -> None:
        """相棒の発話に「次のアクション」の提案を保留中で付ける。

        宛先は assistant のレコードに限る。満たさなければ
        ValueError（リアクションの宛先検証と同じ流儀）。
        """
        turns = self.load_turns()
        if not 0 <= record_index < len(turns) \
                or turns[record_index].get("role") != "assistant":
            raise ValueError("宛先の発話がありません")
        proposals = self.load_proposals()
        proposals[record_index] = {"text": text, "state": "pending"}
        self._save_proposals(proposals)

    def react_to_proposal(self, record_index: int, emoji: str,
                          turned_on: bool) -> str | None:
        """提案のある発話へのリアクションを提案の状態へ反映する。

        保留中の提案に 👍 が付いた（トグルで ON になった）ときだけ
        提案を実行済み（done）にし、実行を促す利用者発話の文面
        （proposal_instruction）を返す。👎 が付いたら見送り
        （dismissed）にする（文面は返さないので会話は進まない）。
        外す操作・保留中でない提案・提案の無い発話では何もせず
        None を返すため、実行は一回限りになる。
        """
        if not turned_on or emoji not in (PROPOSAL_APPROVE_EMOJI,
                                          PROPOSAL_DISMISS_EMOJI):
            return None
        proposals = self.load_proposals()
        proposal = proposals.get(record_index)
        if proposal is None or proposal["state"] != "pending":
            return None
        if emoji == PROPOSAL_APPROVE_EMOJI:
            proposal["state"] = "done"
            self._save_proposals(proposals)
            return proposal_instruction(proposal["text"])
        proposal["state"] = "dismissed"
        self._save_proposals(proposals)
        return None

    def append_summary(self, content: str) -> None:
        """圧縮で作った要約を記録する（role は "summary"）。"""
        self.append_turn("summary", content)

    def load_turns(self) -> list[dict]:
        """このセッションの過去の記録を古い順に読み戻す。

        壊れた行は読み飛ばす。レコードは {"role", "content", ...} の辞書で、
        role には "user" / "assistant" / "summary" が入る。
        """
        if not self.conversation_file.exists():
            return []
        records: list[dict] = []
        for line in self.conversation_file.read_text(encoding="utf-8").splitlines():
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


@dataclass
class ConversationHit:
    """会話記録の横断検索のヒット 1 件（発話単位）。

    session はセッション名（記録ファイルの名前）、ts は記録に
    残っている発話の日時（ISO 文字列。無い記録では空文字列）、
    role は記録上の話者（"user" / "assistant" / "summary" など）、
    excerpt は一致箇所の前後を切り出した本文の抜粋。
    """
    session: str
    ts: str
    role: str
    excerpt: str


# 記録上の role → 利用者に分かる話者の表記
_ROLE_LABELS: dict[str, str] = {
    "user": "利用者",
    "assistant": "クマ",
    "summary": "要約",
    "tool": "道具の結果",
}


def role_label(role: str) -> str:
    """記録上の role を利用者に分かる話者の表記にする。"""
    return _ROLE_LABELS.get(role, role)


# 抜粋の目安の長さ（これを超える発話は一致箇所の前後で切り出す）
_EXCERPT_CHARS = 200


def _conversation_excerpt(content: str, terms: list[str]) -> str:
    """発話本文を表示用の抜粋にする。

    改行や連続する空白は 1 つの空白に潰す。目安の長さ以下なら
    そのまま返し、超える場合は最初に一致した語の前後を中心に
    切り出し、切り捨てた側に「…」を付ける。
    """
    flat = " ".join(content.split())
    if len(flat) <= _EXCERPT_CHARS:
        return flat
    positions = [flat.find(t) for t in terms if t in flat]
    pos = min(positions) if positions else 0
    start = max(0, pos - _EXCERPT_CHARS // 2)
    end = min(len(flat), start + _EXCERPT_CHARS)
    if end - start < _EXCERPT_CHARS:  # 末尾に寄った分は前へ広げる
        start = max(0, end - _EXCERPT_CHARS)
    excerpt = flat[start:end]
    if start > 0:
        excerpt = "…" + excerpt
    if end < len(flat):
        excerpt += "…"
    return excerpt


def _ts_sort_value(ts: str) -> float | None:
    """ISO 日時文字列を並び替え用の時刻値にする（解釈できなければ None）。"""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def search_conversation_records(data_dir: Path, query: str,
                                limit: int = 5) -> list[ConversationHit]:
    """ユーザーの置き場の全会話記録を発話単位でキーワード検索する。

    対象は data_dir / conversations / *.jsonl のすべて（委任の子の
    記録 delegate-* も含む）。クエリの語への分割と点数付けは
    長期記憶の検索（search_memory_text）と同じ流儀で、各語の
    部分文字列一致により「一致した語の数」を主、「語の出現回数」
    を副として点数付けし、同点は新しい発話を優先する。新しさは
    記録の日時（ts）で判定し、日時が無い・解釈できない記録は
    ファイルの更新時刻で代用する（読み進めた順序も同点の
    判定に使う）。壊れた行や読めないファイルは読み飛ばし、
    検索全体は落とさない。
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    if limit < 1:
        limit = 1
    terms = _query_terms(query)
    if not terms:
        return []
    conv = data_dir / "conversations"
    if not conv.is_dir():
        return []
    paths = []
    for path in conv.glob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        paths.append((mtime, path.name, path))
    # 古いファイルから読む（読み進めた順序＝新しさの代用に使うため）
    paths.sort()
    scored: list[tuple[int, int, float, int, ConversationHit]] = []
    ordinal = 0
    for mtime, _name, path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue  # 読めないファイルは飛ばす
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # 壊れた行は飛ばす
            if not isinstance(record, dict):
                continue
            content = record.get("content")
            role = record.get("role")
            if not isinstance(content, str) or not content:
                continue
            if not isinstance(role, str) or not role:
                continue
            ordinal += 1
            matched = [t for t in terms if t in content]
            if not matched:
                continue
            occurrences = sum(content.count(t) for t in matched)
            ts = record.get("ts")
            ts_text = ts if isinstance(ts, str) else ""
            recency = _ts_sort_value(ts_text)
            if recency is None:
                recency = mtime  # 日時が無い記録はファイルの新しさで代用
            hit = ConversationHit(
                session=path.stem, ts=ts_text, role=role,
                excerpt=_conversation_excerpt(content, matched))
            scored.append((-len(matched), -occurrences, -recency,
                           -ordinal, hit))
    scored.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [item[4] for item in scored[:limit]]


def format_conversation_hit(hit: ConversationHit) -> str:
    """会話検索のヒット 1 件を表示用の 1 行にする。

    「（セッション名、日時、話者）抜粋」の形。日時が記録に無い
    発話では日時の部分を省く。
    """
    meta = hit.session
    if hit.ts:
        meta += f"、{hit.ts.replace('T', ' ')}"
    meta += f"、{role_label(hit.role)}"
    return f"（{meta}）{hit.excerpt}"
