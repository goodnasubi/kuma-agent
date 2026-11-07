"""定期ジョブ（決まった予定で仕事を自動実行する仕組み）。

利用者が会話や CLI で登録したジョブを、ユーザーごとの置き場の
jobs.json に保持する。能動発信の巡回（proactive.py）が期限の来た
ジョブを見つけ、委任（delegate.py）の子セッションで実行して、
結果を main の会話へ投稿する。実行そのものは proactive.py 側が
担い、このモジュールは保持と期限判定だけを持つ。

ジョブの項目:
- id: ジョブの ID（使われていない番号を文字列で振る）
- title: 名前（利用者が呼ぶときの名前。削除や ON/OFF は名前でも
  ID でも指定できる）
- prompt: 実行する指示文（子エージェントへそのまま渡す）
- schedule: 予定。形式は次の 3 種だけ:
  - {"kind": "daily", "time": "09:00"}（毎日その時刻）
  - {"kind": "weekly", "weekday": 0, "time": "09:00"}
    （毎週その曜日。weekday は月曜=0 〜 日曜=6）
  - {"kind": "interval", "hours": 6}（前回実行からその時間ごと）
- delivery: 成功結果の届け方。immediate（結果をその場で知ら
  せる、既定）/ digest（結果の要点を置き場へ残し、朝の知らせ
  でまとめて知らせる）/ auto（結果ごとにモデルが判定し、その
  場で知らせるか朝の知らせへ寄せるかを決める。判定できない
  ときはその場で知らせる）の 3 つ。通知の取捨の第3段
  （0.86.0）で足した項目で、値と検証・表示名は見張り
  （watches.py）と共通。旧形式のファイルに無ければ immediate
  として読む。保存済みの値が不正な場合も immediate として
  読む（見張りと同じ考え方）。**失敗の知らせには効かない**:
  実行に失敗した場合は、delivery の値にかかわらずその場で
  知らせる（強制規則。失敗を朝へ回す設定は作らない）
- enabled: 有効かどうか（既定は有効）
- last_run_at: 前回実行した時刻（ISO 8601）。未実行なら空文字列
- created_at: 作成時刻（ISO 8601）。未実行ジョブの期限判定の
  基準にも使う

期限判定（is_due）:
- daily / weekly は、基準時刻（last_run_at、未実行なら created_at）
  より後に来た直近の予定時刻が現在時刻以下なら期限が来ている
- interval は基準時刻 + hours が現在時刻以下なら期限が来ている
- 複数回分を逃しても「期限が来ているかどうか」は真偽値でしか
  表さないため、巡回で実行するのは 1 回だけになる（追いつき
  実行はしない）。実行後は last_run_at が実行時刻に更新される
  ため、同じ予定で二重に実行することもない

ファイルが無い・壊れている場合は空の一覧として始める（例外で
落とさない）。壊れたレコードは読み飛ばす。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .notify_prefs import parse_hhmm
# 届け方（delivery）の値・検証・表示名は見張りと共通で、正は
# watches.py にある（通知の取捨で最初に導入した場所）。ここでは
# ジョブ側からも同じ名前で引けるよう再輸出する
from .watches import (
    DELIVERY_LABELS,
    DELIVERY_VALUES,
    DEFAULT_DELIVERY,
    stored_delivery,
    validate_delivery,
)

JOBS_FILENAME = "jobs.json"

SCHEDULE_KINDS = ("daily", "weekly", "interval")

# 曜日の表示名（月曜=0 の並び、datetime.weekday() と同じ）
WEEKDAY_LABELS_JA = ("月", "火", "水", "木", "金", "土", "日")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_time(raw: object) -> datetime | None:
    """ISO 形式の時刻文字列を読む。読めなければ None。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _as_local_naive(moment: datetime) -> datetime:
    """オフセット付き時刻をサーバーのローカル時刻（naive）にそろえる。

    通知時刻（Task の remind_at）と同じ取り決めで、オフセットなし
    はローカル時刻として解釈し、オフセット付きはローカル時刻へ
    変換して、naive な現在時刻と比較できるようにする。
    """
    if moment.tzinfo is not None:
        return moment.astimezone().replace(tzinfo=None)
    return moment


def _format_hhmm(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def _as_int(raw: object) -> int | None:
    """整数として読める値だけを int にする（真偽値は除く）。

    6.0 のような小数点付きでも、整数値なら受ける（モデルが JSON
    で渡すときに小数で来ることがあるため）。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    return None


def build_schedule(kind: object, time_str: object = None,
                   weekday: object = None, hours: object = None) -> dict:
    """予定の部品から schedule の辞書を組み立て、検証する。

    kind ごとに要る部品が決まっていて、要らない部品が渡された
    組み合わせも不正として ValueError にする（黙って捨てると、
    利用者の意図と違う予定で登録されてしまうため）。

    - daily: time（"HH:MM"）が要る。weekday / hours は不可
    - weekly: time と weekday（月曜=0 〜 日曜=6）が要る。hours は不可
    - interval: hours（1 以上）が要る。time / weekday は不可
    """
    kind_name = str(kind or "").strip().lower()
    if kind_name not in SCHEDULE_KINDS:
        raise ValueError(
            f"予定の種別は {' / '.join(SCHEDULE_KINDS)} のいずれかです")
    if kind_name == "interval":
        if time_str is not None or weekday is not None:
            raise ValueError(
                "interval（間隔）では time や weekday は指定できません")
        hours_value = _as_int(hours)
        if hours_value is None or hours_value < 1:
            raise ValueError(
                "interval（間隔）では hours に 1 以上の時間を指定してください")
        return {"kind": "interval", "hours": hours_value}
    # daily / weekly は時刻が要り、hours は付かない
    if hours is not None:
        raise ValueError(
            f"{kind_name} では hours は指定できません")
    parsed_time = parse_hhmm(time_str)
    if parsed_time is None:
        raise ValueError(
            f"{kind_name} では time を HH:MM 形式で指定してください")
    time_text = _format_hhmm(parsed_time.hour, parsed_time.minute)
    if kind_name == "daily":
        if weekday is not None:
            raise ValueError("daily（毎日）では weekday は指定できません")
        return {"kind": "daily", "time": time_text}
    weekday_value = _as_int(weekday)
    if weekday_value is None or not 0 <= weekday_value <= 6:
        raise ValueError(
            "weekly（毎週）では weekday に 0（月曜）〜6（日曜）を"
            "指定してください")
    return {"kind": "weekly", "weekday": weekday_value, "time": time_text}


def normalize_schedule(raw: object) -> dict:
    """保存済みの schedule を検証し、正規化した辞書を返す。

    形が不正なら ValueError。build_schedule に部品へ分解して
    通すことで、検証と正規化の流儀を 1 か所にそろえる。
    """
    if not isinstance(raw, dict):
        raise ValueError("schedule が辞書ではありません")
    return build_schedule(raw.get("kind"), raw.get("time"),
                          raw.get("weekday"), raw.get("hours"))


def schedule_description(schedule: dict) -> str:
    """予定を利用者向けの文にする（例: 毎日 09:00、毎週月曜 09:00）。

    読めない形式は「不明な予定」とする（手編集されたデータでも
    表示が壊れないように）。
    """
    if not isinstance(schedule, dict):
        return "不明な予定"
    kind = schedule.get("kind")
    if kind == "interval":
        hours = _as_int(schedule.get("hours"))
        return f"{hours} 時間ごと" if hours is not None else "不明な予定"
    parsed_time = parse_hhmm(schedule.get("time"))
    if parsed_time is None:
        return "不明な予定"
    time_text = _format_hhmm(parsed_time.hour, parsed_time.minute)
    if kind == "daily":
        return f"毎日 {time_text}"
    if kind == "weekly":
        weekday = _as_int(schedule.get("weekday"))
        if weekday is None or not 0 <= weekday <= 6:
            return "不明な予定"
        return f"毎週{WEEKDAY_LABELS_JA[weekday]}曜 {time_text}"
    return "不明な予定"


def format_last_run(raw: str) -> str:
    """前回実行時刻を表示用に整える。未実行ならその旨の文にする。"""
    if not raw:
        return "まだ実行していません"
    parsed = _parse_time(raw)
    if parsed is None:
        return raw
    return (f"{parsed.year}/{parsed.month}/{parsed.day} "
            f"{parsed.hour}:{parsed.minute:02d}")


def _reference_time(job: Job) -> datetime | None:
    """期限判定の基準時刻（前回実行、未実行なら作成時刻）。

    どちらも読めなければ None（判定できないので期限扱いにしない）。
    """
    parsed = _parse_time(job.last_run_at) or _parse_time(job.created_at)
    if parsed is None:
        return None
    return _as_local_naive(parsed)


def is_due(job: Job, now: datetime) -> bool:
    """ジョブの期限が来ているかどうかを返す。

    無効（enabled でない）ジョブは期限扱いにしない。予定の形が
    読めないジョブも対象から外す（壊れたデータで巡回を落とさない
    ため）。複数回分を逃していても結果は真偽値だけなので、実行は
    1 回だけになる。
    """
    if not job.enabled:
        return False
    reference = _reference_time(job)
    if reference is None:
        return False
    kind = job.schedule.get("kind") if isinstance(job.schedule, dict) else None
    if kind == "interval":
        hours = _as_int(job.schedule.get("hours"))
        if hours is None or hours < 1:
            return False
        return reference + timedelta(hours=hours) <= now
    if kind not in ("daily", "weekly"):
        return False
    parsed_time = parse_hhmm(job.schedule.get("time"))
    if parsed_time is None:
        return False
    if kind == "daily":
        candidate = datetime.combine(now.date(), parsed_time)
        if candidate > now:
            candidate -= timedelta(days=1)
        return reference < candidate
    weekday = _as_int(job.schedule.get("weekday"))
    if weekday is None or not 0 <= weekday <= 6:
        return False
    offset = (now.weekday() - weekday) % 7
    candidate = datetime.combine(now.date() - timedelta(days=offset),
                                 parsed_time)
    if candidate > now:
        candidate -= timedelta(days=7)
    return reference < candidate


@dataclass
class Job:
    """定期ジョブ 1 件。"""
    id: str
    title: str
    prompt: str
    schedule: dict
    delivery: str = DEFAULT_DELIVERY
    enabled: bool = True
    last_run_at: str = ""
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "schedule": dict(self.schedule),
            "delivery": self.delivery,
            "enabled": self.enabled,
            "last_run_at": self.last_run_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, record: object) -> Job | None:
        """辞書から復元する。形が不正なら None（読み飛ばす）。"""
        if not isinstance(record, dict):
            return None
        job_id = record.get("id")
        title = record.get("title")
        if not isinstance(job_id, str) or not job_id:
            return None
        if not isinstance(title, str) or not title:
            return None
        try:
            schedule = normalize_schedule(record.get("schedule"))
        except ValueError:
            return None
        enabled = record.get("enabled")
        return cls(
            id=job_id,
            title=title,
            prompt=str(record.get("prompt") or ""),
            schedule=schedule,
            # 届け方が無い・壊れている場合は immediate として読む
            # （ジョブ自体は落とさない。見張りと同じ考え方）
            delivery=stored_delivery(record.get("delivery")),
            enabled=enabled if isinstance(enabled, bool) else True,
            last_run_at=str(record.get("last_run_at") or ""),
            created_at=str(record.get("created_at") or _now()),
        )


class JobBoard:
    """定期ジョブの一覧。ジョブファイル（jobs.json）への保存・読み込みを持つ。

    用事の一覧（TaskBoard）と同じ流儀で、書き込みの前にはファイルを
    読み直し、他のセッションの追加を消さないようにする。
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.jobs_file = data_dir / JOBS_FILENAME
        self._jobs: list[Job] = self._load()

    # ------------------------------------------------------------ 永続化

    def _load(self) -> list[Job]:
        if not self.jobs_file.exists():
            return []
        try:
            raw = json.loads(self.jobs_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []  # 壊れたファイルは空として始める
        if not isinstance(raw, list):
            return []
        return [job for record in raw
                if (job := Job.from_dict(record)) is not None]

    def _save(self) -> None:
        self.jobs_file.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps([j.to_dict() for j in self._jobs],
                          ensure_ascii=False, indent=2)
        self.jobs_file.write_text(text + "\n", encoding="utf-8")

    # ------------------------------------------------------------ 操作

    def _next_id(self) -> str:
        used = {j.id for j in self._jobs}
        n = 1
        while str(n) in used:
            n += 1
        return str(n)

    def add(self, title: str, prompt: str, schedule: dict,
            delivery: str = DEFAULT_DELIVERY) -> Job:
        """ジョブを追加して保存する。不正な入力は ValueError。

        delivery は validate_delivery 済みの前提（見張りの
        WatchBoard.add と同じ流儀）。
        """
        title = str(title or "").strip()
        prompt = str(prompt or "").strip()
        if not title:
            raise ValueError("定期ジョブの名前が空です")
        if not prompt:
            raise ValueError("定期ジョブの指示文が空です")
        normalized = normalize_schedule(schedule)
        self._jobs = self._load()  # 他のセッションの追加を消さないよう読み直す
        job = Job(id=self._next_id(), title=title, prompt=prompt,
                  schedule=normalized, delivery=delivery)
        self._jobs.append(job)
        self._save()
        return job

    def list(self) -> list[Job]:
        """ジョブの一覧。返す前にファイルを読み直す。"""
        self._jobs = self._load()
        return list(self._jobs)

    def get(self, job_id: str) -> Job | None:
        for job in self._jobs:
            if job.id == job_id:
                return job
        return None

    def find(self, key: str) -> Job | None:
        """名前または ID でジョブを探す（ID の完全一致を優先する）。

        名前が重なっている場合は、先に登録された方を返す。
        """
        key = str(key or "").strip()
        if not key:
            return None
        for job in self._jobs:
            if job.id == key:
                return job
        for job in self._jobs:
            if job.title == key:
                return job
        return None

    def delete(self, key: str) -> Job | None:
        """名前または ID でジョブを削除して保存する。無ければ None。"""
        self._jobs = self._load()
        job = self.find(key)
        if job is None:
            return None
        self._jobs = [j for j in self._jobs if j.id != job.id]
        self._save()
        return job

    def set_enabled(self, key: str, enabled: bool) -> Job | None:
        """名前または ID でジョブの ON/OFF を切り替える。無ければ None。"""
        self._jobs = self._load()
        job = self.find(key)
        if job is None:
            return None
        job.enabled = bool(enabled)
        self._save()
        return job

    def set_delivery(self, key: str, delivery: str) -> Job | None:
        """名前または ID で成功結果の届け方を切り替える。無ければ None。

        delivery は validate_delivery 済みの前提（見張りの
        WatchBoard.set_delivery と同じ流儀）。失敗の知らせの
        届け方は変わらない（常にその場で知らせる）。
        """
        self._jobs = self._load()
        job = self.find(key)
        if job is None:
            return None
        job.delivery = delivery
        self._save()
        return job

    def mark_run(self, job_id: str,
                 when: datetime | None = None) -> Job | None:
        """前回実行時刻（last_run_at）を記録して保存する。無ければ None。

        成功・失敗にかかわらず実行したこと自体を記録し、同じ予定で
        二重に実行しないために使う。
        """
        self._jobs = self._load()
        job = self.get(job_id)
        if job is None:
            return None
        moment = when or datetime.now()
        job.last_run_at = moment.isoformat(timespec="seconds")
        self._save()
        return job
