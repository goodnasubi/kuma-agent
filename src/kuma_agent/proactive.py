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

巡回にはもう 1 つ、発言を伴わない仕事がある: 長期記憶の自動の
機械的除去（memory_tidy.auto_tidy_memory）。ユーザーの
MEMORY.md が大きい（全文注入の上限の 3 倍超）場合だけ、1 日
1 回まで重複除去を自動適用する。モデル統合は自動では行わず、
適用しても会話へ投稿はしない。実施日はユーザーの置き場の
memory_tidy_state.json に記録する。

投稿先は main の会話記録で、追記は Session が使う既存の保存経路
（Memory.append_turn）を通す。発言には能動発信の印
（proactive: true）を付けるが、読み込み側は role / content /
quote 以外のキーを無視するため、復元は通常の assistant 発言
として扱われ、記録が壊れることはない。

投稿した内容は、呼び出し側が通知コールバック（notify: テナント名・
ユーザー名・文面を受け取る）を渡していれば、それにも渡す。
Telegram への配信（telegram.build_proactive_notifier）はこの口を
使う。通知は投稿が済んだ後に行い、通知側の失敗で投稿や巡回を
落とすことはしない。
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Config, normalize_mode
from .delegate import run_delegated_task
from .jobs import Job, JobBoard, is_due
from .memory import (
    list_tenant_names,
    list_user_names,
    normalize_tenant_name,
    normalize_user_name,
    tenant_dir,
)
from .memory_tidy import auto_tidy_memory
from .notify_prefs import load_notify_prefs
from .session import Session
from .settings import proactive_enabled_for
from .tasks import Task, TaskBoard

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


def _last_posted_on(user_data_dir: Path) -> str | None:
    """最後に投稿した日（YYYY-MM-DD）。無ければ・読めなければ None。"""
    try:
        raw = json.loads(
            (user_data_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict) and isinstance(raw.get("last_posted_on"), str):
        return raw["last_posted_on"]
    return None


def _record_posted(user_data_dir: Path, day: str) -> None:
    """投稿済みの日付をユーザーの置き場に記録する。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"last_posted_on": day}, ensure_ascii=False)
    (user_data_dir / STATE_FILENAME).write_text(text + "\n", encoding="utf-8")


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

# 通知コールバックの型: (テナント名, ユーザー名, 投稿文) を受け取る
NotifyCallback = Callable[[str, str, str], None]


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
    executor = job_executor if job_executor is not None else run_delegated_task
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
        posted.append(text)
        _notify(notify, cfg.tenant, cfg.user, text)
    return posted


def check_and_post(cfg: Config, now: datetime | None = None,
                   session_provider: Callable[[], Session] | None = None,
                   job_executor: Callable[[Session, str], str] | None = None,
                   notify: NotifyCallback | None = None,
                   ) -> str | None:
    """1 テナント×1 ユーザー分をチェックし、投稿したら文面を返す。

    cfg は対象のテナント・ユーザーを指した Config。投稿の条件を
    満たさなければ何もせず None を返す（モジュール冒頭の条件を
    参照）。定期ジョブ・リマインダー・日報のうち投稿したものが
    複数ある場合は、文面を改行でつないで返す。session_provider は
    既存の Session を引くための関数で、投稿すると決まったときだけ
    呼ばれる。無ければ一時的に Session を作って追記し、破棄する。
    job_executor は定期ジョブの実行関数の差し替え（テスト用）で、
    既定は委任の仕組み（run_due_jobs を参照）。notify があれば、
    投稿した文面をその都度通知コールバックへも渡す（Telegram への
    配信など。渡すのはこの cfg のテナント・ユーザー名と文面）。

    定期ジョブはモードを問わず実行する。用事のリマインダーと日報
    は従来どおり相棒モード（ledger）だけが対象で、スレッドモード
    ではジョブの分だけを返す。
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

    # 定期ジョブは用事とは独立の仕組みで、モードも静かな時間帯も
    # 問わず、期限が来ていれば先に実行して結果を投稿する
    posted.extend(run_due_jobs(cfg, now, get_session,
                               job_executor=job_executor, notify=notify))

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
        posted.append(text)
        _notify(notify, cfg.tenant, cfg.user, text)

    # 日報（1 日最大 1 回）。静かな時間帯の間は出さず、投稿済みの
    # 記録も消費しない（時間帯が明けてからその日の分を出す）。
    # 全件ミュートされた場合は言うべきことが無いので出さない
    today = now.date().isoformat()
    if (_last_posted_on(user_data_dir) != today and visible_tasks
            and not prefs.in_quiet_hours(now)):
        text = build_message(visible_tasks, now)
        get_session().post_proactive_message(text)
        _record_posted(user_data_dir, today)
        posted.append(text)
        _notify(notify, cfg.tenant, cfg.user, text)

    if not posted:
        return None  # 言うべきことが無い
    return "\n".join(posted)


def run_sweep(cfg: Config, tenant: str | None = None,
              user: str | None = None, now: datetime | None = None,
              session_getter: Callable[[str, str], Session | None] | None = None,
              job_executor: Callable[[Session, str], str] | None = None,
              notify: NotifyCallback | None = None,
              ) -> list[tuple[str, str, str]]:
    """テナント×ユーザーを巡回して能動発信をチェックする。

    tenant / user を指定すると範囲をそのテナント（配下の全ユーザー）
    や 1 ユーザーだけに絞る。列挙は既存の仕組み（default テナント＋
    tenants/ 配下、default ユーザー＋users/ 配下）を使う。投稿した
    (テナント名, ユーザー名, 文面) の一覧を返す。

    session_getter は (テナント名, ユーザー名) から既存の Session を
    引くための関数で、投稿するときだけ呼ばれる（Web サーバーが保持
    している Session を使うため）。無ければ投稿のたびに一時 Session
    を作る。job_executor は定期ジョブの実行関数の差し替え
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
                                  job_executor=job_executor, notify=notify)
            if text is not None:
                posted.append((tenant_name, user_name, text))
    return posted
