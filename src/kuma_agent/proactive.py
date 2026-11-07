"""能動発信（エージェントから先に発言する仕組み）。

定期チェックが走り、「言うべきことがあるときだけ」main の会話に
エージェントの発言として投稿する。言うべきことが無ければ何もしない。

チェック条件（ユーザーごと）:
1. そのテナントの設定で能動発信が OFF なら何もしない
   （settings.json の proactive_enabled、既定は ON）。
   リマインダーもこの設定に従う
2. リマインダー: open な用事で通知時刻（remind_at）が来ていて
   未通知のものがあれば、その発言を投稿し、通知済み（reminded_at）
   を記録する。利用者が時刻を指定した依頼なので、後述の
   「1 日 1 回まで」の上限（日報）とは独立に発言する。
   同じ巡回で両方立つ場合はリマインダーを先に投稿する
3. 日報: 本日まだ投稿していなければ、進行中の用事（tasks.json の
   open な用事）がある場合に、その一覧を伝える発言を 1 回だけ
   投稿する。用事が無ければ投稿しない
4. 7 日以上 open のままの用事がある場合は、その注意喚起も日報の
   発言に含める（別発言を増やさない）

加えて、利用者の「知らせる・知らせない」の好み（notify_prefs.json、
notify_prefs.py）が効く:
- ミュート: 題名＋メモにミュート語を含む用事は日報にも
  リマインダーにも出さない。ミュートされたリマインダーは通知
  済みの印を付けずに保留し、ミュートを外せば後で通知される
- 静かな時間帯: その間は日報だけを出さない（投稿済みの記録も
  消費しないため、時間帯が明ければその日の日報を出せる）。
  リマインダーは時間帯でも出す

つまり日報の投稿は 1 ユーザーにつき 1 日最大 1 回で、投稿済みの
日付はユーザーの置き場に proactive_state.json として記録する。

日報には拡張として「朝のまとめ」がある（環境変数
KUMA_BRIEFING_TIME、既定 07:30、"off" で無効）:
- 有効な間は、まとめの時刻より前に日報を投稿しない。時刻を
  過ぎた最初の巡回で、委任の子セッションに「今日の予定（カレン
  ダーの道具があれば）と進行中の用事」をまとめさせ、その文章を
  日報の代わりに投稿して、その日の声かけ枠（1 日 1 回）を使う
- 子を動かすのは、進行中の用事があるか、そのセッションの道具に
  カレンダーらしい道具（名前に list-events を含むもの）がある
  ときだけ。どちらも無ければ何もしない
- 子が「なし」と答えたら投稿せず、「今日は確認済み」
  （briefing_checked_on）だけを proactive_state.json に記録して、
  その日子を二度と動かさない。子が失敗したら、用事がある場合に
  従来の日報の文面に倒す（声かけ自体は欠かさない）
- モデルなしの一時 Session（_UnusedModel、CLI の proactive
  など）では子を動かさない。この場合は従来の日報の組み立て
  （モデルなしでできる）だけを行う

対象は相棒モード（ledger）の用事だけである。スレッドモード
（wall）では用事の一覧を使わないため、結果として発言しない。

定期ジョブ（jobs.json）は巡回のもう 1 つの仕事で、用事とは扱いが
違う（詳細は jobs.py と DESIGN.md の「定期ジョブ」節）:
- テナントの設定で能動発信が OFF ならジョブも実行しない（上記 1 と
  同じ条件）。モードは問わず動作し、静かな時間帯も適用しない
  （時刻指定のリマインダーと同じ扱い）
- 期限の来たジョブは委任の仕組み（delegate の子セッション）で
  実行し、結果を main の会話へ「定期ジョブ「{title}」の結果です。」
  の見出し付きで投稿する。実行したら成功・失敗にかかわらず
  last_run_at を更新し、失敗時は短い失敗の知らせを投稿する

巡回には発言を伴わない仕事も 2 つある: 長期記憶の自動の
機械的除去（memory_tidy.auto_tidy_memory）と、長期記憶の
自動抽出（memory_extract.auto_extract_memory）。機械的除去は
ユーザーの MEMORY.md が大きい（全文注入の上限の 3 倍超）
場合だけ、1 日 1 回まで重複除去を自動適用する。モデル統合は
自動では行わず、適用しても会話へ投稿はしない。実施日は
ユーザーの置き場の memory_tidy_state.json に記録する。
自動抽出は、前回の抽出以降に増えた会話があれば 1 日 1 回まで、
実モデルに「長期記憶に残すべきこと」を抽出させて MEMORY.md
へ足す（KUMA_MEMORY_EXTRACT=0 で無効化）。モデルなしの一時
Session の巡回では実行せず、試行の記録も残さない。こちらも
会話へは投稿しない。

加えて長期記憶の棚卸し（memory_audit.audit_memory、AM-3）も
発言を伴わない仕事として 1 日 1 回まで行う: 期限を過ぎた
一時的な項目を控えを取って自動で外してフィードに残し、
変わり得る古い事実は「確認したい記憶」の候補として状態に
残す。候補は朝のまとめの材料にだけ渡し（日報には混ぜない）、
まとめが投稿されたら「尋ねた」印を付ける。モデルは使わない
ため、モデルなしの巡回でも棚卸し自体は行う（確認候補は
モデルなしでは日の目を見ず、翌日以降に回る）。
棚卸し全体は KUMA_MEMORY_AUDIT=0 で無効化できる。

目標の進み具合の観測（goal_progress.observe_goal_progress、
AF-5）も発言を伴わない仕事として 1 日 1 回まで行う: 進行中の
目標について紐づいた用事の完了割合を前回の観測と比べ、変わって
いたらフィード（種別 task）にだけ残す。初回の観測は基準を取る
だけで記録しない。モデルは使わない。

朝のまとめの材料には、main の会話の作業の手順の残り（AF-3）も
足す: 終わっていない手順のうち進行中・停滞のもの（最大 3 件）を、
状態と「その状態になってからの日数」（Step.updated_at から数える）
つきで子の指示文に含める。手順の残りだけでも子を動かす材料に
なるが、日報への倒し込みには混ぜない（声かけは朝のまとめ経由
だけ。日報は用事の知らせと役割を分ける）。

期限の近い記憶（AM-4）も朝のまとめの材料に足す: 属性 until
（AM-2）を持ち、期限まで 3 日以内の項目（最大 3 件）を
「近づいている期限のある記憶」として子の指示文に含める。
進行中の用事と強く重なる項目は、用事側の知らせに任せて材料
から外す（同じことを二重に知らせない）。期限を過ぎた項目は
棚卸し（AM-3）の担当で、こちらには含めない。こちらも日報
への倒し込みには混ぜない。

今日の予定（Google カレンダー連携、0.77.0）も朝のまとめの
材料に足す: 巡回が google_calendar.py を直接使って主
カレンダーの今日の予定を読み、「今日の予定（Google
カレンダー）」の節として子の指示文の先頭に含める（モデルに
道具を呼ばせる方式は正にしない）。用事と強く重なる予定は
独立した節から外し、用事の行へ予定時刻を注記として寄せる
（同じ出来事を二重に触れない）。連携が未設定・未認可・
失敗のときは材料に入れず、エラー文も渡さず、従来どおり
静かに動く。こちらも日報への倒し込みには混ぜない。
0.78.0 からは、利用者が選択したカレンダー群（選択が無け
れば主カレンダーだけ）の予定を時刻順に統合して 1 つの節に
する。一部のカレンダーの取得が失敗しても、取れた分だけで
節を作る（全部失敗のときだけ従来どおり材料なし）。

投稿先は main の会話記録で、追記は Session が使う既存の保存経路
（Memory.append_turn）を通す。発言には能動発信の印
（proactive: true）を付けるが、読み込み側は role / content /
quote 以外のキーを無視するため、復元は通常の assistant 発言
として扱われ、記録が壊れることはない。

投稿が確定した発言は、フィード（feed.py）にも脇の記録として
残す。種別は、朝のまとめの子の文章そのものが文面になった場合
だけ briefing で、それ以外（日報・リマインダー・定期ジョブの
結果や失敗の知らせ）は proactive。会話への投稿の振る舞い自体は
変えず、フィードは読み物として後から並べるための記録である。

投稿した内容は、呼び出し側が通知コールバック（notify: テナント名・
ユーザー名・文面を受け取る）を渡していれば、それにも渡す。
Telegram への配信（telegram.build_proactive_notifier）と LINE
への配信（line_api.build_proactive_notifier）はこの口を使う
（複数ある場合は combine_notifiers でまとめる）。通知は投稿が
済んだ後に行い、通知側の失敗で投稿や巡回を落とすことはしない。
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, time
from pathlib import Path
from typing import Callable

from .activity import auto_cleanup_activity
from .config import Config, normalize_mode
from .delegate import run_delegated_task


def _sweep_executor(session: Session, prompt: str) -> str:
    """巡回（朝のまとめ・定期ジョブ・見張り）用の委任の実行役。

    巡回側には失敗時の受け皿（日報への倒し込み・失敗の知らせ・
    次回の巡回での再確認）が既にあるため、委任の立て直し
    （AF-2）まで重ねると二重の立て直しになる。retry=False で
    抑えた形を巡回の既定の実行役にする。
    """
    return run_delegated_task(session, prompt, retry=False)
from .feed import KIND_BRIEFING, KIND_PROACTIVE, append_feed_entry
from .jobs import Job, JobBoard, is_due
from .llm import ChatModel
from .memory import (
    DEFAULT_SESSION_ID,
    Memory,
    _query_terms,
    list_tenant_names,
    list_user_names,
    normalize_tenant_name,
    normalize_user_name,
    split_memory_segments,
    tenant_dir,
)
from .goal_progress import observe_goal_progress
from .google_calendar import (
    CalendarEvent,
    build_client as build_google_calendar_client,
    calendar_names,
    fetch_events_for_calendars,
    format_events,
    format_time_range,
    selected_calendar_ids,
)
from .memory_audit import audit_memory, mark_confirm_asked, pending_confirm
from .memory_extract import auto_extract_memory
from .memory_meta import load_memory_meta
from .memory_tidy import _is_heading_segment, _segment_key, auto_tidy_memory
from .notify_prefs import load_notify_prefs, parse_hhmm
from .session import Session
from .settings import proactive_enabled_for
from .skill_learn import auto_learn_skills
from .tasks import Task, TaskBoard
from .watches import Watch, WatchBoard, check_watch
from .watches import is_due as watch_is_due
from .worksteps import (
    STATUS_BLOCKED as _STEP_BLOCKED,
    STATUS_IN_PROGRESS as _STEP_IN_PROGRESS,
    STATUS_LABELS as _STEP_LABELS,
    WorkSteps,
)

STATE_FILENAME = "proactive_state.json"

# 7 日以上手つかずの用事は注意喚起する
STALE_DAYS = 7
# 発言に挙げる用事の題名の上限（残りは件数だけ伝える）
MAX_TITLES = 5
# 注意喚起に挙げる用事の上限
_MAX_STALE_TITLES = 3


def _parse_time(raw: str) -> datetime | None:
    """ISO 形式の時刻文字列を読む。読めなければ None。"""
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _as_local_naive(moment: datetime) -> datetime:
    """オフセット付き時刻をサーバーのローカル時刻（naive）にそろえる。

    通知時刻（remind_at）はオフセットなしならローカル時刻として
    解釈する取り決めで、オフセット付きで入ってきた場合も現在時刻
    （naive なローカル時刻）と比較できるように変換する。
    """
    if moment.tzinfo is not None:
        return moment.astimezone().replace(tzinfo=None)
    return moment


def _load_state(user_data_dir: Path) -> dict:
    """proactive_state.json を読む。無い・壊れていたら空の辞書。"""
    try:
        raw = json.loads(
            (user_data_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(user_data_dir: Path, state: dict) -> None:
    """proactive_state.json を書き直す（他の項目は保持する）。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False)
    (user_data_dir / STATE_FILENAME).write_text(text + "\n", encoding="utf-8")


def _last_posted_on(user_data_dir: Path) -> str | None:
    """最後に投稿した日（YYYY-MM-DD）。無ければ・読めなければ None。"""
    value = _load_state(user_data_dir).get("last_posted_on")
    return value if isinstance(value, str) else None


def _record_posted(user_data_dir: Path, day: str) -> None:
    """投稿済みの日付をユーザーの置き場に記録する。"""
    state = _load_state(user_data_dir)
    state["last_posted_on"] = day
    _save_state(user_data_dir, state)


def _briefing_checked_on(user_data_dir: Path) -> str | None:
    """朝のまとめで「今日は確認済み」と記録した日。無ければ None。"""
    value = _load_state(user_data_dir).get("briefing_checked_on")
    return value if isinstance(value, str) else None


def _record_briefing_checked(user_data_dir: Path, day: str) -> None:
    """朝のまとめの子が「なし」と答えた日を記録する。

    投稿はしていないので声かけ枠（last_posted_on）は消費しないが、
    同じ日に子を何度も動かさないための目印として残す。
    """
    state = _load_state(user_data_dir)
    state["briefing_checked_on"] = day
    _save_state(user_data_dir, state)


def _open_days(task: Task, now: datetime) -> int | None:
    """用事が作られてからの日数。時刻が読めなければ None。

    「open のままの日数」は作られた時刻から数える（メモの更新では
    延びない）。作られた時刻が読めない場合は更新時刻で代用する。
    """
    created = _parse_time(task.created_at) or _parse_time(task.updated_at)
    if created is None:
        return None
    return (now - created).days


def build_message(open_tasks: list[Task], now: datetime) -> str:
    """進行中の用事から発言文を組み立てる。

    題名は最大 MAX_TITLES 件まで挙げ、残りは件数だけを伝える。
    7 日以上 open のままの用事があれば、同じ発言の末尾に注意喚起を
    含める。
    """
    lines = [f"お知らせです。進行中の用事が {len(open_tasks)} 件あります。"]
    for task in open_tasks[:MAX_TITLES]:
        lines.append(f"・{task.title}")
    if len(open_tasks) > MAX_TITLES:
        lines.append(f"ほか {len(open_tasks) - MAX_TITLES} 件あります。")
    stale = [task for task in open_tasks
             if (days := _open_days(task, now)) is not None
             and days >= STALE_DAYS]
    if stale:
        names = "、".join(f"「{task.title}」"
                          for task in stale[:_MAX_STALE_TITLES])
        rest = (f"（ほか {len(stale) - _MAX_STALE_TITLES} 件）"
                if len(stale) > _MAX_STALE_TITLES else "")
        lines.append(
            f"なお、{names}{rest}は {STALE_DAYS} 日以上そのままになっています。")
    return "\n".join(lines)


def due_reminders(open_tasks: list[Task], now: datetime) -> list[Task]:
    """通知時刻が来ていて未通知の open な用事（時刻の早い順）。

    通知時刻が読めない用事は対象から外す（壊れたデータで巡回を
    落とさないため）。
    """
    due: list[tuple[datetime, Task]] = []
    for task in open_tasks:
        if not task.remind_at or task.reminded_at:
            continue
        when = _parse_time(task.remind_at)
        if when is None:
            continue
        when = _as_local_naive(when)
        if when <= now:
            due.append((when, task))
    due.sort(key=lambda pair: pair[0])
    return [task for _when, task in due]


def build_reminder_message(due_tasks: list[Task]) -> str:
    """リマインダーの発言文を組み立てる（日報の文面に似せる）。"""
    lines = [f"お知らせです。通知の時刻になった用事が "
             f"{len(due_tasks)} 件あります。"]
    for task in due_tasks[:MAX_TITLES]:
        lines.append(f"・{task.title}")
    if len(due_tasks) > MAX_TITLES:
        lines.append(f"ほか {len(due_tasks) - MAX_TITLES} 件あります。")
    return "\n".join(lines)


class _UnusedModel:
    """能動発信の一時 Session 専用のモデル。

    発言文はモデルなしで組み立てるため、Session を一時的に作る
    ときの受け皿としてだけ使う（chat が呼ばれたら前提違いなので
    例外にする）。
    """

    def chat(self, system_prompt, messages, tools):
        raise RuntimeError("能動発信ではモデルを使いません")


# ジョブの失敗の知らせに添える理由の上限（知らせ自体は短く保つ）
_MAX_JOB_ERROR_CHARS = 200

# 朝のまとめの既定の時刻（KUMA_BRIEFING_TIME が未設定・不正な値の
# ときに使う）
DEFAULT_BRIEFING_TIME = time(7, 30)
# カレンダーらしい道具の目印。Google カレンダーの MCP サーバーの
# 予定一覧の道具（gcal__list-events）の名前に含まれる文字列で
# 判断する
_CALENDAR_TOOL_MARKER = "list-events"
_WEEKDAY_NAMES = "月火水木金土日"

# 朝のまとめの材料に挙げる、終わっていない手順の上限（AF-3）。
# 対象は進行中・停滞だけ（未着手はこれからの分、完了・中止は
# 終わった分なので、再開の声かけには含めない）
_MAX_BRIEFING_STEPS = 3
_BRIEFING_STEP_STATUSES = (_STEP_IN_PROGRESS, _STEP_BLOCKED)


def _briefing_at(cfg: Config) -> time | None:
    """朝のまとめの時刻。"off" なら None、不正な値は既定に倒す。"""
    raw = (cfg.briefing_time or "").strip().lower()
    if raw == "off":
        return None
    parsed = parse_hhmm(raw)
    return parsed if parsed is not None else DEFAULT_BRIEFING_TIME


def open_steps_text(user_data_dir: Path, now: datetime) -> str | None:
    """朝のまとめの材料にする、終わっていない手順の文面（AF-3）。

    対象は main の会話の手順（相棒モードの声かけは main の会話
    宛て）のうち、進行中・停滞のものだけ、最大 _MAX_BRIEFING_STEPS
    件。各行には状態と、その状態になってからの日数（updated_at
    から数えられる場合だけ）を添える。対象が無ければ None。
    """
    steps = WorkSteps(user_data_dir, DEFAULT_SESSION_ID).items()
    picked = [s for s in steps if s.status in _BRIEFING_STEP_STATUSES]
    if not picked:
        return None
    lines = []
    for step in picked[:_MAX_BRIEFING_STEPS]:
        line = f"・[{_STEP_LABELS[step.status]}] {step.text}"
        updated = _parse_time(step.updated_at)
        if updated is not None:
            days = (now - _as_local_naive(updated)).days
            if days >= 1:
                line += f"（{days}日この状態のまま）"
        lines.append(line)
    return "\n".join(lines)


# 朝のまとめの材料に挙げる「期限が近い」の窓（AM-4）。設計では
# 「期限が近い（例: 3 日以内）」とされ、今日からこの日数以内に
# 期限が来る項目を対象にする（期限当日を含む）
NEAR_DEADLINE_DAYS = 3
# 朝のまとめの材料に挙げる、期限の近い記憶の上限（AM-4）。
# 他の材料の上限（手順の残り 3 件）に合わせる
_MAX_BRIEFING_DEADLINES = 3


def _terms_overlap(terms: list[str], text: str) -> bool:
    """語の一覧が本文へ「強く」重なるか（AM-4 の物差し）。

    知らせを消す側の判定なので保守的にし、はっきり重なる
    場合だけ True にする:
    - 一致する語が 2 つ以上ある場合
    - 語が 1 つしか取れない場合は、その語自体が 4 文字以上
      （＝それだけで具体的な内容を持つ長さ）で、本文にそのまま
      含まれるときだけ
    """
    if not terms:
        return False
    matched = [term for term in terms if term in text]
    if len(matched) >= 2:
        return True
    return (len(terms) == 1 and len(matched) == 1
            and len(terms[0]) >= 4)


def _strongly_overlaps_task(item_text: str, task: Task) -> bool:
    """記憶の項目と用事が「同じこと」を指すほど強く重なるか（AM-4）。

    判定は記憶の検索（search_memory_text）と同じ、キーワードの
    部分文字列一致の流儀で、用事の題名・メモから取った語が項目
    本文にどれだけ含まれるかで見る（物差しは _terms_overlap）。
    言い換えや弱い重なりは拾わない（その場合は記憶側も知らせる。
    二重になる代わりに、知らせが消えすぎる事故を避ける）。
    """
    return _terms_overlap(
        _query_terms(f"{task.title} {task.memo}"), item_text)


def _event_overlaps_task(event: CalendarEvent, task: Task) -> bool:
    """予定と用事が「同じ出来事」を指すほど強く重なるか。

    重なりの物差しは _strongly_overlaps_task と同じで、予定の
    題名と用事の題名・メモの双方へかける（予定の題名は短く、
    用事の語がそのまま入るとは限らないため）。弱い重なりや
    言い換えだけの場合は寄せない（知らせが重なる代わりに、
    予定や用事を取りこぼす事故を避ける。設計 §4.3）。
    """
    if _strongly_overlaps_task(event.title, task):
        return True
    return _terms_overlap(
        _query_terms(event.title), f"{task.title} {task.memo}")


def calendar_briefing_materials(
        cfg: Config, visible_tasks: list[Task], now: datetime,
        *, client=None, tz=None,
        ) -> tuple[str | None, dict[str, str], bool]:
    """朝のまとめの材料にする、今日の予定（Google カレンダー）。

    戻り値は (予定の節の文面, 用事への注記 {用事 ID: 注記},
    取得できたが予定が 0 件だったか)。対象は利用者が選択した
    カレンダー群（選択が無ければ主カレンダーだけ、0.77.0 と
    互換）で、予定は時刻順に統合して 1 つの節にする。連携が
    未設定・未認可・（全部のカレンダーが）失敗のときは
    (None, {}, False) で、節も注記も渡さない（エラー文も渡さ
    ない。設計 §4.1・§4.4）。一部のカレンダーだけ失敗した
    場合は、取れた分だけで節を作る。用事と強く重なる予定は
    節から外し、用事の行への注記（時刻つきは
    「予定 HH:MM–HH:MM」、終日は「終日予定あり」）に寄せる。
    client はテスト用の差し替え口で、通常は cfg から組み立てる。
    """
    if client is None:
        client = build_google_calendar_client(
            cfg.user_data_dir, enabled=cfg.google_calendar_enabled,
            client_secrets_file=cfg.google_calendar_client_secrets_file)
    if client.status().state != "authorized":
        return None, {}, False
    today = now.date()
    targets = selected_calendar_ids(cfg.user_data_dir)
    names = calendar_names(client) if len(targets) > 1 else {}
    events, has_more, errors = fetch_events_for_calendars(
        client, targets, today, today, tz=tz, names=names)
    if errors and len(errors) >= len(targets):
        return None, {}, False  # 失効・通信失敗は静かに従来動作へ
    if not events:
        return None, {}, True
    remaining: list[CalendarEvent] = []
    note_parts: dict[str, list[str]] = {}
    for event in events:
        matched = next(
            (task for task in visible_tasks
             if _event_overlaps_task(event, task)), None)
        if matched is None:
            remaining.append(event)
            continue
        part = ("終日予定あり" if event.all_day
                else f"予定 {format_time_range(event)}")
        note_parts.setdefault(matched.id, []).append(part)
    task_notes = {task_id: f"（{'、'.join(parts)}）"
                  for task_id, parts in note_parts.items()}
    events_text = (format_events(remaining, today, today,
                                 has_more=has_more)
                   if remaining else None)
    return events_text, task_notes, False


def near_deadline_text(user_data_dir: Path, open_tasks: list[Task],
                       now: datetime) -> str | None:
    """朝のまとめの材料にする、期限の近い記憶の文面（AM-4）。

    属性 until（AM-2）を持ち、期限まで NEAR_DEADLINE_DAYS 日
    以内の項目を、期限の近い順に最大 _MAX_BRIEFING_DEADLINES
    件、各行に残りの日数を添えて返す。期限を過ぎた項目は
    棚卸し（AM-3）の担当なので含めない。進行中の用事と強く
    重なる項目は、用事側の知らせに任せて外す（同じことを
    二重に知らせない）。対象が無ければ None。
    """
    text = Memory(user_data_dir).load_long_term()
    if not text.strip():
        return None
    meta = load_memory_meta(user_data_dir)
    if not meta:
        return None
    today = now.date()
    picked: list[tuple[str, int, str]] = []
    for seg in split_memory_segments(text):
        if _is_heading_segment(seg.text):
            continue
        entry = meta.get(_segment_key(seg.text))
        if not entry or not entry.get("until"):
            continue
        try:
            until = datetime.strptime(entry["until"], "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (until - today).days
        if days < 0 or days > NEAR_DEADLINE_DAYS:
            continue  # 期限切れは棚卸しの担当。遠い期限はまだ知らせない
        if any(_strongly_overlaps_task(seg.text, task)
               for task in open_tasks):
            continue  # 用事として立っている分は用事側の知らせに任せる
        picked.append((entry["until"], seg.line, seg.text))
    if not picked:
        return None
    picked.sort()
    lines = []
    for until, _line, seg_text in picked[:_MAX_BRIEFING_DEADLINES]:
        body = seg_text.lstrip()
        if body.startswith("- "):
            body = body[2:]
        days = (datetime.strptime(until, "%Y-%m-%d").date() - today).days
        if days == 0:
            lines.append(f"・{body}（期限は今日まで）")
        else:
            lines.append(f"・{body}（期限まであと{days}日）")
    return "\n".join(lines)


def build_briefing_prompt(open_tasks: list[Task], now: datetime,
                          confirm_text: str | None = None,
                          steps_text: str | None = None,
                          deadlines_text: str | None = None,
                          calendar_text: str | None = None,
                          task_notes: dict[str, str] | None = None,
                          calendar_empty: bool = False) -> str:
    """朝のまとめを子へ頼む指示文を組み立てる。

    その日の日付と、進行中の用事の一覧（ミュート分は呼び出し側で
    除いてある）を含める。用事が無い場合はその旨を明記する
    （カレンダーの予定だけでもまとめられるように）。
    材料の並びは、今日の予定（Google カレンダー）→進行中の
    用事→作業の手順の残り→確認したい記憶→近づいている期限の
    ある記憶の順（設計 §4.2）。
    calendar_text（Google カレンダー連携の今日の予定）があれば、
    「今日の予定（Google カレンダー）」の節として最初に足す
    （巡回が直接集めた材料で、子は同じ範囲を取り直さない）。
    取得できたが予定が 0 件だった場合は calendar_empty で、
    その旨だけを文脈として渡す（それ自体は材料にしない）。
    task_notes は、用事と強く重なった予定を用事の行へ寄せた
    注記（{用事 ID: 注記}）で、用事の行の末尾に添える。
    confirm_text（棚卸し AM-3 の確認候補）があれば、「確認したい
    記憶」として材料に足し、まとめの中で自然に 1 件だけ尋ねる
    よう指示する。日報の組み立てには混ぜない。
    steps_text（AF-3 の手順の残り）があれば、「エージェントの
    作業の手順の残り」として材料に足し、まとめの中で自然に触れる
    よう指示する。こちらも日報の組み立てには混ぜない。
    deadlines_text（AM-4 の期限の近い記憶）があれば、「近づいて
    いる期限のある記憶」として材料に足し、まとめの中で自然に
    期限が近いことに触れるよう指示する。こちらも日報の組み立て
    には混ぜない。
    """
    date_text = (f"{now.year}年{now.month}月{now.day}日"
                 f"（{_WEEKDAY_NAMES[now.weekday()]}）")
    lines = [
        "朝のまとめを作ってください。",
        "",
        f"今日は {date_text} です。",
    ]
    if calendar_text:
        lines += [
            "",
            "今日の予定（Google カレンダー）:",
            calendar_text,
        ]
    elif calendar_empty:
        lines += [
            "",
            "Google カレンダーに今日の予定はありません。",
        ]
    lines.append("")
    if open_tasks:
        lines.append("進行中の用事:")
        for task in open_tasks:
            memo = f"（{task.memo}）" if task.memo else ""
            note = (task_notes or {}).get(task.id, "")
            lines.append(f"・{task.title}{memo}{note}")
    else:
        lines.append("進行中の用事はありません。")
    if steps_text:
        lines += [
            "",
            "エージェントの作業の手順の残り:",
            steps_text,
        ]
    if confirm_text:
        lines += [
            "",
            "確認したい記憶:",
            f"・{confirm_text}",
        ]
    if deadlines_text:
        lines += [
            "",
            "近づいている期限のある記憶:",
            deadlines_text,
        ]
    lines += [
        "",
        "やること:",
        "- 「今日の予定（Google カレンダー）」の節がある場合は、"
        "それを今日の予定として使ってください（同じ範囲の予定を"
        "道具で取り直さないこと）。節が無い場合は、カレンダーを"
        "確認できる道具（予定の一覧を取る道具）があれば、今日の"
        "予定を確認してください。",
        "- 今日の予定の節も進行中の用事も無く、確認したい記憶も"
        "作業の手順の残りも近づいている期限のある記憶も"
        "無ければ、「なし」とだけ答えてください。",
        "- どちらかあれば、朝の挨拶に続けて、今日の予定（時刻と"
        "題名）と進行中の用事を短くまとめた、利用者への知らせを"
        "書いてください（その文章がそのまま利用者へ投稿されます）。",
    ]
    if calendar_text:
        lines.append(
            "- 予定と用事は別のものとしてまとめ、同じ内容は "
            "1 回だけ触れてください。")
    if steps_text:
        lines.append(
            "- エージェントの作業の手順の残りがあれば、知らせの"
            "中で自然に、その作業が残っていることに触れてください"
            "（例:「昨日からの続きの作業が残っています」）。")
    if confirm_text:
        lines.append(
            "- 確認したい記憶があれば、知らせの中で自然に、その"
            "内容が今も同じかどうかを尋ねてください（1 件だけ。"
            "例:「そういえば、〜は今も同じですか」）。")
    if deadlines_text:
        lines.append(
            "- 近づいている期限のある記憶があれば、知らせの中で"
            "自然に、その期限が近いことに触れてください。")
    return "\n".join(lines)


def _has_calendar_tool(session: Session) -> bool:
    """そのセッションの道具にカレンダーらしい道具があるか。

    名前に list-events を含む道具（Google カレンダーの
    gcal__list-events 等）を目印にする。道具一覧が取れなければ
    「無い」として扱う（巡回を落とさないため）。
    """
    try:
        return any(_CALENDAR_TOOL_MARKER in str(schema.get("name", ""))
                   for schema in session.tools.schemas())
    except Exception:
        return False


def _daily_message(cfg: Config, now: datetime, visible_tasks: list[Task],
                   user_data_dir: Path,
                   get_session: Callable[[], Session],
                   briefing_executor: Callable[[Session, str], str] | None,
                   manual: bool = False,
                   confirm_text: str | None = None,
                   steps_text: str | None = None,
                   deadlines_text: str | None = None,
                   calendar_text: str | None = None,
                   task_notes: dict[str, str] | None = None,
                   calendar_empty: bool = False,
                   ) -> tuple[str, str] | None:
    """その日の声かけ（朝のまとめ／日報）の文面を決める。

    投稿すべき文面があれば (文面, フィードの種別) を返し、無ければ
    None を返す。種別は、朝のまとめの子の文章がそのまま文面に
    なったときだけ KIND_BRIEFING で、従来の日報の組み立てに
    倒した場合は KIND_PROACTIVE（フィードへ残すときの区別）。
    朝のまとめで子が「なし」と答えた場合は、確認済みの記録だけを
    残して None を返す（投稿と声かけ枠の消費は呼び出し側の仕事）。

    判断の順序:
    1. 朝のまとめが無効（off）なら従来の日報（用事があるときだけ）
    2. まとめの時刻より前なら、まだ何も言わない（日報も出さない）。
       ただし手動実行（manual）のときはこの制限を受けない
    3. 今日すでに子が確認済み（「なし」）なら何も言わない
    4. モデルなしの一時 Session では子を動かさず、従来の日報に
       倒す（日報の組み立てはモデルなしでできるため）
    5. 用事もカレンダーらしい道具も無ければ、子を動かす材料が
       無いので何も言わない。ただし確認したい記憶（confirm_text、
       棚卸し AM-3 の候補）や作業の手順の残り（steps_text、
       AF-3）、期限の近い記憶（deadlines_text、AM-4）、今日の
       予定（calendar_text、Google カレンダー連携。用事へ寄せた
       注記 task_notes だけの場合も含む）がある場合は、それ
       自体が材料になる。予定が 0 件だった旨（calendar_empty）
       は文脈として渡すだけで、材料にはしない
    6. 子にまとめさせ、失敗・空応答なら用事がある場合だけ従来の
       日報に倒す。「なし」なら確認済みを記録して何も言わない
       （日報に倒した場合は確認も手順の残りも期限の近い記憶も
       予定も混ぜない。候補は翌日以降に回る）
    """
    def daily_fallback() -> tuple[str, str] | None:
        """従来の日報の組み立てに倒す（種別は能動発信）。"""
        if not visible_tasks:
            return None
        return build_message(visible_tasks, now), KIND_PROACTIVE

    briefing_at = _briefing_at(cfg)
    if briefing_at is None:
        return daily_fallback()
    today = now.date().isoformat()
    if now.time() < briefing_at and not manual:
        return None
    if _briefing_checked_on(user_data_dir) == today:
        return None
    session = get_session()
    if isinstance(session.model, _UnusedModel):
        return daily_fallback()
    if not visible_tasks and not _has_calendar_tool(session) \
            and not confirm_text and not steps_text \
            and not deadlines_text and not calendar_text \
            and not task_notes:
        return None
    executor = (briefing_executor if briefing_executor is not None
                else _sweep_executor)
    try:
        result = executor(
            session, build_briefing_prompt(visible_tasks, now,
                                           confirm_text=confirm_text,
                                           steps_text=steps_text,
                                           deadlines_text=deadlines_text,
                                           calendar_text=calendar_text,
                                           task_notes=task_notes,
                                           calendar_empty=calendar_empty))
    except Exception as e:  # 子の失敗で声かけ自体は欠かさない
        result = f"error: {e}"
    if not isinstance(result, str):
        result = str(result)
    result = result.strip()
    if not result or result.startswith("error:"):
        return daily_fallback()
    if result.rstrip("。") == "なし":
        _record_briefing_checked(user_data_dir, today)
        return None
    return result, KIND_BRIEFING

# 通知コールバックの型: (テナント名, ユーザー名, 投稿文) を受け取る
NotifyCallback = Callable[[str, str, str], None]


def combine_notifiers(
        *notifiers: NotifyCallback | None) -> NotifyCallback | None:
    """複数の通知コールバックを 1 つにまとめる。

    Telegram と LINE のように配信先が複数ある場合に使う。有効な
    ものが 1 つも無ければ None（通知なし）、1 つだけならそれ自体を
    返す。まとめたコールバックは、投稿のたびに対象の全部へ順に
    渡す（個々の失敗の扱いは _notify 側と各コールバック側に任せる）。
    """
    active = [n for n in notifiers if n is not None]
    if not active:
        return None
    if len(active) == 1:
        return active[0]

    def notify(tenant: str, user: str, text: str) -> None:
        for callback in active:
            callback(tenant, user, text)

    return notify


def _notify(notify: NotifyCallback | None, tenant: str, user: str,
            text: str) -> None:
    """通知コールバックを呼ぶ（投稿が済んだ文面を外部へ渡す口）。

    通知側の失敗で投稿や巡回を落とさないよう、例外はここで飲む。
    """
    if notify is None:
        return
    try:
        notify(tenant, user, text)
    except Exception:
        pass  # 通知の失敗は巡回全体に影響させない


def build_job_result_message(job: Job, result: str) -> str:
    """ジョブの実行結果から投稿文を組み立てる（成功時）。"""
    body = result.strip()
    heading = f"定期ジョブ「{job.title}」の結果です。"
    return f"{heading}\n{body}" if body else heading


def build_job_failure_message(job: Job, result: str) -> str:
    """ジョブの失敗の知らせを組み立てる（短く、理由は切り詰める）。"""
    detail = result.removeprefix("error:").strip()
    text = f"定期ジョブ「{job.title}」の実行に失敗しました。"
    if detail:
        if len(detail) > _MAX_JOB_ERROR_CHARS:
            detail = detail[:_MAX_JOB_ERROR_CHARS] + "…"
        text += f"\n（{detail}）"
    return text


def run_due_jobs(cfg: Config, now: datetime, get_session: Callable[[], Session],
                 job_executor: Callable[[Session, str], str] | None = None,
                 notify: NotifyCallback | None = None,
                 ) -> list[str]:
    """期限の来た定期ジョブを実行し、結果を main の会話へ投稿する。

    get_session は投稿先（そのユーザーの main）の Session を引く
    ための関数で、期限の来たジョブがあるときだけ呼ばれる。実行は
    job_executor（親 Session と指示文を受けて結果を返す関数）で、
    既定は委任の仕組み（delegate.run_delegated_task: 子セッションが
    仕事をして最終応答を返す）。実行したら成功・失敗にかかわらず
    last_run_at を実行時刻に更新する。実行の例外や error の結果は
    失敗として扱い、短い失敗の知らせを投稿する。投稿した文面の
    一覧を返す。notify があれば、投稿した文面（結果・失敗の知らせ
    とも）をその都度通知コールバックへも渡す。
    """
    board = JobBoard(cfg.user_data_dir)
    due = [job for job in board.list() if is_due(job, now)]
    if not due:
        return []
    executor = job_executor if job_executor is not None else _sweep_executor
    posted: list[str] = []
    for job in due:
        session = get_session()
        if isinstance(session.model, _UnusedModel):
            # モデルなしの一時 Session（CLI の proactive など）では
            # ジョブを実行できない。失敗として消費せず、実行できる
            # 経路（Web の巡回など）の巡回に委ねる
            return posted
        try:
            result = executor(session, job.prompt)
        except Exception as e:  # 実行の失敗で巡回全体は落とさない
            result = f"error: {e}"
        if not isinstance(result, str):
            result = str(result)
        if result.startswith("error:"):
            text = build_job_failure_message(job, result)
        else:
            text = build_job_result_message(job, result)
        try:
            session.post_proactive_message(text)
        finally:
            # 投稿まで進んだら、成否にかかわらず実行済みを記録する
            board.mark_run(job.id, now)
        append_feed_entry(cfg.user_data_dir, KIND_PROACTIVE, text)
        posted.append(text)
        _notify(notify, cfg.tenant, cfg.user, text)
    return posted


def build_watch_prompt(watch: Watch, old_summary: str,
                       new_summary: str) -> str:
    """変化を知らせる文を作らせるための、委任の子への指示文。

    前回と今回の要約を材料として渡し、変化の要点だけを短く
    知らせる文を作らせる。対象そのものの取得は巡回側で済んで
    いるため、子は取得をやり直さなくてよい。
    """
    lines = [
        f"見張り「{watch.title}」の対象に変化がありました。",
        f"対象: {watch.target}",
        f"前回の内容: {old_summary or '（記録なし）'}",
        f"今回の内容: {new_summary}",
        "変化の要点を短くまとめ、利用者への知らせ文を作ってください。",
    ]
    if watch.note:
        lines.append(f"利用者からの指示: {watch.note}")
    return "\n".join(lines)


def build_watch_message(watch: Watch, result: str) -> str:
    """見張りの知らせ文を組み立てる（変化があったとき）。"""
    body = result.strip()
    heading = f"見張り「{watch.title}」で変化がありました。"
    return f"{heading}\n{body}" if body else heading


def build_watch_failure_message(watch: Watch, result: str) -> str:
    """見張りの知らせ文を作れなかったときの短い知らせ。"""
    detail = result.removeprefix("error:").strip()
    text = f"見張り「{watch.title}」で変化がありましたが、知らせ文を作れませんでした。"
    if detail:
        if len(detail) > _MAX_JOB_ERROR_CHARS:
            detail = detail[:_MAX_JOB_ERROR_CHARS] + "…"
        text += f"\n（{detail}）"
    return text


def run_due_watches(cfg: Config, now: datetime,
                    get_session: Callable[[], Session],
                    watch_executor: Callable[[Session, str], str] | None = None,
                    notify: NotifyCallback | None = None,
                    ) -> list[str]:
    """間隔の来た見張りを確認し、変化があったら知らせを投稿する。

    確認はモデルを使わない（対象のハッシュの比較だけ）。最初の
    確認は基準の記録だけで知らせない。確認に失敗したら理由を
    記録して次回に回す（知らせはしない）。変化があったとき
    だけ、知らせ文の作成を watch_executor（既定は委任の仕組み）
    へ頼み、結果を main の会話へ投稿する。投稿した文面の一覧を
    返す。モデルなしの一時 Session の経路では、変化を消費せず
    実行できる経路（Web の巡回など）に委ねる（定期ジョブと同じ）。
    """
    board = WatchBoard(cfg.user_data_dir)
    due = [w for w in board.list() if watch_is_due(w, now)]
    if not due:
        return []
    executor = watch_executor if watch_executor is not None \
        else _sweep_executor
    posted: list[str] = []
    for watch in due:
        try:
            new_hash, new_summary = check_watch(watch, Path.cwd())
        except Exception as e:  # 確認の失敗で巡回全体は落とさない
            board.mark_checked(watch.id, now, error=str(e)[:200])
            continue
        if not watch.last_hash:
            # 最初の確認は基準の記録だけ
            board.mark_checked(watch.id, now, hash=new_hash,
                               summary=new_summary)
            continue
        if new_hash == watch.last_hash:
            board.mark_checked(watch.id, now, hash=new_hash,
                               summary=new_summary)
            continue
        session = get_session()
        if isinstance(session.model, _UnusedModel):
            # モデルなしの経路では知らせ文を作れない。変化を
            # 消費せず、実行できる経路の巡回に委ねる
            return posted
        prompt = build_watch_prompt(watch, watch.last_summary, new_summary)
        try:
            result = executor(session, prompt)
        except Exception as e:  # 作成の失敗で巡回全体は落とさない
            result = f"error: {e}"
        if not isinstance(result, str):
            result = str(result)
        if result.startswith("error:"):
            text = build_watch_failure_message(watch, result)
        else:
            text = build_watch_message(watch, result)
        try:
            session.post_proactive_message(text)
        finally:
            # 投稿まで進んだら、成否にかかわらず確認済みを記録する
            board.mark_checked(watch.id, now, hash=new_hash,
                               summary=new_summary, changed=True)
        append_feed_entry(cfg.user_data_dir, KIND_PROACTIVE, text)
        posted.append(text)
        _notify(notify, cfg.tenant, cfg.user, text)
    return posted


def check_and_post(cfg: Config, now: datetime | None = None,
                   session_provider: Callable[[], Session] | None = None,
                   job_executor: Callable[[Session, str], str] | None = None,
                   watch_executor: Callable[[Session, str], str] | None = None,
                   notify: NotifyCallback | None = None,
                   briefing_executor: Callable[[Session, str], str] | None = None,
                   manual: bool = False,
                   ) -> str | None:
    """1 テナント×1 ユーザー分をチェックし、投稿したら文面を返す。

    cfg は対象のテナント・ユーザーを指した Config。投稿の条件を
    満たさなければ何もせず None を返す（モジュール冒頭の条件を
    参照）。定期ジョブ・リマインダー・日報のうち投稿したものが
    複数ある場合は、文面を改行でつないで返す。session_provider は
    既存の Session を引くための関数で、投稿すると決まったときだけ
    呼ばれる。無ければ一時的に Session を作って追記し、破棄する。
    job_executor は定期ジョブの実行関数の差し替え（テスト用）で、
    既定は委任の仕組み（run_due_jobs を参照）。watch_executor は
    見張りの知らせ文の作成関数の差し替え（テスト用）で、既定は
    同じく委任の仕組み（run_due_watches を参照）。briefing_executor は
    朝のまとめの実行関数の差し替え（テスト用）で、既定は同じく
    委任の仕組み。notify があれば、投稿した文面をその都度通知
    コールバックへも渡す（Telegram への配信など。渡すのはこの
    cfg のテナント・ユーザー名と文面）。manual が True のとき
    （CLI の手動実行）は、朝のまとめの時刻より前でも日報を出す
    （自動巡回は時刻まで待つ。手動で今すぐ出す判断を優先する）。

    定期ジョブと見張りはモードを問わず実行する。用事のリマインダー
    と日報は従来どおり相棒モード（ledger）だけが対象で、スレッド
    モードではジョブと見張りの分だけを返す。
    """
    now = now or datetime.now()
    if not proactive_enabled_for(cfg.data_dir, cfg.tenant):
        return None  # テナント OFF ならジョブ・リマインダーとも何もしない
    user_data_dir = cfg.user_data_dir

    # 長期記憶の自動の機械的除去（memory_tidy）: 記憶が大きい
    # （全文注入の上限の 3 倍超）ユーザーだけ、1 日 1 回まで重複
    # 除去を自動適用する。モデル統合は自動では行わず、適用して
    # も会話へは投稿しない。失敗しても巡回自体は続ける。テナント
    # の能動発信が OFF のときは、この巡回の一部として行わない
    try:
        auto_tidy_memory(user_data_dir, cfg.memory_full_inject_chars,
                         now=now)
    except Exception:
        pass  # 整理の失敗で巡回全体は落とさない

    # 活動の記録（activity.jsonl）の保持期限の掃除: 1 日 1 回まで、
    # 期限（activity_retention_days、既定 90 日）より古い行を落とす。
    # 会話への投稿はしない。失敗しても巡回自体は続ける
    try:
        auto_cleanup_activity(user_data_dir, cfg.activity_retention_days,
                              now=now)
    except Exception:
        pass  # 掃除の失敗で巡回全体は落とさない

    posted: list[str] = []
    session: Session | None = None

    def get_session() -> Session:
        nonlocal session
        if session is None:
            session = (session_provider()
                       if session_provider is not None else None)
            if session is None:
                session = Session(cfg, _UnusedModel())
        return session

    # 長期記憶の自動抽出（memory_extract）: 前回の抽出以降に増えた
    # 会話があれば、1 日 1 回まで実モデルに抽出させて MEMORY.md
    # へ足す。会話への投稿はしない。モデルなしの一時 Session の
    # 経路（CLI の proactive）では実行せず、試行の記録も残さない
    # （定期ジョブのモデルなしガードと同じ考え方）。モデルは、
    # 新しい材料があるときだけセッションから引く
    def extract_model() -> ChatModel | None:
        model = get_session().model
        return None if isinstance(model, _UnusedModel) else model

    try:
        auto_extract_memory(user_data_dir, extract_model, now=now)
    except Exception:
        pass  # 抽出の失敗で巡回全体は落とさない

    # 手順書の自動学習（skill_learn）: 前回の学習以降に増えた
    # 会話があれば、1 日 1 回まで実モデルに学習させて新しい
    # 手順書として保存する。会話への投稿はせず、保存したら
    # フィードにだけ残す。モデルの引き方は自動抽出と同じ
    try:
        auto_learn_skills(cfg.data_dir, user_data_dir, extract_model,
                          now=now)
    except Exception:
        pass  # 学習の失敗で巡回全体は落とさない

    # 長期記憶の棚卸し（memory_audit、AM-3）: 1 日 1 回まで、
    # 期限を過ぎた一時的な項目を控えを取って自動で外し
    # （フィードにだけ残す）、変わり得る古い事実の確認候補を
    # 状態に残す。モデルは使わないので、モデルなしの経路でも
    # 行う。確認候補はこの後の朝のまとめにだけ渡す
    try:
        audit_memory(user_data_dir, now=now)
    except Exception:
        pass  # 棚卸しの失敗で巡回全体は落とさない

    # 目標の進み具合の観測（goal_progress、AF-5）: 1 日 1 回まで、
    # 進行中の目標の完了割合が前回の観測から変わっていたら
    # フィードにだけ残す。会話への投稿はせず、モデルも使わない
    # ので、モデルなしの経路でも行う。モードを問わず行う
    # （目標は利用者ごとの置き場で、両モード共通のため）
    try:
        observe_goal_progress(user_data_dir, now=now)
    except Exception:
        pass  # 観測の失敗で巡回全体は落とさない

    # 定期ジョブは用事とは独立の仕組みで、モードも静かな時間帯も
    # 問わず、期限が来ていれば先に実行して結果を投稿する
    posted.extend(run_due_jobs(cfg, now, get_session,
                               job_executor=job_executor, notify=notify))

    # 見張りも定期ジョブと同じくモードを問わず、間隔が来ていれば
    # 確認し、変化があった分だけ知らせを投稿する
    posted.extend(run_due_watches(cfg, now, get_session,
                                  watch_executor=watch_executor,
                                  notify=notify))

    if normalize_mode(cfg.mode) != "ledger":
        # スレッドモードでは用事を使わないため、残りは発言しない
        return "\n".join(posted) if posted else None
    board = TaskBoard(user_data_dir)
    open_tasks = board.open_tasks()
    # 「知らせる・知らせない」の好み: ミュートされた用事は日報にも
    # リマインダーにも出さない（日報側は絞った一覧で組み立てる）
    prefs = load_notify_prefs(user_data_dir)
    visible_tasks = [task for task in open_tasks
                     if not prefs.is_muted(task.title, task.memo)]

    # リマインダーは利用者が時刻を指定した依頼なので、日報の
    # 上限とは独立に、先に投稿する。投稿できたら通知済みを記録
    # して二度と通知しない。ミュートされた用事は印を付けずに
    # 保留する（ミュートを外せば後の巡回で通知される）ため、
    # visible_tasks から拾う
    due = due_reminders(visible_tasks, now)
    if due:
        text = build_reminder_message(due)
        get_session().post_proactive_message(text)
        for task in due:
            board.mark_reminded(task.id)
        append_feed_entry(user_data_dir, KIND_PROACTIVE, text)
        posted.append(text)
        _notify(notify, cfg.tenant, cfg.user, text)

    # 日報／朝のまとめ（1 日最大 1 回）。静かな時間帯の間は出さず、
    # 投稿済みの記録も消費しない（時間帯が明けてからその日の分を
    # 出す）。文面の決め方（朝のまとめの時刻・子の実行・失敗時の
    # 日報への倒し方）は _daily_message が担う。全件ミュートされた
    # 場合は、用事が材料として渡らない形で判断される
    today = now.date().isoformat()
    if (_last_posted_on(user_data_dir) != today
            and not prefs.in_quiet_hours(now)):
        # 棚卸し（AM-3）の確認候補: 朝のまとめの材料にだけ渡す。
        # 読み取りの失敗で日報自体は落とさない
        confirm: tuple[str, str] | None = None
        try:
            confirm = pending_confirm(user_data_dir, now=now)
        except Exception:
            confirm = None
        # 作業の手順の残り（AF-3）: こちらも朝のまとめの材料に
        # だけ渡す。読み取りの失敗で日報自体は落とさない
        steps_text: str | None = None
        try:
            steps_text = open_steps_text(user_data_dir, now)
        except Exception:
            steps_text = None
        # 期限の近い記憶（AM-4）: こちらも朝のまとめの材料に
        # だけ渡す。用事と強く重なる項目は材料の組み立て側で
        # 外してある。読み取りの失敗で日報自体は落とさない
        deadlines_text: str | None = None
        try:
            deadlines_text = near_deadline_text(
                user_data_dir, visible_tasks, now)
        except Exception:
            deadlines_text = None
        # 今日の予定（Google カレンダー連携）: こちらも朝の
        # まとめの材料にだけ足す。未設定・未認可・失敗では
        # 材料に入らず従来どおり静かに動く。用事と強く重なる
        # 予定は、材料の組み立て側で用事の行への注記に寄せて
        # ある。読み取りの失敗で日報自体は落とさない
        calendar_text: str | None = None
        task_notes: dict[str, str] = {}
        calendar_empty = False
        try:
            calendar_text, task_notes, calendar_empty = \
                calendar_briefing_materials(cfg, visible_tasks, now)
        except Exception:
            calendar_text, task_notes, calendar_empty = None, {}, False
        daily = _daily_message(cfg, now, visible_tasks, user_data_dir,
                               get_session, briefing_executor, manual=manual,
                               confirm_text=confirm[1] if confirm else None,
                               steps_text=steps_text,
                               deadlines_text=deadlines_text,
                               calendar_text=calendar_text,
                               task_notes=task_notes,
                               calendar_empty=calendar_empty)
        if daily is not None:
            text, feed_kind = daily
            get_session().post_proactive_message(text)
            _record_posted(user_data_dir, today)
            append_feed_entry(user_data_dir, feed_kind, text)
            posted.append(text)
            _notify(notify, cfg.tenant, cfg.user, text)
            # 朝のまとめとして投稿された場合だけ「尋ねた」印を
            # 付ける（日報に倒した場合は候補を翌日以降に残す）
            if confirm is not None and feed_kind == KIND_BRIEFING:
                try:
                    mark_confirm_asked(user_data_dir, confirm[0], now=now)
                except Exception:
                    pass  # 印付けの失敗で投稿や巡回は落とさない

    if not posted:
        return None  # 言うべきことが無い
    return "\n".join(posted)


def run_sweep(cfg: Config, tenant: str | None = None,
              user: str | None = None, now: datetime | None = None,
              session_getter: Callable[[str, str], Session | None] | None = None,
              job_executor: Callable[[Session, str], str] | None = None,
              notify: NotifyCallback | None = None,
              briefing_executor: Callable[[Session, str], str] | None = None,
              manual: bool = False,
              ) -> list[tuple[str, str, str]]:
    """テナント×ユーザーを巡回して能動発信をチェックする。

    tenant / user を指定すると範囲をそのテナント（配下の全ユーザー）
    や 1 ユーザーだけに絞る。列挙は既存の仕組み（default テナント＋
    tenants/ 配下、default ユーザー＋users/ 配下）を使う。投稿した
    (テナント名, ユーザー名, 文面) の一覧を返す。

    session_getter は (テナント名, ユーザー名) から既存の Session を
    引くための関数で、投稿するときだけ呼ばれる（Web サーバーが保持
    している Session を使うため）。無ければ投稿のたびに一時 Session
    を作る。job_executor / briefing_executor は実行関数の差し替え
    （check_and_post と同じ）。notify は通知コールバックで、各
    ユーザーへの投稿のたびにそのユーザー名で呼ばれる（届けるか
    どうかの判断はコールバック側の役目）。
    """
    posted: list[tuple[str, str, str]] = []
    tenant_names = ([normalize_tenant_name(tenant)] if tenant is not None
                    else list_tenant_names(cfg.data_dir))
    for tenant_name in tenant_names:
        tdir = tenant_dir(cfg.data_dir, tenant_name)
        user_names = ([normalize_user_name(user)] if user is not None
                      else list_user_names(tdir))
        for user_name in user_names:
            scoped = replace(cfg, tenant=tenant_name, user=user_name)
            provider = None
            if session_getter is not None:
                provider = (lambda getter=session_getter, t=tenant_name,
                            u=user_name: getter(t, u))
            text = check_and_post(scoped, now=now, session_provider=provider,
                                  job_executor=job_executor, notify=notify,
                                  briefing_executor=briefing_executor,
                                  manual=manual)
            if text is not None:
                posted.append((tenant_name, user_name, text))
    return posted
