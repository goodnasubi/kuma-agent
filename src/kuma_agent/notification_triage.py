"""通知の取捨（見張りの変化を朝の知らせへ寄せる置き場）。

能動発信の通知を重要度で振り分ける仕組み。見張りごとに届け方
（watches.Watch.delivery）を持ち、digest（朝のまとめに寄せる）
にした見張りの変化は、その場では会話へ投稿せず、発生時点の
材料をこの置き場へ残して次の朝の知らせ（朝のまとめ／日報）で
まとめて届ける（第1段、0.84.0）。

第2段（0.85.0）では、届け方 auto（おまかせ）を足した。auto の
見張りの変化は、内部専用の振り分け判定役（道具なし。委任の
内部の型 triage）が決められた JSON で「その場で知らせるか、
朝へ寄せるか」を判定する。判定の検証はこのモジュールの
parse_triage_verdict が担い、採用できない判定（形式不正・
例外・確信なし等）は理由コードを残して即時へ倒す。immediate
／digest を明示した見張りでは判定自体を行わない（決め手の
優先順位は decide_watch_route を参照）。

置き場は利用者の置き場の notification_queue.jsonl で、活動の
記録（activity.jsonl）やフィード（feed.jsonl）と同じ1行1件の
JSONL。同じ出来事の状態変化（配信済み化）も新しい行として
追記し、読み出し側は出来事 ID ごとの最新行を正とする。これに
より、保持と「なぜその経路になったか」の検証の履歴を同じ
置き場でまかなえる。フィードは「起きたことの記録」であり、
未配信という状態は持たないため置き場にはしない（発生時の
短い記録と、実際に届けた朝の知らせはフィードにも残る。合成は
proactive.py が担う）。

出来事1件の形（設計 §3.2）:
- id: 出来事を一意にする文字列。見張りでは
  "watch:<見張りID>:<前回確認時刻>:<新しいハッシュの先頭12字>"
  で、基準更新前に同じ変化を再検出しても同じ ID になる
- status: pending（未配信）/ delivered（朝の知らせ等で配信
  済み）/ superseded（明示的に取り消されたもの）
- recorded_at / occurred_at: 記録した時刻／発生した時刻
  （サーバーのローカル時刻、ISO 8601）
- source_kind / source_id / source_title: 発生源（第1段は
  watch だけ）と、その ID・名前
- title: 知らせの見出し相当の文
- old_summary / new_summary: 材料（見張りの前回・今回の要約、
  各最大400字）。対象の URL・検索語・ファイルパスは置き場へ
  重ねて保存しない（発生源の ID から元の見張りを引ける）
- source_note: 知らせるときの指示（見張りの note）
- decision / decision_by / decision_reason: どの経路に、誰が
  （規則／設定／既定／モデル）、なぜ決めたか。理由は短い
  コードで残し、材料の全文は写さない
- delivered_on / delivered_via: 配信した日と経路
  （briefing=朝のまとめ、daily=日報の組み立て）

順序の原則（設計 §3.3）:
- 寄せる場合は、置き場へ保存できたことを確認してから見張りの
  基準を更新する。保存できなければ呼び出し側が即時へ倒す
- 配信済みにするのは、朝の知らせとして実際に投稿された後
  だけ。投稿前に配信済みにしない（重複配信と取りこぼしでは
  重複の方が害が小さいため）
- pending の出来事は経過日数だけでは消さない（自動の有効
  期限は付けない）。delivered / superseded は検証のため
  保持し、90日を目安に巡回で1日1回まで掃除する
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .watches import Watch

QUEUE_FILE = "notification_queue.jsonl"
CLEANUP_STATE_FILE = "notification_cleanup_state.json"

STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_SUPERSEDED = "superseded"

# 配信済み・取り消し済みの記録の保持日数（活動の記録と同じ
# 考え方で90日。未配信の出来事は対象にしない）
RETENTION_DAYS = 90

# 朝の知らせへ入れる寄せた変化の上限（古い順）。朝のまとめを
# 「読める長さ」に保つための上限で、溢れた分は消さず未配信の
# まま翌日以降へ回す
MAX_BRIEFING_EVENTS = 5

# 朝のまとめの材料へ入れる際の、要約1件あたりの切り詰め。
# 置き場の原本（最大400字）は保持し、指示文へ入れる分だけ短く
# する
_MATERIAL_SUMMARY_CHARS = 160
# 節全体の目安（文字数）。5件×（要約2つ＋見出し等）でも収まる
# ようにし、超える場合は要約をさらに切り詰める
_MATERIAL_TOTAL_CHARS = 2000

# 判断理由のコード（設計 §2.2）
REASON_WATCH_SETTING_DIGEST = "watch_setting_digest"
REASON_WATCH_SETTING_IMMEDIATE = "watch_setting_immediate"
REASON_QUEUE_FAILED_IMMEDIATE = "queue_failed_immediate"
REASON_NO_DIGEST_ROUTE_IMMEDIATE = "no_digest_route_immediate"
REASON_TRIAGE_DISABLED_IMMEDIATE = "triage_disabled_immediate"
# モデル判定（auto、0.85.0）の理由コード。digest / immediate は
# 採用された判定、uncertain は「確信が持てないため採用せず
# 即時」、invalid は「形式不正・例外などで判定が使えなかった
# ため即時」
REASON_MODEL_DIGEST = "model_digest"
REASON_MODEL_IMMEDIATE = "model_immediate"
REASON_MODEL_UNCERTAIN_IMMEDIATE = "model_uncertain_immediate"
REASON_MODEL_INVALID_IMMEDIATE = "model_invalid_immediate"

ROUTE_IMMEDIATE = "immediate"
ROUTE_DIGEST = "digest"
# auto の見張りで、モデル判定がまだ必要な状態を表す経路。
# decide_watch_route が返し、巡回側が判定役を呼んで最終的な
# 経路（immediate / digest）と理由コードに置き換える
ROUTE_AUTO = "auto"

# 朝のまとめ／日報の節の見出し（材料文と日報の組み立てで共用）
DIGEST_SECTION_TITLE = "まとめて知らせる変化"

# 追記と掃除の書き換えを直列化する（活動の記録と同じ流儀）
_write_lock = threading.Lock()


def decide_watch_route(watch: Watch, *, mode: str,
                       triage_enabled: bool) -> tuple[str, str]:
    """見張りの変化の届け方を決め、(経路, 理由コード) を返す。

    決め方の優先順位（設計 §2.2）:
    1. 強制規則: スレッドモード（wall）では朝の知らせの経路が
       無いため、digest / auto の指定でも即時に倒す（auto の
       判定も行わない）
    2. 切り戻し: 振り分け自体が無効化されている間
       （KUMA_NOTIFICATION_TRIAGE=0）は即時に倒す（auto の
       判定も行わない）。ただし既に置き場にある未配信分は、
       この判定の対象ではなく朝の知らせで読み続けられる
       （呼び出し側の扱い）
    3. 見張りの明示設定: immediate / digest はそのまま経路に
       なる。モデル判定で上書きはしない
    4. モデル判定: auto のときだけ ROUTE_AUTO を返し、実際の
       判定は巡回側が振り分け判定役を呼んで行う。この関数の
       返す理由コードは空で、最終的な理由コードは判定結果
       （parse_triage_verdict）で決まる
    """
    if mode != "ledger":
        return ROUTE_IMMEDIATE, REASON_NO_DIGEST_ROUTE_IMMEDIATE
    if not triage_enabled:
        return ROUTE_IMMEDIATE, REASON_TRIAGE_DISABLED_IMMEDIATE
    if watch.delivery == "digest":
        return ROUTE_DIGEST, REASON_WATCH_SETTING_DIGEST
    if watch.delivery == "auto":
        return ROUTE_AUTO, ""
    return ROUTE_IMMEDIATE, REASON_WATCH_SETTING_IMMEDIATE


def watch_event_id(watch: Watch, new_hash: str) -> str:
    """見張りの変化1件の出来事 ID を決める。

    見張り ID＋前回確認時刻＋新しいハッシュから作るため、保存や
    基準更新の失敗で同じ変化が再検出されても同じ ID になり、
    置き場へ二重に入らない。
    """
    return (f"watch:{watch.id}:{watch.last_checked_at}:"
            f"{new_hash[:12]}")


@dataclass
class QueueEvent:
    """置き場の出来事1件（ID ごとの最新行の姿）。"""
    id: str
    status: str = STATUS_PENDING
    recorded_at: str = ""
    occurred_at: str = ""
    source_kind: str = ""
    source_id: str = ""
    source_title: str = ""
    title: str = ""
    old_summary: str = ""
    new_summary: str = ""
    source_note: str = ""
    decision: str = ""
    decision_by: str = ""
    decision_reason: str = ""
    delivered_on: str = ""
    delivered_via: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "recorded_at": self.recorded_at,
            "occurred_at": self.occurred_at,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_title": self.source_title,
            "title": self.title,
            "old_summary": self.old_summary,
            "new_summary": self.new_summary,
            "source_note": self.source_note,
            "decision": self.decision,
            "decision_by": self.decision_by,
            "decision_reason": self.decision_reason,
            "delivered_on": self.delivered_on,
            "delivered_via": self.delivered_via,
        }

    @classmethod
    def from_dict(cls, record: object) -> QueueEvent | None:
        """辞書から復元する。ID が無い行は None（読み飛ばす）。"""
        if not isinstance(record, dict):
            return None
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            return None
        status = record.get("status")
        return cls(
            id=event_id,
            status=(status if status in (STATUS_PENDING,
                                         STATUS_DELIVERED,
                                         STATUS_SUPERSEDED)
                    else STATUS_PENDING),
            recorded_at=str(record.get("recorded_at") or ""),
            occurred_at=str(record.get("occurred_at") or ""),
            source_kind=str(record.get("source_kind") or ""),
            source_id=str(record.get("source_id") or ""),
            source_title=str(record.get("source_title") or ""),
            title=str(record.get("title") or ""),
            old_summary=str(record.get("old_summary") or ""),
            new_summary=str(record.get("new_summary") or ""),
            source_note=str(record.get("source_note") or ""),
            decision=str(record.get("decision") or ""),
            decision_by=str(record.get("decision_by") or ""),
            decision_reason=str(record.get("decision_reason") or ""),
            delivered_on=str(record.get("delivered_on") or ""),
            delivered_via=str(record.get("delivered_via") or ""),
        )


def build_watch_event(watch: Watch, old_summary: str, new_summary: str,
                      new_hash: str, now: datetime,
                      reason: str,
                      decision_by: str = "setting") -> QueueEvent:
    """見張りの変化から、置き場へ入れる出来事を組み立てる。

    decision_by は誰が決めたか（setting=見張りの明示設定、
    model=auto のモデル判定）。理由コードと対で記録し、後から
    「なぜ朝へ寄ったか」を検証できるようにする。
    """
    stamp = now.isoformat(timespec="seconds")
    return QueueEvent(
        id=watch_event_id(watch, new_hash),
        status=STATUS_PENDING,
        recorded_at=stamp,
        occurred_at=stamp,
        source_kind="watch",
        source_id=watch.id,
        source_title=watch.title,
        title=f"見張り「{watch.title}」で変化がありました",
        old_summary=old_summary,
        new_summary=new_summary,
        source_note=watch.note,
        decision=ROUTE_DIGEST,
        decision_by=decision_by,
        decision_reason=reason,
    )


# ---------------------------------------------------------- モデル判定（auto）

# 判定の知らせ文候補（notice）として受け付ける最大の文字数。
# これより長いものは「検証に通らない候補」として使わず、通常の
# 知らせ文作成に回す（判定そのものの採用は妨げない）
_MAX_TRIAGE_NOTICE_CHARS = 1000
# 記録に残すモデルの理由の最大の文字数（理由欄は短く保つ）
_MAX_TRIAGE_REASON_CHARS = 200


@dataclass
class TriageVerdict:
    """採用されたモデル判定（auto の判定結果）。

    route は最終的な経路（immediate / digest）。reason_text は
    モデルが返した短い理由（記録用。そのままの全文ではなく、
    記録向けに切り詰めたもの）。notice は即時になった場合の
    知らせ文候補で、検証を通らなかった場合は None（呼び出し側
    が通常の知らせ文作成に回す）。
    """
    route: str
    reason_text: str = ""
    notice: str | None = None


def _unwrap_json_text(raw: str) -> str:
    """判定結果の文字列から JSON 本文を取り出す。

    前後の空白と、コードフェンス（```json … ```）で包まれた
    場合の囲みだけを取り除く。本文の前後に説明文が付いたもの
    は「JSON だけを返す」という約束に反するため、ここでは
    救済しない（呼び出し側で形式不正として即時へ倒れる）。
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            text = "\n".join(lines[1:-1]).strip()
    return text


def parse_triage_verdict(
        raw: object) -> tuple[TriageVerdict | None, str, str]:
    """判定役の応答を検証し、(判定, 理由コード, モデルの理由) を返す。

    採用できるのは、route が immediate / digest のどちらかで、
    certain が真で、reason が空でない JSON だけ。それ以外は
    判定を None とし、理由コードで「なぜ採用しなかったか」を
    返す（呼び出し側は全て即時へ倒す。モデルの失敗で朝へ
    回したり出来事を消したりはしない）:

    - error の結果・文字列でない・JSON として読めない・項目が
      欠ける・知らない値: REASON_MODEL_INVALID_IMMEDIATE
    - certain が真でない（迷い・確信なし）:
      REASON_MODEL_UNCERTAIN_IMMEDIATE

    採用した場合は経路に応じた REASON_MODEL_* を返す。第3の
    戻り値は、モデルが返した理由の短い文（記録用に切り詰めた
    もの。読めなかった場合は空）。
    """
    if not isinstance(raw, str) or raw.startswith("error:"):
        return None, REASON_MODEL_INVALID_IMMEDIATE, ""
    try:
        data = json.loads(_unwrap_json_text(raw))
    except ValueError:
        return None, REASON_MODEL_INVALID_IMMEDIATE, ""
    if not isinstance(data, dict):
        return None, REASON_MODEL_INVALID_IMMEDIATE, ""
    reason_text = data.get("reason")
    reason_text = (reason_text.strip()[:_MAX_TRIAGE_REASON_CHARS]
                   if isinstance(reason_text, str) else "")
    route = data.get("route")
    if route not in (ROUTE_IMMEDIATE, ROUTE_DIGEST):
        return None, REASON_MODEL_INVALID_IMMEDIATE, reason_text
    certain = data.get("certain")
    if certain is not True:
        # certain が欠けている・真偽値でない場合は形式不正、
        # false の場合は「確信なし」として扱う
        if isinstance(certain, bool):
            return None, REASON_MODEL_UNCERTAIN_IMMEDIATE, reason_text
        return None, REASON_MODEL_INVALID_IMMEDIATE, reason_text
    if not reason_text:
        return None, REASON_MODEL_INVALID_IMMEDIATE, ""
    notice = data.get("notice")
    if (not isinstance(notice, str)
            or not notice.strip()
            or len(notice) > _MAX_TRIAGE_NOTICE_CHARS
            or notice.strip().startswith("error:")):
        # 知らせ文候補が使えなくても判定自体は採用し、候補だけ
        # を捨てる（即時なら通常の知らせ文作成に回る）
        notice = None
    else:
        notice = notice.strip()
    code = (REASON_MODEL_DIGEST if route == ROUTE_DIGEST
            else REASON_MODEL_IMMEDIATE)
    return TriageVerdict(route=route, reason_text=reason_text,
                         notice=notice), code, reason_text


def triage_feed_note(watch_title: str, reason: str,
                     model_reason: str = "") -> str:
    """auto の判定で即時になった場合に、フィードへ残す判定の記録。

    即時の出来事は置き場の対象外（置き場は「寄せた通知」の
    保持が役割）のため、判定の理由はこちらに残す（digest の
    出来事は置き場の decision / decision_reason が正）。文面は
    読み返す記録として平易な日本語にし、モデルの短い理由が
    あれば添える。理由コード自体は、置き場と突き合わせられる
    よう括弧内に併記する。
    """
    if reason == REASON_MODEL_IMMEDIATE:
        base = (f"見張り「{watch_title}」の変化は、おまかせ判定で"
                "その場でお知らせしました")
    elif reason == REASON_MODEL_UNCERTAIN_IMMEDIATE:
        base = (f"見張り「{watch_title}」の変化は、おまかせ判定が"
                "定まらなかったため、その場でお知らせしました")
    elif reason == REASON_QUEUE_FAILED_IMMEDIATE:
        base = (f"見張り「{watch_title}」の変化は、朝へ寄せる置き場"
                "へ保存できなかったため、その場でお知らせしました")
    else:  # REASON_MODEL_INVALID_IMMEDIATE 等
        base = (f"見張り「{watch_title}」の変化は、おまかせ判定を"
                "使えなかったため、その場でお知らせしました")
    detail = f"判定の理由: {model_reason}（{reason}）" if model_reason \
        else f"理由コード: {reason}"
    return f"{base}（{detail}）。"


def _read_records(user_data_dir: Path) -> list[dict]:
    """置き場の全行を読む。ファイルが無い・読めなければ空。"""
    try:
        lines = (user_data_dir / QUEUE_FILE).read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # 壊れた行は読み飛ばす
        if isinstance(record, dict):
            records.append(record)
    return records


def load_events(user_data_dir: Path) -> list[QueueEvent]:
    """出来事を ID ごとの最新行で、最初に現れた順に返す。"""
    events: dict[str, QueueEvent] = {}
    order: list[str] = []
    for record in _read_records(user_data_dir):
        event = QueueEvent.from_dict(record)
        if event is None:
            continue
        if event.id not in events:
            order.append(event.id)
        events[event.id] = event
    return [events[event_id] for event_id in order]


def pending_events(user_data_dir: Path,
                   limit: int | None = None) -> list[QueueEvent]:
    """未配信の出来事を古い順に返す。"""
    events = [e for e in load_events(user_data_dir)
              if e.status == STATUS_PENDING]
    events.sort(key=lambda e: (e.recorded_at, e.id))
    if limit is not None:
        events = events[:limit]
    return events


def has_event(user_data_dir: Path, event_id: str) -> bool:
    """その ID の出来事が置き場に既にあるかどうか。"""
    return any(e.id == event_id for e in load_events(user_data_dir))


def enqueue_event(user_data_dir: Path, event: QueueEvent) -> bool:
    """出来事を置き場へ追記し、保存できたかどうかを返す。

    呼び出し側（巡回）が「保存できた場合だけ基準を進める」判断
    に使うため、成否は戻り値で返し、例外は外へ出さない。同じ
    ID の出来事が既にある場合は二重に入れず、保存済みとして
    True を返す（再検出された同じ変化）。
    """
    try:
        with _write_lock:
            if has_event(user_data_dir, event.id):
                return True
            user_data_dir.mkdir(parents=True, exist_ok=True)
            with open(user_data_dir / QUEUE_FILE, "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict(),
                                   ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def mark_delivered(user_data_dir: Path, event_ids: list[str], *,
                   via: str, now: datetime | None = None) -> int:
    """選んだ出来事に配信済みの行を追記し、付けた件数を返す。

    朝の知らせとして実際に投稿された後にだけ呼ぶこと。対象は
    現在 pending のものだけ（既に配信済みのものへは重ねない）。
    追記に失敗しても例外は外へ出さず、その場合は未配信のまま
    残り、翌日以降に重複して届く側に倒れる。
    """
    if not event_ids:
        return 0
    now = now or datetime.now()
    marked = 0
    try:
        with _write_lock:
            current = {e.id: e for e in load_events(user_data_dir)}
            lines = []
            for event_id in event_ids:
                event = current.get(event_id)
                if event is None or event.status != STATUS_PENDING:
                    continue
                event.status = STATUS_DELIVERED
                event.delivered_on = now.date().isoformat()
                event.delivered_via = via
                lines.append(json.dumps(event.to_dict(),
                                        ensure_ascii=False))
                marked += 1
            if lines:
                user_data_dir.mkdir(parents=True, exist_ok=True)
                with open(user_data_dir / QUEUE_FILE, "a",
                          encoding="utf-8") as f:
                    for line in lines:
                        f.write(line + "\n")
    except Exception:
        return 0
    return marked


# ---------------------------------------------------------- 材料の組み立て

def _clip(text: str, limit: int) -> str:
    """材料用に切り詰める（空白を潰し、上限を超えたら … を付ける）。"""
    collapsed = " ".join(str(text).split())
    if len(collapsed) > limit:
        return collapsed[:limit] + "…"
    return collapsed


def _event_date_prefix(event: QueueEvent, now: datetime) -> str:
    """検出日を表す前置き。今日なら空、前日以前なら日付や日数。"""
    try:
        occurred = datetime.fromisoformat(event.occurred_at)
    except (TypeError, ValueError):
        return ""
    days = (now.date() - occurred.date()).days
    if days <= 0:
        return ""
    if days >= 7:
        return f"（{days}日前）"
    return f"（{occurred.month}月{occurred.day}日）"


def _event_line(event: QueueEvent, now: datetime,
                summary_chars: int) -> str:
    """出来事1件を節の1行にする（材料文と日報で同じ形）。"""
    line = (f"・{_event_date_prefix(event, now)}"
            f"見張り「{event.source_title}」: "
            f"今回は {_clip(event.new_summary, summary_chars)}"
            f"（前回: {_clip(event.old_summary, summary_chars)}）")
    if event.source_note:
        line += f"（指示: {_clip(event.source_note, summary_chars)}）"
    return line


def _join_lines(events: list[QueueEvent], now: datetime) -> str:
    """出来事の行を、節全体の目安に収まるよう要約を調整して組む。"""
    for summary_chars in (_MATERIAL_SUMMARY_CHARS, 80, 40):
        text = "\n".join(_event_line(e, now, summary_chars)
                         for e in events)
        if len(text) <= _MATERIAL_TOTAL_CHARS:
            return text
    return text[:_MATERIAL_TOTAL_CHARS]


def build_digest_material(events: list[QueueEvent],
                          now: datetime) -> str | None:
    """朝のまとめの材料にする、寄せた変化の節の文面。

    出来事ごとの行（検出日・見張りの名前・前回と今回の要約・
    指示）を古い順に並べる。対象が無ければ None。
    """
    if not events:
        return None
    return _join_lines(events, now)


def build_digest_daily_section(events: list[QueueEvent],
                               now: datetime) -> str | None:
    """日報の組み立てに足す、寄せた変化の節（コードで決まった形）。

    朝のまとめの子が失敗・「なし」・モデルなしの場合でも届ける
    ための倒し込みで、用事が無くてもこの節だけで日報になる。
    対象が無ければ None。
    """
    if not events:
        return None
    lines = [f"まとめてのお知らせが {len(events)} 件あります。"]
    lines.append(_join_lines(events, now))
    return "\n".join(lines)


# ---------------------------------------------------------- 掃除

def cleanup_queue(user_data_dir: Path,
                  retention_days: int = RETENTION_DAYS,
                  now: datetime | None = None) -> int:
    """配信済み・取り消し済みで期限より古い出来事を置き場から落とす。

    未配信（pending）の出来事は、どれだけ古くても落とさない
    （自動の有効期限は付けない）。落とすのは、ID ごとの最新行が
    delivered / superseded で、その行の記録時刻が期限より前の
    出来事の全行。時刻が読めない行は古いと判断できないため残す
    （消す側ではなく残す側に倒す）。落とした出来事の件数を返す。
    失敗しても例外は外へ出さない。
    """
    if retention_days <= 0:
        return 0
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)
    path = user_data_dir / QUEUE_FILE
    try:
        with _write_lock:
            try:
                raw_lines = path.read_text(
                    encoding="utf-8").splitlines()
            except OSError:
                return 0
            latest: dict[str, QueueEvent] = {}
            for line in raw_lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                event = QueueEvent.from_dict(record)
                if event is not None:
                    latest[event.id] = event
            drop_ids: set[str] = set()
            for event_id, event in latest.items():
                if event.status == STATUS_PENDING:
                    continue
                try:
                    recorded = datetime.fromisoformat(event.recorded_at)
                except (TypeError, ValueError):
                    continue
                if recorded < cutoff:
                    drop_ids.add(event_id)
            if not drop_ids:
                return 0
            kept: list[str] = []
            for line in raw_lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    kept.append(line)  # 壊れた行は残す側に倒す
                    continue
                if (isinstance(record, dict)
                        and record.get("id") in drop_ids):
                    continue
                kept.append(line)
            removed_events = len(drop_ids)
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for line in kept:
                    f.write(line + "\n")
            os.replace(tmp, path)
    except Exception:
        return 0  # 掃除の失敗で巡回は落とさない
    return removed_events


def _last_cleanup_on(user_data_dir: Path) -> str | None:
    """前回の自動掃除の日付（YYYY-MM-DD）。無ければ None。"""
    try:
        data = json.loads(
            (user_data_dir / CLEANUP_STATE_FILE).read_text(
                encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict):
        value = data.get("last_cleanup_on")
        if isinstance(value, str):
            return value
    return None


def auto_cleanup_queue(user_data_dir: Path,
                       now: datetime | None = None) -> int:
    """巡回用の自動掃除。1日1回まで、古い配信済み記録を落とす。

    活動の記録の自動掃除と同じ流儀で、落とせる行が無かった日も
    掃除済みとして記録する。失敗しても例外は外へ出さない。
    """
    now = now or datetime.now()
    today = now.date().isoformat()
    try:
        if _last_cleanup_on(user_data_dir) == today:
            return 0  # 本日は掃除済み
        user_data_dir.mkdir(parents=True, exist_ok=True)
        (user_data_dir / CLEANUP_STATE_FILE).write_text(
            json.dumps({"last_cleanup_on": today}), encoding="utf-8")
    except Exception:
        return 0
    return cleanup_queue(user_data_dir, now=now)
