"""定期実行の棚卸し（フリート監査）。

見張りと定期ジョブという「定期実行」の定義と履歴を突き合わせ、
見直しをおすすめするものに印を付けて、直し案と一緒に知らせる
仕組み。能動発信の巡回（proactive.py）の裏の仕事として、月1回
まで行う。設計は goals の fleet-audit-design-2026-11-07.md
（取り込み第4弾、第1段＝0.89.0、第2段＝0.90.0）。

長期記憶の棚卸し（memory_audit）が「記憶の中身の古さ」を対象に
するのに対し、こちらは「定期実行の定義と履歴」が対象である。
性質の違いとして、こちらは自動では一切変えない（検出と提示
だけ）。定期実行の要否は対象の意味（変化が無いことが正常な
見張り、同じ結果が正常なジョブ）を利用者しか判断できないため
であり、変更は利用者が会話で決める。直し案の実行手段として、
見張りの間隔の変更（set_watch_interval）と定期ジョブの予定の
変更（set_job_schedule）の道具を合わせて足してある。

検出する印（第1段。いずれも保存済みの記録の突き合わせだけで
判定し、対象の確認は新たに実行しない。モデルも使わない）:
- watch_error_streak: 見張りの確認の失敗が続いている
  （error_count が 3 以上、かつ first_error_at から 24 時間以上）
- watch_no_change: 確認が続いているのに長く変化のない見張り
  （前回変化、無ければ作成時刻から 30 日以上。確認の継続は
  last_checked_at が「間隔の 2 倍＋1 時間」以内に新しいことで
  見る。直近の確認が失敗している見張りは、失敗続きの印の担当
  としてこちらには含めない）
- watch_duplicate_target: 同じ kind＋target の見張りの重なり
- job_time_clash: 定期ジョブの実行時刻の重なり（daily 同士の
  時刻、weekly 同士の曜日＋時刻。interval は対象外）
- queue_stale: 未配信のまま滞った知らせ（最古の出来事から
  3 日以上）
- job_same_result: いつも同じ結果のジョブ（第2段。同じ成功
  結果が 4 回以上続いている。空の結果は印7の担当として除く）
- job_empty_result: いつも空の結果のジョブ（第2段。成功結果
  が空のまま 2 回以上続いている。空の結果は投稿本文のハッシュ
  が常に同じになるため、専用の項目は足さず、空のハッシュと
  same_result_streak の組み合わせで数える）
- job_error_streak: 実行の失敗が続くジョブ（第2段。失敗の
  連続が 3 回以上。時刻条件は使わず回数だけ。失敗そのものは
  従来どおりその場で知らせる決まりで、こちらは束ねて繰り返し
  のパターンとして見せる追加分）

加えて「印ではない提示」として、止めたままの見張り・ジョブの
一覧と、有効な見張り・ジョブの総数や届け方の分布などの集計を、
報告（フィード）へ添える。

頻度と状態:
- 月1回まで。状態は利用者の置き場の fleet_audit_state.json に
  持つ（memory_audit_state.json と同じ流儀）: last_audit_on
  （前回実施した日。当日の年月と比べ、月が変わった最初の巡回
  で実施する）、reported（印の指紋の記録）、briefing（当日の
  朝のまとめへ渡す短い材料。まとめが投稿されたら取り下げる）
- 見張りもジョブも 1 件も無い利用者では、実施として記録しない
  （材料が無い日を試行として数えない、既存の作法）
- 棚卸し全体は環境変数 KUMA_FLEET_AUDIT=0 で無効化できる

重複の抑制:
- 印ごとに指紋（その印を付けた時点の対象の状態）を持ち、前回
  出したときと指紋が同じ印は次回は出さない。指紋が変わった
  （悪化した）場合は改めて出す。印の条件から外れた対象の記録
  は、次回の棚卸しで掃除する

出し先:
- フィードに詳細を 1 件残す（種別 proactive、件名は
  「定期実行の棚卸し」）。朝のまとめには短い材料（件数と印の
  見出し）を渡し、詳細はフィードへ誘導する。会話への直接投稿
  はしない。印が無い月は、何も出さない

文面には、対象の名前・種別・間隔・予定・根拠の数値と日付まで
だけを含める。見張りの対象そのもの（URL・検索語・ファイル
パス）や、ジョブの指示文は写さない。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .feed import KIND_PROACTIVE, append_feed_entry
from .jobs import EMPTY_RESULT_HASH, Job, JobBoard, schedule_description
from .notification_triage import QueueEvent, pending_events
from .watches import DELIVERY_LABELS, KIND_LABELS, Watch, WatchBoard

STATE_FILENAME = "fleet_audit_state.json"

# 無効化の環境変数（"0" で無効、それ以外・未設定は有効）。
# memory_audit の KUMA_MEMORY_AUDIT と同じ流儀で、モジュール
# 側で呼ぶ時点に読む
AUDIT_ENV = "KUMA_FLEET_AUDIT"

# フィードへ残す報告の件名
FEED_TITLE = "定期実行の棚卸し"

# 印の種類
MARK_WATCH_ERROR_STREAK = "watch_error_streak"
MARK_WATCH_NO_CHANGE = "watch_no_change"
MARK_WATCH_DUPLICATE_TARGET = "watch_duplicate_target"
MARK_JOB_TIME_CLASH = "job_time_clash"
MARK_QUEUE_STALE = "queue_stale"
MARK_JOB_SAME_RESULT = "job_same_result"
MARK_JOB_EMPTY_RESULT = "job_empty_result"
MARK_JOB_ERROR_STREAK = "job_error_streak"

# 印1（失敗続き）: 連続回数の下限と、連続が始まってからの時間
ERROR_STREAK_MIN_COUNT = 3
ERROR_STREAK_MIN_HOURS = 24
# 印2（変化なし）: 前回変化からの日数の下限と、悪化の段階
# （指紋に使う。30 日ごとに 1 段階とし、上限は 3＝90 日以上）
NO_CHANGE_MIN_DAYS = 30
NO_CHANGE_STAGE_DAYS = 30
NO_CHANGE_MAX_STAGE = 3
# 印5（未配信の滞留）: 最古の出来事からの日数の下限
QUEUE_STALE_MIN_DAYS = 3
# 印6（同じ結果）: 同じ成功結果が続いている回数の下限
# （streak 3 = 同じ結果が 4 回以上）
SAME_RESULT_MIN_STREAK = 3
# 印7（空の結果）: 空が続いている回数の下限（streak 1 = 空が
# 2 回以上。streak は「前回と同じ結果が続いた回数」なので、
# 回数そのものは streak + 1）
EMPTY_RESULT_MIN_STREAK = 1
# 印8（失敗続き）: 失敗の連続回数の下限（時刻条件は使わない）
JOB_ERROR_STREAK_MIN_COUNT = 3


def audit_enabled() -> bool:
    """棚卸しが有効かどうか（KUMA_FLEET_AUDIT=0 で無効化）。"""
    return os.environ.get(AUDIT_ENV, "") != "0"


# ---------------------------------------------------------- 状態

def _load_state(user_data_dir: Path) -> dict:
    """fleet_audit_state.json を読む。無い・壊れていたら空の辞書。"""
    try:
        raw = json.loads(
            (user_data_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_state(user_data_dir: Path, state: dict) -> None:
    """fleet_audit_state.json を書き直す（他の項目は保持する）。"""
    user_data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False)
    (user_data_dir / STATE_FILENAME).write_text(text + "\n", encoding="utf-8")


def _reported_map(state: dict) -> dict[str, dict]:
    """状態から「印の記録」を取り出す（壊れた分は除く）。

    キーは「印の種類＋対象の識別子」、値は出した日（on）と
    指紋（fingerprint）の辞書。
    """
    raw = state.get("reported")
    if not isinstance(raw, dict):
        return {}
    reported: dict[str, dict] = {}
    for key, value in raw.items():
        if (isinstance(key, str) and isinstance(value, dict)
                and isinstance(value.get("fingerprint"), str)):
            reported[key] = {
                "on": str(value.get("on") or ""),
                "fingerprint": value["fingerprint"],
            }
    return reported


# ---------------------------------------------------------- 時刻

def _parse_time(raw: object) -> datetime | None:
    """ISO 形式の時刻文字列を読む。読めなければ None。

    オフセット付きはサーバーのローカル時刻（naive）へそろえる
    （jobs.py の期限判定と同じ取り決め）。
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def _date_text(moment: datetime) -> str:
    """文面用の日付（例: 11月5日）。"""
    return f"{moment.month}月{moment.day}日"


# ---------------------------------------------------------- 印

@dataclass
class FleetMark:
    """印 1 件（内部の表現。そのまま利用者へは出さない）。

    key は状態ファイルの reported に使う「印の種類＋対象の
    識別子」。text はフィードの詳細文、headline は朝のまとめ
    用の短い見出しで、どちらも検出の時点で組み立てておく。
    """
    kind: str
    source_kind: str
    source_id: str
    source_title: str
    key: str
    detail: dict
    fingerprint: str
    suggestion: str
    text: str
    headline: str


def _detect_watch_error_streak(watches: list[Watch],
                               now: datetime) -> list[FleetMark]:
    """印1: 見張りの確認の失敗が続いている。"""
    marks: list[FleetMark] = []
    for watch in watches:
        if not watch.enabled or watch.error_count < ERROR_STREAK_MIN_COUNT:
            continue
        first = _parse_time(watch.first_error_at)
        if first is None:
            continue
        if now - first < timedelta(hours=ERROR_STREAK_MIN_HOURS):
            continue
        marks.append(FleetMark(
            kind=MARK_WATCH_ERROR_STREAK,
            source_kind="watch",
            source_id=watch.id,
            source_title=watch.title,
            key=f"{MARK_WATCH_ERROR_STREAK}:{watch.id}",
            detail={"error_count": watch.error_count,
                    "first_error_at": watch.first_error_at},
            fingerprint=str(watch.error_count),
            suggestion="inspect_or_stop",
            text=(f"見張り「{watch.title}」は、確認に "
                  f"{watch.error_count} 回続けて失敗しています"
                  f"（{_date_text(first)}から）。対象が移転・廃止"
                  "された可能性があります。対象を確認するか、"
                  "止めるか、消すかできます。"),
            headline=f"見張り「{watch.title}」の確認が続けて失敗しています",
        ))
    return marks


def _detect_watch_no_change(watches: list[Watch],
                            now: datetime) -> list[FleetMark]:
    """印2: 確認が続いているのに長く変化のない見張り。"""
    marks: list[FleetMark] = []
    for watch in watches:
        if not watch.enabled:
            continue
        if watch.last_error:
            # 直近の確認が失敗している見張りは、失敗続きの印
            # （印1）の担当。「確認が続いている」とは言えない
            continue
        checked = _parse_time(watch.last_checked_at)
        if checked is None:
            continue
        fresh_window = timedelta(hours=watch.interval_hours * 2 + 1)
        if now - checked > fresh_window:
            continue  # 確認自体が止まっている（この印の対象外）
        reference = (_parse_time(watch.last_changed_at)
                     or _parse_time(watch.created_at))
        if reference is None:
            continue
        days = (now - reference).days
        if days < NO_CHANGE_MIN_DAYS:
            continue
        ref_text = watch.last_changed_at or watch.created_at
        stage = min(days // NO_CHANGE_STAGE_DAYS, NO_CHANGE_MAX_STAGE)
        marks.append(FleetMark(
            kind=MARK_WATCH_NO_CHANGE,
            source_kind="watch",
            source_id=watch.id,
            source_title=watch.title,
            key=f"{MARK_WATCH_NO_CHANGE}:{watch.id}",
            detail={"days": days,
                    "reference_at": ref_text},
            fingerprint=f"{ref_text}|{stage}",
            suggestion="widen_or_stop",
            text=(f"見張り「{watch.title}」は、{days} 日間変化が"
                  "ありません（確認は続いています）。確認の間隔を"
                  "広げるか、止めるか、消すかできます。"),
            headline=f"見張り「{watch.title}」に {days} 日間変化がありません",
        ))
    return marks


def _detect_watch_duplicate_target(
        watches: list[Watch]) -> list[FleetMark]:
    """印3: 同じ対象の見張りの重なり。"""
    groups: dict[tuple[str, str], list[Watch]] = {}
    for watch in watches:
        if watch.enabled:
            groups.setdefault((watch.kind, watch.target), []).append(watch)
    marks: list[FleetMark] = []
    for (kind, target), members in groups.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda w: w.id)
        names = "、".join(f"「{w.title}」" for w in members)
        label = KIND_LABELS.get(kind, kind)
        same_shape = (len({w.interval_hours for w in members}) == 1
                      and len({w.delivery for w in members}) == 1)
        text = (f"見張り{names}は、同じ{label}の対象を見張って"
                f"います（{len(members)} 件）。変化があると重なって"
                "知らせるので、片方を消すことを検討できます。")
        if same_shape:
            text += "間隔も届け方も同じです。"
        marks.append(FleetMark(
            kind=MARK_WATCH_DUPLICATE_TARGET,
            source_kind="watch",
            source_id=",".join(w.id for w in members),
            source_title=members[0].title,
            key=f"{MARK_WATCH_DUPLICATE_TARGET}:{kind}:{target}",
            detail={"count": len(members),
                    "ids": [w.id for w in members],
                    "same_shape": same_shape},
            fingerprint=",".join(w.id for w in members),
            suggestion="remove_duplicate",
            text=text,
            headline=f"同じ対象の見張りが {len(members)} 件重なっています",
        ))
    return marks


def _detect_job_time_clash(jobs: list[Job]) -> list[FleetMark]:
    """印4: 定期ジョブの実行時刻の重なり（interval は対象外）。"""
    groups: dict[tuple, list[Job]] = {}
    for job in jobs:
        if not job.enabled or not isinstance(job.schedule, dict):
            continue
        kind = job.schedule.get("kind")
        if kind == "daily":
            group_key = ("daily", job.schedule.get("time"))
        elif kind == "weekly":
            group_key = ("weekly", job.schedule.get("weekday"),
                         job.schedule.get("time"))
        else:
            continue  # interval は時刻に固定されないため判定できない
        groups.setdefault(group_key, []).append(job)
    marks: list[FleetMark] = []
    for group_key, members in groups.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda j: j.id)
        names = "、".join(f"「{j.title}」" for j in members)
        desc = schedule_description(members[0].schedule)
        same_prompt = len({j.prompt for j in members}) == 1
        text = (f"定期ジョブ{names}が、どちらも{desc}の実行です。"
                "結果の投稿が重なるので、どちらかの時刻をずらすか、"
                "片方を消すかできます。")
        if same_prompt:
            text += ("予定も指示文も同じで、重複して登録されている"
                     "可能性があります。")
        marks.append(FleetMark(
            kind=MARK_JOB_TIME_CLASH,
            source_kind="job",
            source_id=",".join(j.id for j in members),
            source_title=members[0].title,
            key=(f"{MARK_JOB_TIME_CLASH}:"
                 + ":".join(str(part) for part in group_key)),
            detail={"count": len(members),
                    "ids": [j.id for j in members],
                    "same_prompt": same_prompt},
            fingerprint=",".join(j.id for j in members),
            suggestion="shift_schedule",
            text=text,
            headline=f"定期ジョブ {len(members)} 件の実行時刻が重なっています",
        ))
    return marks


def _detect_queue_stale(pending: list[QueueEvent],
                        now: datetime) -> list[FleetMark]:
    """印5: 未配信のまま滞った知らせ。"""
    dated: list[tuple[datetime, QueueEvent]] = []
    for event in pending:
        moment = (_parse_time(event.occurred_at)
                  or _parse_time(event.recorded_at))
        if moment is not None:
            dated.append((moment, event))
    if not dated:
        return []
    oldest_at, oldest = min(dated, key=lambda pair: pair[0])
    days = (now - oldest_at).days
    if days < QUEUE_STALE_MIN_DAYS:
        return []
    return [FleetMark(
        kind=MARK_QUEUE_STALE,
        source_kind="queue",
        source_id="queue",
        source_title="朝へ寄せた知らせ",
        key=f"{MARK_QUEUE_STALE}:queue",
        detail={"count": len(pending),
                "oldest_at": oldest.occurred_at or oldest.recorded_at,
                "days": days},
        fingerprint=oldest.id,
        suggestion="wait_for_briefing",
        text=(f"朝へ寄せた知らせが {len(pending)} 件、まだ届いて"
              f"いません（いちばん古いものは {_date_text(oldest_at)} "
              f"から {days} 日経っています）。次の朝のまとめで"
              "届く予定です。"),
        headline=f"届いていない知らせが {len(pending)} 件あります",
    )]


def _detect_job_same_result(jobs: list[Job]) -> list[FleetMark]:
    """印6: いつも同じ結果のジョブ。

    同じ結果であること自体は異常ではなく、印は「止めるべき
    もの」ではなく「届け方を見直す候補」として出す。空の結果
    の連続は印7の担当なので、ここでは対象にしない。
    """
    marks: list[FleetMark] = []
    for job in jobs:
        if not job.enabled:
            continue
        if job.same_result_streak < SAME_RESULT_MIN_STREAK:
            continue
        if (not job.last_result_hash
                or job.last_result_hash == EMPTY_RESULT_HASH):
            continue
        count = job.same_result_streak + 1
        marks.append(FleetMark(
            kind=MARK_JOB_SAME_RESULT,
            source_kind="job",
            source_id=job.id,
            source_title=job.title,
            key=f"{MARK_JOB_SAME_RESULT}:{job.id}",
            detail={"same_result_streak": job.same_result_streak,
                    "count": count},
            fingerprint=str(job.same_result_streak),
            suggestion="digest_or_revise",
            text=(f"定期ジョブ「{job.title}」は、同じ結果が "
                  f"{count} 回続いています。その都度届くのが"
                  "負担であれば、届け方を朝のまとめに寄せるか、"
                  "止めるか、指示文を見直すかできます。"),
            headline=(f"定期ジョブ「{job.title}」の結果が "
                      f"{count} 回同じです"),
        ))
    return marks


def _detect_job_empty_result(jobs: list[Job]) -> list[FleetMark]:
    """印7: いつも空の結果のジョブ。

    空の結果は投稿本文のハッシュが常に同じ（EMPTY_RESULT_HASH）
    になるため、「空が続いている回数」は same_result_streak
    そのもので数えられる（専用の材料は足していない）。
    """
    marks: list[FleetMark] = []
    for job in jobs:
        if not job.enabled:
            continue
        if job.last_result_hash != EMPTY_RESULT_HASH:
            continue
        if job.same_result_streak < EMPTY_RESULT_MIN_STREAK:
            continue
        count = job.same_result_streak + 1
        marks.append(FleetMark(
            kind=MARK_JOB_EMPTY_RESULT,
            source_kind="job",
            source_id=job.id,
            source_title=job.title,
            key=f"{MARK_JOB_EMPTY_RESULT}:{job.id}",
            detail={"empty_streak": job.same_result_streak,
                    "count": count},
            fingerprint=str(job.same_result_streak),
            suggestion="revise_prompt_or_stop",
            text=(f"定期ジョブ「{job.title}」は、結果が空のまま "
                  f"{count} 回続いています（見出しだけで届いて"
                  "います）。実行が何も返していない可能性が"
                  "あります。指示文を見直すか、止めるか、"
                  "消すかできます。"),
            headline=(f"定期ジョブ「{job.title}」の結果が "
                      f"{count} 回空です"),
        ))
    return marks


def _detect_job_error_streak(jobs: list[Job]) -> list[FleetMark]:
    """印8: 実行の失敗が続くジョブ。

    失敗はその都度その場で知らせる決まり（強制規則）がある
    ため、こちらの役割は流れて行く失敗を束ねて、繰り返しの
    パターンとして見せることにある。ジョブの予定は日次・
    週次を含むため、時刻条件は使わず回数だけで判定する。
    """
    marks: list[FleetMark] = []
    for job in jobs:
        if not job.enabled or job.error_count < JOB_ERROR_STREAK_MIN_COUNT:
            continue
        first = _parse_time(job.first_error_at)
        since = f"（{_date_text(first)}から）" if first is not None else ""
        marks.append(FleetMark(
            kind=MARK_JOB_ERROR_STREAK,
            source_kind="job",
            source_id=job.id,
            source_title=job.title,
            key=f"{MARK_JOB_ERROR_STREAK}:{job.id}",
            detail={"error_count": job.error_count,
                    "first_error_at": job.first_error_at},
            fingerprint=str(job.error_count),
            suggestion="inspect_or_stop",
            text=(f"定期ジョブ「{job.title}」は、実行に "
                  f"{job.error_count} 回続けて失敗しています"
                  f"{since}。指示文や対象を確認するか、"
                  "止めるか、消すかできます。"),
            headline=(f"定期ジョブ「{job.title}」の実行が"
                      "続けて失敗しています"),
        ))
    return marks


def detect_marks(watches: list[Watch], jobs: list[Job],
                 pending: list[QueueEvent],
                 now: datetime) -> list[FleetMark]:
    """印の一覧を検出する（抑制はしない。並びは設計の印の順）。"""
    marks: list[FleetMark] = []
    marks += _detect_watch_error_streak(watches, now)
    marks += _detect_watch_no_change(watches, now)
    marks += _detect_watch_duplicate_target(watches)
    marks += _detect_job_time_clash(jobs)
    marks += _detect_queue_stale(pending, now)
    marks += _detect_job_same_result(jobs)
    marks += _detect_job_empty_result(jobs)
    marks += _detect_job_error_streak(jobs)
    return marks


# ---------------------------------------------------------- 文面

def _summary_lines(watches: list[Watch], jobs: list[Job],
                   pending: list[QueueEvent]) -> list[str]:
    """印ではない提示（止めたままの一覧と集計）の行を作る。"""
    lines: list[str] = []
    disabled_watches = [w for w in watches if not w.enabled]
    disabled_jobs = [j for j in jobs if not j.enabled]
    if disabled_watches or disabled_jobs:
        lines.append("止めたままのもの:")
        lines += [f"・見張り「{w.title}」" for w in disabled_watches]
        lines += [f"・定期ジョブ「{j.title}」" for j in disabled_jobs]
        lines.append("")
    enabled_watches = [w for w in watches if w.enabled]
    enabled_jobs = [j for j in jobs if j.enabled]
    lines.append(
        f"いまの定期実行: 見張り {len(watches)} 件"
        f"（有効 {len(enabled_watches)} 件・"
        f"止めている {len(disabled_watches)} 件）、"
        f"定期ジョブ {len(jobs)} 件"
        f"（有効 {len(enabled_jobs)} 件・"
        f"止めている {len(disabled_jobs)} 件）")
    distribution = {name: 0 for name in DELIVERY_LABELS}
    for item in [*enabled_watches, *enabled_jobs]:
        if item.delivery in distribution:
            distribution[item.delivery] += 1
    lines.append(
        "届け方の内訳: "
        + "、".join(f"{DELIVERY_LABELS[name]} {count} 件"
                    for name, count in distribution.items()))
    lines.append(
        f"朝へ寄せて未配信の知らせ: {len(pending)} 件")
    return lines


def _build_feed_text(marks: list[FleetMark], watches: list[Watch],
                     jobs: list[Job], pending: list[QueueEvent]) -> str:
    """フィードへ残す報告の本文（詳細＋印ではない提示）。"""
    lines = [f"見直しをおすすめするものが {len(marks)} 件あります。", ""]
    lines += [f"・{mark.text}" for mark in marks]
    lines.append("")
    lines += _summary_lines(watches, jobs, pending)
    return "\n".join(lines)


def _build_briefing_text(marks: list[FleetMark]) -> str:
    """朝のまとめへ渡す短い材料（件数と印の見出しだけ）。"""
    lines = [f"定期実行の棚卸しで、見直しをおすすめするものが "
             f"{len(marks)} 件あります。"]
    lines += [f"・{mark.headline}" for mark in marks]
    lines.append("詳細はフィードの「定期実行の棚卸し」で確認できます。")
    return "\n".join(lines)


# ---------------------------------------------------------- 棚卸しの実行

@dataclass
class FleetAuditResult:
    """棚卸し 1 回分の結果（今回出した印と文面）。

    印が無かった・全て抑制された場合は marks が空で、文面も
    空になる（フィードにも何も残していない）。
    """
    marks: list[FleetMark] = field(default_factory=list)
    feed_text: str = ""
    briefing_text: str = ""


def audit_fleet(user_data_dir: Path,
                now: datetime | None = None) -> FleetAuditResult | None:
    """巡回用の棚卸し。条件を満たさなければ None（何もしない）。

    条件: 棚卸しが有効（KUMA_FLEET_AUDIT=0 でない）、今月まだ
    実施していない（月が変わった最初の巡回で実施する）、見張り
    か定期ジョブが 1 件以上ある。実施したら、まず実施日を記録
    する（途中で失敗しても、その月はやり直さない）。検出した
    印のうち、指紋が前回と違うもの（新しい・悪化したもの）だけ
    をフィードに残し、朝のまとめ用の短い材料を状態に残す。
    定義は一切変更しない。
    """
    now = now or datetime.now()
    if not audit_enabled():
        return None
    state = _load_state(user_data_dir)
    last = _parse_time(state.get("last_audit_on"))
    if last is not None and (last.year, last.month) == (now.year, now.month):
        return None  # 今月は実施済み
    watches = WatchBoard(user_data_dir).list()
    jobs = JobBoard(user_data_dir).list()
    if not watches and not jobs:
        return None  # 対象が無い（実施として記録しない）
    today = now.date().isoformat()
    state["last_audit_on"] = today
    _save_state(user_data_dir, state)  # 実施日を先に記録する

    pending = pending_events(user_data_dir)
    marks = detect_marks(watches, jobs, pending, now)
    reported = _reported_map(state)
    new_reported: dict[str, dict] = {}
    to_report: list[FleetMark] = []
    for mark in marks:
        prev = reported.get(mark.key)
        if prev is not None and prev["fingerprint"] == mark.fingerprint:
            # 指紋が同じ印は繰り返さない（出した日は初回のまま）
            new_reported[mark.key] = prev
        else:
            to_report.append(mark)
            new_reported[mark.key] = {
                "on": today, "fingerprint": mark.fingerprint}
    # 条件から外れた印の記録は掃除する（new_reported に残った
    # 分だけが現在の印）
    state["reported"] = new_reported

    result = FleetAuditResult()
    if to_report:
        result.marks = to_report
        result.feed_text = _build_feed_text(
            to_report, watches, jobs, pending)
        append_feed_entry(user_data_dir, KIND_PROACTIVE,
                          result.feed_text, title=FEED_TITLE, now=now)
        result.briefing_text = _build_briefing_text(to_report)
        state["briefing"] = {"on": today, "text": result.briefing_text}
    _save_state(user_data_dir, state)
    return result


def pending_fleet_text(user_data_dir: Path,
                       now: datetime | None = None) -> str | None:
    """今日の朝のまとめへ渡す棚卸しの材料（未だ渡していないもの）。

    棚卸しが当日分として状態に残した材料だけを対象にする
    （棚卸しを無効化した後などに、古い材料が朝のまとめへ
    混ざらないように）。長期記憶の棚卸しの確認候補
    （memory_audit.pending_confirm）と同じ流儀。
    """
    now = now or datetime.now()
    state = _load_state(user_data_dir)
    briefing = state.get("briefing")
    if not isinstance(briefing, dict):
        return None
    if briefing.get("on") != now.date().isoformat():
        return None
    text = briefing.get("text")
    if not isinstance(text, str) or not text:
        return None
    return text


def mark_fleet_briefing_used(user_data_dir: Path,
                             now: datetime | None = None) -> None:
    """棚卸しの材料を「朝のまとめで触れた」として取り下げる。

    朝のまとめとして投稿された後にだけ呼ぶ（日報に倒した場合は
    候補を残す、という確認候補の流儀とは違い、棚卸しの材料は
    当日分だけが対象で、日報には混ぜない決まりのため、取り下げ
    の条件は「まとめとして投稿されたこと」だけにする）。
    """
    state = _load_state(user_data_dir)
    if "briefing" in state:
        state.pop("briefing")
        _save_state(user_data_dir, state)
