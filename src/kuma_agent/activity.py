"""活動の記録（道具の呼び出し・承認・外部送信・CLI の起動）。

何か起きたときに「いつ・どの会話で・どの道具が・何の引数で・
承認されたか・結果はどうだったか」と「外へ何を送ったか」を、
モデルの応答文に頼らず後から追えるようにするための記録。
使用量（usage.py）と同じ流儀で、そのユーザーの置き場の
activity.jsonl へ 1 行 1 件で追記する。記録の失敗で会話や送信を
落とさないよう、例外は外へ出さない。

記録する種別は 5 つ:
- tool_call: 道具の呼び出し（引数の要約と結果の抜粋）
- approval: 承認対象の道具への承認・却下
- external_send: Telegram / LINE への能動発信の配信
- cli_launch: CLI 系の頭脳の起動。CLI 系では道具の実行が外部
  CLI の側で行われるため、道具単位の記録はできず、起動した
  ことだけを残す（記録の粒度が粗くなる限界がある）
- background_model: 巡回の裏側のモデル呼び出し（長期記憶の
  自動抽出・手順書の自動学習）の実行。Session を経由しない
  単発呼び出しのため、成功・失敗と件数などの要点だけを残す
  （プロンプトや会話の全文は残さない）。対象は巡回分だけで、
  CLI の明示実行（memory-extract / skill-learn）は残さない。
  使用量は usage.py 側にも併せて記録する（歯止めの漏れ点検
  L3 の直し、0.88.0）

引数は承認画面向けの要約（tools.summarize_tool_args）を再利用し、
引数全体は記録しない（秘密情報を持ち込む道具があるため）。

道具の失敗は、結果文字列から種類に分けて error_kind としても
残す（classify_tool_error）。成功した呼び出しには付けない。
分類は結果の文言頼みで完全ではないため、どれにも当てはまら
ない失敗は error（その他の例外）に倒す。

保持期限: 記録は行ごとの時刻で切り、期限より古い行は能動発信の
巡回（proactive.check_and_post）から 1 日 1 回まで
auto_cleanup_activity が落とす。保持日数は Config の
activity_retention_days（環境変数 KUMA_ACTIVITY_RETENTION_DAYS、
既定 90 日。0 で無期限）。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .tools import APPROVAL_DENIED_RESULT, CANCELLED_RESULT

ACTIVITY_FILE = "activity.jsonl"
CLEANUP_STATE_FILE = "activity_cleanup_state.json"

KIND_TOOL_CALL = "tool_call"
KIND_APPROVAL = "approval"
KIND_EXTERNAL_SEND = "external_send"
KIND_CLI_LAUNCH = "cli_launch"
KIND_BACKGROUND_MODEL = "background_model"

# 道具の失敗の種類（error_kind）。結果文字列の出どころごとに
# 分ける（AF-1）。成功した呼び出しの記録には付けない
ERROR_KIND_APPROVAL_DENIED = "approval_denied"  # 承認されなかった
ERROR_KIND_CANCELLED = "cancelled"  # キャンセルされた
ERROR_KIND_NOT_FOUND = "not_found"  # 対象が見つからない
ERROR_KIND_TIMEOUT = "timeout"  # 時間切れ
ERROR_KIND_UNKNOWN_TOOL = "unknown_tool"  # 無い道具
ERROR_KIND_ERROR = "error"  # その他の例外
# Google カレンダー連携の状態の区別（list_calendar_events の
# 失敗だけに付ける。設計 §3.6）
ERROR_KIND_NOT_CONFIGURED = "not_configured"  # 連携が未設定
ERROR_KIND_NOT_AUTHORIZED = "not_authorized"  # 利用者が未認可
ERROR_KIND_REVOKED = "revoked"  # 許可が失効・取り消し
ERROR_KIND_INSUFFICIENT_SCOPE = "insufficient_scope"  # 許可の範囲不足

# 時間切れと分かる結果文の断片。委任の時間切れ（delegate.py）、
# MCP の応答待ち（mcp_client.py）、道具の subprocess や Web 取得
# のタイムアウト（例外の文言）で出どころがばらばらのため、
# 断片の集合で拾う
_TIMEOUT_MARKERS = (
    "時間内に終わりませんでした",
    "以内に応答しませんでした",
    "timed out",
)


def classify_tool_error(result: str) -> str | None:
    """道具の結果文字列を失敗の種類に分ける。失敗でなければ None。

    失敗かどうかは従来どおり「error で始まるか」で判定する。
    分類は文言頼みで完全ではないため、どれにも当てはまらない
    失敗は ERROR_KIND_ERROR（その他の例外）に倒す。Session の
    道具ループ（立て直しの促しの出し分け）と活動の記録の両方
    から使う。
    """
    if not result.startswith("error"):
        return None
    if result == APPROVAL_DENIED_RESULT:
        return ERROR_KIND_APPROVAL_DENIED
    if result == CANCELLED_RESULT:
        return ERROR_KIND_CANCELLED
    if result.startswith("error: unknown tool"):
        return ERROR_KIND_UNKNOWN_TOOL
    if result.startswith("error: Google カレンダー"):
        # 連携の失敗は状態で区別する（文面は google_calendar.py の
        # 結果文字列が正。取得失敗そのものはその他の例外に倒す）
        if "まだ設定されていません" in result:
            return ERROR_KIND_NOT_CONFIGURED
        if "許可がまだ済んでいません" in result:
            return ERROR_KIND_NOT_AUTHORIZED
        if "許可が無効になっています" in result:
            return ERROR_KIND_REVOKED
        if "許可の範囲が足りません" in result:
            return ERROR_KIND_INSUFFICIENT_SCOPE
    if "見つかりません" in result:
        return ERROR_KIND_NOT_FOUND
    if any(marker in result for marker in _TIMEOUT_MARKERS):
        return ERROR_KIND_TIMEOUT
    return ERROR_KIND_ERROR

# 一覧で返す件数の既定と上限（feed.py と同じ考え方）
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# 結果や文面の抜粋の上限（引数の要約と同じ 200 字）
SUMMARY_LIMIT = 200

# 追記と掃除の書き換えを直列化する（Web サーバーは複数スレッド
# から同時に記録し、巡回も同じファイルを触るため）
_write_lock = threading.Lock()


def _excerpt(text: object, limit: int = SUMMARY_LIMIT) -> str:
    """記録用の抜粋にする。改行などを空白に潰し、長ければ切り詰める。"""
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def record_activity(user_data_dir: Path, kind: str, *,
                    session_id: str = "", mode: str = "",
                    tool: str = "", summary: str = "",
                    result: str = "", target: str = "",
                    ok: bool | None = None,
                    error_kind: str = "",
                    now: datetime | None = None) -> None:
    """活動を 1 件、ユーザーの置き場の activity.jsonl へ追記する。

    記録に失敗しても呼び出し側（会話・送信・巡回）を落とさない
    よう、例外は外へ出さない。
    """
    entry: dict[str, object] = {
        "ts": (now or datetime.now()).isoformat(timespec="seconds"),
        "kind": kind,
        "session_id": session_id,
        "mode": mode,
        "tool": tool,
        "summary": summary,
        "result": result,
    }
    if target:
        entry["target"] = target
    if ok is not None:
        entry["ok"] = ok
    if error_kind:
        entry["error_kind"] = error_kind
    try:
        with _write_lock:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            with open(user_data_dir / ACTIVITY_FILE, "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 記録の失敗で本体の動作を落とさない


def record_tool_call(user_data_dir: Path, session_id: str, mode: str,
                     name: str, args_summary: str, result: str,
                     now: datetime | None = None) -> None:
    """道具の呼び出しを記録する（引数の要約と結果の抜粋）。

    失敗した呼び出しには、失敗の種類（error_kind）も付ける。
    """
    record_activity(
        user_data_dir, KIND_TOOL_CALL, session_id=session_id, mode=mode,
        tool=name, summary=args_summary, result=_excerpt(result),
        ok=not result.startswith("error"),
        error_kind=classify_tool_error(result) or "", now=now)


def record_approval(user_data_dir: Path, session_id: str, mode: str,
                    name: str, args_summary: str, approved: bool,
                    now: datetime | None = None) -> None:
    """承認対象の道具への承認・却下を記録する。"""
    record_activity(
        user_data_dir, KIND_APPROVAL, session_id=session_id, mode=mode,
        tool=name, summary=args_summary,
        result="approved" if approved else "denied", ok=approved,
        now=now)


def record_external_send(user_data_dir: Path, channel: str, target: str,
                         text: str, now: datetime | None = None) -> None:
    """外部（Telegram / LINE）への送信を記録する。

    宛先（chat id / user id）は利用者向け画面に出すものではない
    が、記録ファイル自体が利用者の置き場の中にあるため、誰へ
    送ったかを追えるよう target として残す。
    """
    record_activity(
        user_data_dir, KIND_EXTERNAL_SEND, tool=channel,
        summary=_excerpt(text), target=target, now=now)


def record_cli_launch(user_data_dir: Path, session_id: str, mode: str,
                      provider: str, model: str,
                      now: datetime | None = None) -> None:
    """CLI 系の頭脳を起動したことを記録する（道具単位は追えない）。"""
    record_activity(
        user_data_dir, KIND_CLI_LAUNCH, session_id=session_id, mode=mode,
        tool=provider, summary=model, now=now)


def record_background_model(user_data_dir: Path, process: str,
                            summary: str, *, ok: bool, result: str = "",
                            now: datetime | None = None) -> None:
    """巡回の裏側のモデル呼び出し（自動抽出・自動学習）を記録する。

    process は処理の名前（memory_extract / skill_learn）、summary
    は件数などの要点だけ（プロンプトや会話の全文は渡さない）。
    失敗した実行には error_kind として error を付ける。
    """
    record_activity(
        user_data_dir, KIND_BACKGROUND_MODEL, tool=process,
        summary=_excerpt(summary), result=result, ok=ok,
        error_kind="" if ok else ERROR_KIND_ERROR, now=now)


def _normalize_limit(limit: object) -> int:
    """件数の指定を既定・上限の範囲へそろえる。"""
    try:
        value = int(limit)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def load_activity_records(user_data_dir: Path,
                          limit: object = None) -> list[dict]:
    """activity.jsonl の記録を新しい順に返す。壊れた行は読み飛ばす。"""
    path = user_data_dir / ACTIVITY_FILE
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
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    records.reverse()
    return records[:_normalize_limit(limit)]


def _record_time(line: str) -> datetime | None:
    """1 行の記録の時刻を取り出す。読めなければ None。"""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    ts = record.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def cleanup_expired_activity(user_data_dir: Path, retention_days: int,
                             now: datetime | None = None) -> int:
    """保持期限より古い行を activity.jsonl から落とし、件数を返す。

    retention_days が 0 以下なら無期限として何もしない。時刻が
    読めない行（壊れた行を含む）は、古いと判断できないため残す
    （消す側ではなく残す側に倒す）。書き換えは一時ファイルへ
    書いてから os.replace で差し替え、追記と競合しないよう
    記録用のロックの中で行う。失敗しても例外は外へ出さない。
    """
    if retention_days <= 0:
        return 0
    path = user_data_dir / ACTIVITY_FILE
    now = now or datetime.now()
    cutoff = now - timedelta(days=retention_days)
    removed = 0
    try:
        with _write_lock:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return 0
            kept: list[str] = []
            for line in lines:
                if not line.strip():
                    continue
                ts = _record_time(line)
                if ts is not None and ts < cutoff:
                    removed += 1
                    continue
                kept.append(line)
            if not removed:
                return 0
            tmp = path.with_name(path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for line in kept:
                    f.write(line + "\n")
            os.replace(tmp, path)
    except Exception:
        return 0  # 掃除の失敗で巡回や会話を落とさない
    return removed


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


def auto_cleanup_activity(user_data_dir: Path, retention_days: int,
                          now: datetime | None = None) -> int:
    """巡回用の自動掃除。1 日 1 回まで、期限より古い行を落とす。

    落とせる行が無かった日も掃除済みとして記録する（巡回のたびに
    ファイルを読み直さないため）。無期限（retention_days が 0
    以下）のときは何もしない。失敗しても例外は外へ出さない。
    """
    if retention_days <= 0:
        return 0
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
    return cleanup_expired_activity(user_data_dir, retention_days, now=now)
