"""定期ジョブ（jobs）のテスト。

保持と期限判定（daily / weekly / interval、未実行の扱い、追いつき
実行しないこと）、道具の作成・一覧・削除・ON/OFF と不正入力、
能動発信の巡回での実行（結果が main の会話へ投稿され、
last_run_at が更新されること、テナント OFF では実行されないこと）
を確認する。
"""
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.delegate import DELEGATE_NOTICE
from kuma_agent.jobs import Job, JobBoard, build_schedule, is_due
from kuma_agent.llm import Message
from kuma_agent.notify_prefs import set_quiet_hours
from kuma_agent.proactive import check_and_post, run_sweep
from kuma_agent.session import Session
from kuma_agent.settings import set_proactive_enabled
from kuma_agent.tools import ToolBox, activity_label

# 2026-11-02 は月曜日
NOW = datetime(2026, 11, 2, 9, 30, 0)


def _job(schedule: dict, created_at: str = "2026-11-01T08:00:00",
         last_run_at: str = "", enabled: bool = True) -> Job:
    return Job(id="1", title="テストジョブ", prompt="仕事をする",
               schedule=schedule, enabled=enabled,
               last_run_at=last_run_at, created_at=created_at)


def _write_jobs(user_dir: Path, records: list[dict]) -> Path:
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / "jobs.json"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return path


def _job_record(title: str = "朝の確認", prompt: str = "ニュースを確認して",
                schedule: dict | None = None, enabled: bool = True,
                last_run_at: str = "",
                created_at: str = "2026-11-01T08:00:00",
                job_id: str = "1") -> dict:
    return {"id": job_id, "title": title, "prompt": prompt,
            "schedule": schedule or {"kind": "daily", "time": "09:00"},
            "enabled": enabled, "last_run_at": last_run_at,
            "created_at": created_at}


def _records(user_dir: Path) -> list[dict]:
    path = user_dir / "conversations" / "main.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- 期限判定: daily

def test_daily_due_after_scheduled_time():
    job = _job({"kind": "daily", "time": "09:00"})
    assert is_due(job, datetime(2026, 11, 2, 9, 0, 0))
    assert is_due(job, NOW)


def test_daily_not_due_before_scheduled_time():
    # 当日作ったジョブは、その日の予定時刻までは期限でない
    job = _job({"kind": "daily", "time": "09:00"},
               created_at="2026-11-02T08:00:00")
    assert not is_due(job, datetime(2026, 11, 2, 8, 30, 0))
    assert is_due(job, datetime(2026, 11, 2, 9, 0, 0))


def test_daily_uses_last_run_as_reference():
    job = _job({"kind": "daily", "time": "09:00"},
               last_run_at="2026-11-02T09:05:00")
    # 今日の分は実行済み
    assert not is_due(job, datetime(2026, 11, 2, 15, 0, 0))
    # 明日の予定時刻が来れば再び期限
    assert not is_due(job, datetime(2026, 11, 3, 8, 59, 0))
    assert is_due(job, datetime(2026, 11, 3, 9, 0, 0))


def test_daily_missed_days_still_single_due():
    # 数日分を逃しても「期限が来ている」は真偽値だけ（実行は 1 回）
    job = _job({"kind": "daily", "time": "09:00"},
               created_at="2026-10-30T08:00:00")
    assert is_due(job, NOW)


# ---------------------------------------------------------------- 期限判定: weekly

def test_weekly_due_on_matching_weekday():
    # 月曜=0。2026-11-02（月）の 09:00 が予定
    job = _job({"kind": "weekly", "weekday": 0, "time": "09:00"},
               created_at="2026-10-28T10:00:00")
    assert not is_due(job, datetime(2026, 11, 2, 8, 59, 0))
    assert is_due(job, NOW)


def test_weekly_not_due_after_this_weeks_run():
    job = _job({"kind": "weekly", "weekday": 0, "time": "09:00"},
               last_run_at="2026-11-02T09:10:00")
    # 火曜になっても今週の分は実行済み
    assert not is_due(job, datetime(2026, 11, 3, 10, 0, 0))
    # 来週の月曜の予定時刻で再び期限
    assert is_due(job, datetime(2026, 11, 9, 9, 0, 0))


def test_weekly_other_weekday_uses_latest_occurrence():
    # 水曜=2 のジョブ。前回実行が先週水曜なら、月曜の時点では期限でない
    job = _job({"kind": "weekly", "weekday": 2, "time": "09:00"},
               last_run_at="2026-10-28T09:05:00")
    assert not is_due(job, NOW)
    # 今週の水曜（11/4）の予定時刻が来れば期限
    assert is_due(job, datetime(2026, 11, 4, 9, 0, 0))


# ---------------------------------------------------------------- 期限判定: interval

def test_interval_due_after_hours_from_creation():
    job = _job({"kind": "interval", "hours": 6},
               created_at="2026-11-02T08:00:00")
    assert not is_due(job, datetime(2026, 11, 2, 13, 59, 0))
    assert is_due(job, datetime(2026, 11, 2, 14, 0, 0))


def test_interval_uses_last_run_as_reference():
    job = _job({"kind": "interval", "hours": 6},
               created_at="2026-11-01T08:00:00",
               last_run_at="2026-11-02T10:00:00")
    assert not is_due(job, datetime(2026, 11, 2, 15, 59, 0))
    assert is_due(job, datetime(2026, 11, 2, 16, 0, 0))


def test_interval_no_catchup_after_mark_run(tmp_path: Path):
    board = JobBoard(tmp_path)
    job = board.add("間隔ジョブ", "仕事をする", {"kind": "interval", "hours": 6})
    # 作成時刻を過去にずらし、複数回分を逃した状態にする
    record = _job_record(title="間隔ジョブ", prompt="仕事をする",
                         schedule={"kind": "interval", "hours": 6},
                         created_at="2026-11-01T00:00:00", job_id=job.id)
    _write_jobs(tmp_path, [record])
    stored = JobBoard(tmp_path).get(job.id)
    assert stored is not None and is_due(stored, NOW)
    # 実行済みを記録すれば、同じ時刻では二度と期限にならない
    board.mark_run(job.id, NOW)
    stored = JobBoard(tmp_path).get(job.id)
    assert stored is not None
    assert stored.last_run_at == NOW.isoformat(timespec="seconds")
    assert not is_due(stored, NOW)
    assert not is_due(stored, NOW + timedelta(hours=5, minutes=59))
    assert is_due(stored, NOW + timedelta(hours=6))


def test_disabled_job_is_never_due():
    job = _job({"kind": "daily", "time": "09:00"}, enabled=False)
    assert not is_due(job, NOW)


def test_broken_schedule_is_not_due():
    job = _job({"kind": "daily", "time": "9時"})
    assert not is_due(job, NOW)
    job = _job({"kind": "unknown"})
    assert not is_due(job, NOW)


# ---------------------------------------------------------------- 予定の検証

def test_build_schedule_valid_forms():
    assert build_schedule("daily", "9:00") == {"kind": "daily", "time": "09:00"}
    assert build_schedule("weekly", "09:00", 0) == {
        "kind": "weekly", "weekday": 0, "time": "09:00"}
    assert build_schedule("interval", None, None, 6) == {
        "kind": "interval", "hours": 6}


def test_build_schedule_rejects_invalid_combinations():
    bad_calls = [
        lambda: build_schedule("daily"),                    # time がない
        lambda: build_schedule("daily", "09:00", weekday=1),  # daily に weekday
        lambda: build_schedule("daily", "09:00", hours=6),  # daily に hours
        lambda: build_schedule("daily", "9時"),             # time の形式が不正
        lambda: build_schedule("weekly", "09:00"),          # weekday がない
        lambda: build_schedule("weekly", "09:00", 7),       # weekday の範囲外
        lambda: build_schedule("weekly", "09:00", 0, hours=6),  # weekly に hours
        lambda: build_schedule("interval"),                 # hours がない
        lambda: build_schedule("interval", hours=0),        # hours が 0
        lambda: build_schedule("interval", "09:00", hours=6),  # interval に time
        lambda: build_schedule("monthly", "09:00"),         # 種別が不正
    ]
    for call in bad_calls:
        try:
            call()
        except ValueError:
            pass
        else:
            raise AssertionError("不正な予定が通ってしまった")


# ---------------------------------------------------------------- 保持

def test_board_add_list_and_persistence(tmp_path: Path):
    board = JobBoard(tmp_path)
    first = board.add("朝の確認", "ニュースを確認して",
                      {"kind": "daily", "time": "09:00"})
    second = board.add("週次まとめ", "今週の進み具合をまとめて",
                       {"kind": "weekly", "weekday": 4, "time": "18:00"})
    assert first.id != second.id
    assert first.enabled is True and first.last_run_at == ""
    assert (tmp_path / "jobs.json").exists()

    restored = JobBoard(tmp_path)
    assert [j.title for j in restored.list()] == ["朝の確認", "週次まとめ"]
    got = restored.get(first.id)
    assert got is not None and got.schedule == {"kind": "daily", "time": "09:00"}
    assert got.prompt == "ニュースを確認して"


def test_board_starts_empty_on_broken_json(tmp_path: Path):
    (tmp_path / "jobs.json").write_text("{壊れた JSON", encoding="utf-8")
    board = JobBoard(tmp_path)  # 例外で落ちない
    assert board.list() == []
    job = board.add("復旧後のジョブ", "仕事をする",
                    {"kind": "interval", "hours": 3})
    assert JobBoard(tmp_path).get(job.id) is not None

    (tmp_path / "jobs.json").write_text('{"not": "a list"}', encoding="utf-8")
    assert JobBoard(tmp_path).list() == []


def test_board_skips_broken_records(tmp_path: Path):
    _write_jobs(tmp_path, [
        _job_record(title="まともなジョブ"),
        {"id": "2", "title": "予定が壊れたジョブ", "prompt": "x",
         "schedule": {"kind": "daily"}},  # time がない
        "not a dict",
        {"id": "3"},  # 名前がない
    ])
    assert [j.title for j in JobBoard(tmp_path).list()] == ["まともなジョブ"]


def test_board_find_delete_and_set_enabled(tmp_path: Path):
    board = JobBoard(tmp_path)
    job = board.add("掃除", "掃除をする", {"kind": "daily", "time": "10:00"})
    assert board.find(job.id) is not None
    assert board.find("掃除") is not None
    assert board.find("ない名前") is None

    updated = board.set_enabled("掃除", False)  # 名前で切り替え
    assert updated is not None and updated.enabled is False
    assert JobBoard(tmp_path).get(job.id).enabled is False
    assert board.set_enabled(job.id, True) is not None
    assert board.set_enabled("ない名前", True) is None

    removed = board.delete(job.id)  # ID で削除
    assert removed is not None and removed.title == "掃除"
    assert board.list() == []
    assert board.delete("掃除") is None


# ---------------------------------------------------------------- 道具

def _toolbox(tmp_path: Path) -> ToolBox:
    return ToolBox(tmp_path, job_board=JobBoard(tmp_path))


def test_job_tools_registered_only_with_board(tmp_path: Path):
    plain_names = {s["name"] for s in ToolBox(tmp_path).schemas()}
    assert "create_job" not in plain_names
    names = {s["name"] for s in _toolbox(tmp_path).schemas()}
    assert {"create_job", "list_jobs", "delete_job", "set_job_enabled"} <= names


def test_job_tools_roundtrip(tmp_path: Path):
    box = _toolbox(tmp_path)
    assert "定期ジョブはありません" in box.call("list_jobs", {})

    out = box.call("create_job", {
        "title": "朝の確認", "prompt": "ニュースを確認して",
        "kind": "daily", "time": "09:00"})
    assert "登録しました" in out and "朝の確認" in out and "毎日 09:00" in out
    out = box.call("create_job", {
        "title": "週報", "prompt": "週のまとめを作る",
        "kind": "weekly", "time": "18:00", "weekday": 4})
    assert "登録しました" in out and "毎週金曜 18:00" in out
    out = box.call("create_job", {
        "title": "見回り", "prompt": "状態を見回る",
        "kind": "interval", "hours": 6})
    assert "登録しました" in out and "6 時間ごと" in out

    listing = box.call("list_jobs", {})
    assert "朝の確認" in listing and "ON" in listing
    assert "まだ実行していません" in listing
    assert "ニュースを確認して" in listing

    # 名前で OFF、ID で ON
    assert "OFF" in box.call("set_job_enabled",
                             {"job": "朝の確認", "enabled": False})
    board = JobBoard(tmp_path)
    job = board.find("朝の確認")
    assert job is not None and job.enabled is False
    assert "ON" in box.call("set_job_enabled",
                            {"job": job.id, "enabled": True})

    # ID で削除、名前で削除
    assert "削除しました" in box.call("delete_job", {"job": job.id})
    assert "削除しました" in box.call("delete_job", {"job": "週報"})
    assert [j.title for j in JobBoard(tmp_path).list()] == ["見回り"]


def test_job_tools_not_found(tmp_path: Path):
    box = _toolbox(tmp_path)
    assert box.call("delete_job", {"job": "ないジョブ"}).startswith("error:")
    assert box.call("set_job_enabled",
                    {"job": "ないジョブ", "enabled": True}).startswith("error:")


def test_job_tools_reject_invalid_schedules(tmp_path: Path):
    box = _toolbox(tmp_path)
    base = {"title": "ジョブ", "prompt": "仕事をする"}
    invalid_args = [
        {**base, "kind": "daily"},                                   # time なし
        {**base, "kind": "daily", "time": "09:00", "weekday": 1},    # daily に weekday
        {**base, "kind": "daily", "time": "09:00", "hours": 6},      # daily に hours
        {**base, "kind": "daily", "time": "9時"},                    # 形式が不正
        {**base, "kind": "weekly", "time": "09:00"},                 # weekday なし
        {**base, "kind": "weekly", "time": "09:00", "weekday": 7},   # 範囲外
        {**base, "kind": "weekly", "time": "09:00", "weekday": 0,
         "hours": 6},                                                # weekly に hours
        {**base, "kind": "interval"},                                # hours なし
        {**base, "kind": "interval", "hours": 0},                    # hours が 0
        {**base, "kind": "interval", "hours": 6, "time": "09:00"},   # interval に time
        {**base, "kind": "monthly", "time": "09:00"},                # 種別が不正
        {"title": "", "prompt": "仕事をする", "kind": "daily",
         "time": "09:00"},                                           # 名前が空
        {"title": "ジョブ", "prompt": "", "kind": "daily",
         "time": "09:00"},                                           # 指示文が空
    ]
    for args in invalid_args:
        out = box.call("create_job", args)
        assert out.startswith("error:"), (args, out)
    # 不正なものは 1 件も登録されていない
    assert JobBoard(tmp_path).list() == []
    # enabled が真偽値でない場合も error
    box.call("create_job", {**base, "kind": "daily", "time": "09:00"})
    out = box.call("set_job_enabled", {"job": "ジョブ", "enabled": "yes"})
    assert out.startswith("error:")


def test_job_activity_labels():
    assert activity_label("create_job") == "定期ジョブを記録中"
    assert activity_label("list_jobs") == "定期ジョブを確認中"
    assert activity_label("delete_job") == "定期ジョブを記録中"
    assert activity_label("set_job_enabled") == "定期ジョブを記録中"


def test_session_registers_job_tools_in_both_modes(tmp_path: Path):
    class _Echo:
        def chat(self, system_prompt, messages, tools):
            return Message("assistant", "はい")

    for mode in ("ledger", "wall"):
        session = Session(Config(data_dir=tmp_path / mode, mode=mode), _Echo())
        names = {s["name"] for s in session.tools.schemas()}
        assert {"create_job", "list_jobs", "delete_job",
                "set_job_enabled"} <= names
        if mode == "wall":
            # スレッドモードでは用事系は出ないが、定期ジョブは出る
            assert "create_task" not in names


# ---------------------------------------------------------------- 巡回での実行

class _JobModel:
    """子（委任先）として呼ばれたら実行結果を返すスタブ頭脳。

    親と子は同じモデルを共用するため、子のシステムプロンプトに
    だけ入る委任の一文で見分ける（test_delegate の流儀）。
    """

    def __init__(self) -> None:
        self.child_task: str | None = None

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            self.child_task = messages[-1].content
            return Message("assistant", "ジョブの実行結果")
        return Message("assistant", "親への応答")


def test_sweep_runs_due_job_and_posts_result(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, [_job_record()])
    model = _JobModel()
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, model)

    text = check_and_post(cfg, now=NOW, session_provider=lambda: session)
    assert text is not None
    assert "定期ジョブ「朝の確認」の結果です。" in text
    assert "ジョブの実行結果" in text
    # 子には指示文がそのまま渡った
    assert model.child_task == "ニュースを確認して"

    # main の会話へ能動発信の印付きで投稿された
    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["role"] == "assistant"
    assert records[0]["content"] == text
    assert records[0]["proactive"] is True

    # 子の会話は delegate- の別セッションとして残る
    children = sorted((tmp_path / "conversations").glob("delegate-*.jsonl"))
    assert len(children) == 1

    # last_run_at が実行時刻に更新され、同じ時刻では再実行しない
    job = JobBoard(tmp_path).list()[0]
    assert job.last_run_at == NOW.isoformat(timespec="seconds")
    assert check_and_post(cfg, now=NOW, session_provider=lambda: session) is None
    assert len(_records(tmp_path)) == 1


def test_sweep_skips_not_due_and_disabled_jobs(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, [
        _job_record(title="未来のジョブ", job_id="1",
                    schedule={"kind": "daily", "time": "18:00"},
                    created_at="2026-11-02T08:00:00"),
        _job_record(title="止めたジョブ", job_id="2", enabled=False),
    ])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _JobModel())
    assert check_and_post(cfg, now=NOW, session_provider=lambda: session) is None
    assert _records(tmp_path) == []
    assert all(j.last_run_at == "" for j in JobBoard(tmp_path).list())


def test_sweep_job_failure_posts_notice_and_marks_run(tmp_path: Path):
    _write_jobs(tmp_path, [_job_record()])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _JobModel())

    def failing_executor(parent, prompt):
        return "error: 実行先で問題が起きました"

    text = check_and_post(cfg, now=NOW, session_provider=lambda: session,
                          job_executor=failing_executor)
    assert text is not None
    assert "定期ジョブ「朝の確認」の実行に失敗しました。" in text
    records = _records(tmp_path)
    assert len(records) == 1 and records[0]["proactive"] is True
    # 失敗でも last_run_at は更新される（同じ予定で繰り返さない）
    job = JobBoard(tmp_path).list()[0]
    assert job.last_run_at == NOW.isoformat(timespec="seconds")

    # executor が例外を投げても失敗の知らせになる
    _write_jobs(tmp_path, [_job_record(last_run_at="")])
    records_path = tmp_path / "conversations" / "main.jsonl"
    records_path.unlink()

    def raising_executor(parent, prompt):
        raise RuntimeError("爆発しました")

    text = check_and_post(cfg, now=NOW, session_provider=lambda: session,
                          job_executor=raising_executor)
    assert text is not None and "失敗しました" in text
    assert JobBoard(tmp_path).list()[0].last_run_at != ""


def test_sweep_job_not_run_when_tenant_off(tmp_path: Path):
    user_dir = tmp_path / "tenants" / "acme"
    _write_jobs(user_dir, [_job_record()])
    set_proactive_enabled(tmp_path, "acme", False)
    cfg = Config(data_dir=tmp_path, tenant="acme")
    session = Session(cfg, _JobModel())
    assert check_and_post(cfg, now=NOW, session_provider=lambda: session) is None
    assert _records(user_dir) == []
    assert JobBoard(user_dir).list()[0].last_run_at == ""


def test_sweep_job_runs_in_wall_mode(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, [_job_record()])
    cfg = Config(data_dir=tmp_path, mode="wall")
    session = Session(cfg, _JobModel())
    text = check_and_post(cfg, now=NOW, session_provider=lambda: session)
    assert text is not None and "結果です" in text
    assert len(_records(tmp_path)) == 1


def test_sweep_job_ignores_quiet_hours(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, [_job_record()])
    # NOW（9:30）を含む静かな時間帯。日報は止まるがジョブは止まらない
    set_quiet_hours(tmp_path, "08:00", "10:00")
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _JobModel())
    text = check_and_post(cfg, now=NOW, session_provider=lambda: session)
    assert text is not None and "結果です" in text


def test_run_sweep_runs_jobs_per_user(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, [_job_record(title="既定のジョブ")])
    bob_dir = tmp_path / "tenants" / "acme" / "users" / "bob"
    _write_jobs(bob_dir, [_job_record(title="bobのジョブ")])
    cfg = Config(data_dir=tmp_path)
    sessions: dict[tuple[str, str], Session] = {}

    def getter(tenant: str, user: str) -> Session:
        key = (tenant, user)
        if key not in sessions:
            scoped = replace(cfg, tenant=tenant, user=user)
            sessions[key] = Session(scoped, _JobModel())
        return sessions[key]

    posted = run_sweep(cfg, now=NOW, session_getter=getter)
    assert {(t, u) for t, u, _text in posted} == {
        ("default", "default"), ("acme", "bob")}
    assert "既定のジョブ" in _records(tmp_path)[0]["content"]
    assert "bobのジョブ" in _records(bob_dir)[0]["content"]
    # 2 回目の巡回ではどちらも再実行しない
    assert run_sweep(cfg, now=NOW, session_getter=getter) == []


# ---------------------------------------------------------------- CLI

def test_cli_jobs(monkeypatch, tmp_path: Path, capsys):
    _write_jobs(tmp_path, [
        _job_record(),
        _job_record(title="週報", job_id="2",
                    schedule={"kind": "weekly", "weekday": 4, "time": "18:00"},
                    enabled=False, last_run_at="2026-10-30T18:01:00"),
    ])
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KUMA_TENANT", raising=False)
    monkeypatch.delenv("KUMA_USER", raising=False)
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)

    assert cli.main(["jobs"]) == 0
    out = capsys.readouterr().out
    assert "朝の確認" in out and "毎日 09:00" in out and "ON" in out
    assert "週報" in out and "毎週金曜 18:00" in out and "OFF" in out
    assert "ニュースを確認して" in out


def test_cli_jobs_empty(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KUMA_TENANT", raising=False)
    monkeypatch.delenv("KUMA_USER", raising=False)
    assert cli.main(["jobs"]) == 0
    assert "定期ジョブはありません" in capsys.readouterr().out


def test_cli_jobs_scoped_by_tenant_and_user(monkeypatch, tmp_path: Path, capsys):
    bob_dir = tmp_path / "tenants" / "acme" / "users" / "bob"
    _write_jobs(bob_dir, [_job_record(title="bobのジョブ")])
    _write_jobs(tmp_path, [_job_record(title="既定のジョブ")])
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KUMA_TENANT", raising=False)
    monkeypatch.delenv("KUMA_USER", raising=False)
    assert cli.main(["jobs", "--tenant", "acme", "--user", "bob"]) == 0
    out = capsys.readouterr().out
    assert "bobのジョブ" in out and "既定のジョブ" not in out


def test_sweep_without_model_does_not_consume_jobs(tmp_path: Path,
                                                    monkeypatch):
    """モデルなしの巡回（CLI の proactive）ではジョブを実行も消費もしない。"""
    monkeypatch.chdir(tmp_path)
    _write_jobs(tmp_path, [_job_record()])
    cfg = Config(data_dir=tmp_path)

    assert check_and_post(cfg, now=NOW) is None
    jobs = JobBoard(tmp_path).list()
    assert jobs[0].last_run_at == ""  # 実行済みになっていない
    assert _records(tmp_path) == []  # 失敗の知らせも投稿されていない
