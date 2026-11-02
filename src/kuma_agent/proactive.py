"""能動発信（エージェントから先に発言する仕組み）。

定期チェックが走り、「言うべきことがあるときだけ」main の会話に
エージェントの発言として投稿する。言うべきことが無ければ何もしない。

チェック条件（ユーザーごと）:
1. そのテナントの設定で能動発信が OFF なら何もしない
   （settings.json の proactive_enabled、既定は ON）
2. 本日まだ投稿していなければ、進行中の用事（tasks.json の open な
   用事）がある場合に、その一覧を伝える発言を 1 回だけ投稿する。
   用事が無ければ投稿しない
3. 7 日以上 open のままの用事がある場合は、その注意喚起も同じ
   発言に含める（別発言を増やさない）

つまり投稿は 1 ユーザーにつき 1 日最大 1 回で、投稿済みの日付は
ユーザーの置き場に proactive_state.json として記録する。

対象は相棒モード（ledger）の用事だけである。スレッドモード
（wall）では用事の一覧を使わないため、結果として発言しない。

投稿先は main の会話記録で、追記は Session が使う既存の保存経路
（Memory.append_turn）を通す。発言には能動発信の印
（proactive: true）を付けるが、読み込み側は role / content /
quote 以外のキーを無視するため、復元は通常の assistant 発言
として扱われ、記録が壊れることはない。
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Config, normalize_mode
from .memory import (
    list_tenant_names,
    list_user_names,
    normalize_tenant_name,
    normalize_user_name,
    tenant_dir,
)
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


class _UnusedModel:
    """能動発信の一時 Session 専用のモデル。

    発言文はモデルなしで組み立てるため、Session を一時的に作る
    ときの受け皿としてだけ使う（chat が呼ばれたら前提違いなので
    例外にする）。
    """

    def chat(self, system_prompt, messages, tools):
        raise RuntimeError("能動発信ではモデルを使いません")


def check_and_post(cfg: Config, now: datetime | None = None,
                   session_provider: Callable[[], Session] | None = None,
                   ) -> str | None:
    """1 テナント×1 ユーザー分をチェックし、投稿したら文面を返す。

    cfg は対象のテナント・ユーザーを指した Config。投稿の条件を
    満たさなければ何もせず None を返す（モジュール冒頭の条件を
    参照）。session_provider は既存の Session を引くための関数で、
    投稿すると決まったときだけ呼ばれる。無ければ一時的に Session を
    作って追記し、破棄する。
    """
    now = now or datetime.now()
    if normalize_mode(cfg.mode) != "ledger":
        return None  # スレッドモードでは用事を使わないため発言しない
    if not proactive_enabled_for(cfg.data_dir, cfg.tenant):
        return None
    user_data_dir = cfg.user_data_dir
    today = now.date().isoformat()
    if _last_posted_on(user_data_dir) == today:
        return None  # 本日は投稿済み（1 日最大 1 回）
    open_tasks = TaskBoard(user_data_dir).open_tasks()
    if not open_tasks:
        return None  # 言うべきことが無い
    text = build_message(open_tasks, now)
    session = session_provider() if session_provider is not None else None
    if session is None:
        session = Session(cfg, _UnusedModel())
    session.post_proactive_message(text)
    _record_posted(user_data_dir, today)
    return text


def run_sweep(cfg: Config, tenant: str | None = None,
              user: str | None = None, now: datetime | None = None,
              session_getter: Callable[[str, str], Session | None] | None = None,
              ) -> list[tuple[str, str, str]]:
    """テナント×ユーザーを巡回して能動発信をチェックする。

    tenant / user を指定すると範囲をそのテナント（配下の全ユーザー）
    や 1 ユーザーだけに絞る。列挙は既存の仕組み（default テナント＋
    tenants/ 配下、default ユーザー＋users/ 配下）を使う。投稿した
    (テナント名, ユーザー名, 文面) の一覧を返す。

    session_getter は (テナント名, ユーザー名) から既存の Session を
    引くための関数で、投稿するときだけ呼ばれる（Web サーバーが保持
    している Session を使うため）。無ければ投稿のたびに一時 Session
    を作る。
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
            text = check_and_post(scoped, now=now, session_provider=provider)
            if text is not None:
                posted.append((tenant_name, user_name, text))
    return posted
