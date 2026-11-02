"""頭脳の使用量の記録と集計。

モデルが応答するたびに、そのユーザーの置き場の usage.jsonl へ
1 行ずつ記録する（Session が呼び出しのたびに record_usage を呼ぶ）。
記録には呼び出し回数として数えるための情報（時刻・頭脳・モデル・
セッション）と、API 系の頭脳が応答に含めていた使用量（トークン数）
が入る。CLI 系の頭脳は使用量を返さないため、トークン数は None
（呼び出し回数だけが記録される）。

集計（summarize_usage）は合計・頭脳／モデル別・日別（直近30日）を
返す。費用への換算はしない（単価は頭脳・モデルごとに異なり、契約
形態でも変わるため、使用量そのものだけを見せる）。

usage.jsonl は運用の記録として残すため、保持期限の掃除
（memory.cleanup_expired）の対象にはしない。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .llm import Usage

USAGE_FILE = "usage.jsonl"

# 日別集計に含める直近の日数（今日を含む）
RECENT_DAYS = 30

# 追記を直列化する（Web サーバーは複数スレッドから同時に記録する）
_write_lock = threading.Lock()


def record_usage(user_data_dir: Path, provider: str, model: str,
                 usage: Usage | None, session_id: str,
                 now: datetime | None = None) -> None:
    """使用量を 1 件、ユーザーの置き場の usage.jsonl へ追記する。

    usage が None（CLI 系の頭脳や、使用量を返さなかった応答）の
    ときはトークン数をすべて None にして、呼び出し回数だけを記録
    する。記録に失敗しても会話を落とさないよう、例外は外へ
    出さない。
    """
    entry = {
        "ts": (now or datetime.now()).isoformat(timespec="seconds"),
        "provider": provider,
        "model": model,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "total_tokens": usage.total_tokens if usage else None,
        "session_id": session_id,
    }
    try:
        with _write_lock:
            user_data_dir.mkdir(parents=True, exist_ok=True)
            with open(user_data_dir / USAGE_FILE, "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 記録の失敗で会話を落とさない


def load_usage_records(user_data_dir: Path) -> list[dict]:
    """usage.jsonl の記録をすべて読む。壊れた行は読み飛ばす。"""
    path = user_data_dir / USAGE_FILE
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
    return records


@dataclass
class UsageTotals:
    """使用量の合計。トークン数は、記録にある分だけを足す。"""

    calls: int = 0  # 呼び出し回数
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    token_calls: int = 0  # トークン数が 1 項目でも記録されていた回数

    def add(self, record: dict) -> None:
        self.calls += 1
        known = False
        for key, attr in (("input_tokens", "input_tokens"),
                          ("output_tokens", "output_tokens"),
                          ("total_tokens", "total_tokens")):
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                setattr(self, attr, getattr(self, attr) + value)
                known = True
        if known:
            self.token_calls += 1

    @property
    def has_tokens(self) -> bool:
        """トークン数が記録されていた呼び出しが 1 件でもあるか。"""
        return self.token_calls > 0

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "has_tokens": self.has_tokens,
        }


@dataclass
class UsageSummary:
    """使用量の集計結果（合計・頭脳／モデル別・日別）。"""

    total: UsageTotals = field(default_factory=UsageTotals)
    # (頭脳, モデル, 合計) の並び。頭脳名・モデル名の順
    by_provider: list[tuple[str, str, UsageTotals]] = field(default_factory=list)
    # (日付 "YYYY-MM-DD", 合計) の並び。日付の昇順、直近 RECENT_DAYS 日だけ
    by_day: list[tuple[str, UsageTotals]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            **self.total.to_dict(),
            "by_provider": [
                {"provider": provider, "model": model, **totals.to_dict()}
                for provider, model, totals in self.by_provider
            ],
            "by_day": [
                {"date": day, **totals.to_dict()}
                for day, totals in self.by_day
            ],
        }


def _record_day(record: dict) -> str | None:
    """記録の日時から日付（YYYY-MM-DD）を取り出す。読めなければ None。"""
    ts = record.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts).date().isoformat()
    except ValueError:
        return None


def summarize_usage(user_data_dir: Path,
                    now: datetime | None = None) -> UsageSummary:
    """usage.jsonl を集計する（合計・頭脳／モデル別・日別）。

    日別は直近 RECENT_DAYS 日（今日を含む）に記録がある日だけを
    日付の昇順で返す。古い記録も合計と頭脳別には含める。
    """
    now = now or datetime.now()
    cutoff = (now.date() - timedelta(days=RECENT_DAYS - 1)).isoformat()
    summary = UsageSummary()
    providers: dict[tuple[str, str], UsageTotals] = {}
    days: dict[str, UsageTotals] = {}
    for record in load_usage_records(user_data_dir):
        summary.total.add(record)
        key = (str(record.get("provider") or ""),
               str(record.get("model") or ""))
        providers.setdefault(key, UsageTotals()).add(record)
        day = _record_day(record)
        if day is not None and day >= cutoff:
            days.setdefault(day, UsageTotals()).add(record)
    summary.by_provider = [
        (provider, model, providers[(provider, model)])
        for provider, model in sorted(providers)
    ]
    summary.by_day = [(day, days[day]) for day in sorted(days)]
    return summary


def _format_tokens(totals: UsageTotals) -> str:
    """トークン数の並びを表示用に整える（記録が無ければその旨）。"""
    if not totals.has_tokens:
        return "トークン数は記録されていません"
    return (f"入力 {totals.input_tokens:,}、出力 {totals.output_tokens:,}、"
            f"合計 {totals.total_tokens:,}")


def format_usage_summary(summary: UsageSummary) -> str:
    """集計を CLI 表示用のテキストにする。"""
    lines = [f"呼び出し回数: {summary.total.calls} 回",
             f"トークン数: {_format_tokens(summary.total)}"]
    if summary.by_provider:
        lines.append("")
        lines.append("頭脳別:")
        for provider, model, totals in summary.by_provider:
            label = provider + (f" / {model}" if model else "")
            lines.append(f"- {label}: {totals.calls} 回"
                         f"（{_format_tokens(totals)}）")
    if summary.by_day:
        lines.append("")
        lines.append(f"日別（直近{RECENT_DAYS}日）:")
        for day, totals in summary.by_day:
            lines.append(f"- {day}: {totals.calls} 回"
                         f"（{_format_tokens(totals)}）")
    return "\n".join(lines)
