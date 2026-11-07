"""定期実行の棚卸し（フリート監査、第1段）のテスト。

印5種それぞれの検出と境界（閾値の前後）、月1回の頻度制御、
指紋による重複抑制と悪化時の再提示、朝のまとめ材料とフィード
記録、道具2つ（set_watch_interval / set_job_schedule）が基準や
次回実行時刻を壊さないこと、棚卸しの失敗が巡回本体を落とさない
ことを確認する。モデルは使わない仕組みなので、巡回の確認も
スタブで差し替える。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import kuma_agent.proactive as proactive_mod
from kuma_agent.config import Config
from kuma_agent.feed import load_feed_entries
from kuma_agent.fleet_audit import (
    STATE_FILENAME,
    audit_fleet,
    mark_fleet_briefing_used,
    pending_fleet_text,
)
from kuma_agent.jobs import JobBoard
from kuma_agent.llm import Message
from kuma_agent.notification_triage import QueueEvent, enqueue_event
from kuma_agent.proactive import check_and_post
from kuma_agent.session import Session
from kuma_agent.tools import READ_ONLY_TOOLS, ToolBox
from kuma_agent.watches import WatchBoard

NOW = datetime(2026, 11, 15, 12, 0, 0)
DEC = datetime(2026, 12, 15, 12, 0, 0)
JAN = datetime(2027, 1, 15, 12, 0, 0)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _watch_record(**overrides) -> dict:
    record = {
        "id": "1",
        "title": "見張り",
        "kind": "web",
        "target": "https://example.com/page",
        "interval_hours": 1,
        "note": "",
        "delivery": "immediate",
        "enabled": True,
        "last_hash": "abc123",
        "last_summary": "要約",
        "last_checked_at": _stamp(NOW),
        "last_changed_at": _stamp(NOW),
        "last_error": "",
        "error_count": 0,
        "first_error_at": "",
        "created_at": _stamp(NOW - timedelta(days=1)),
    }
    record.update(overrides)
    return record


def _job_record(**overrides) -> dict:
    record = {
        "id": "1",
        "title": "ジョブ",
        "prompt": "指示文",
        "schedule": {"kind": "daily", "time": "07:00"},
        "delivery": "immediate",
        "enabled": True,
        "last_run_at": _stamp(NOW),
        "created_at": _stamp(NOW - timedelta(days=1)),
    }
    record.update(overrides)
    return record


def _write_watches(data_dir: Path, records: list[dict]) -> None:
    (data_dir / "watches.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")


def _write_jobs(data_dir: Path, records: list[dict]) -> None:
    (data_dir / "jobs.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")


def _enqueue(data_dir: Path, occurred: datetime,
             event_id: str = "ev1") -> None:
    event = QueueEvent(
        id=event_id,
        recorded_at=_stamp(occurred),
        occurred_at=_stamp(occurred),
        source_kind="watch",
        source_id="9",
        source_title="何かの見張り",
        title="見張り「何かの見張り」で変化がありました",
    )
    assert enqueue_event(data_dir, event)


def _feed_texts(data_dir: Path) -> list:
    return load_feed_entries(data_dir)


def _audit_entries(data_dir: Path) -> list:
    return [e for e in load_feed_entries(data_dir)
            if e.title == "定期実行の棚卸し"]


def _state(data_dir: Path) -> dict:
    return json.loads(
        (data_dir / STATE_FILENAME).read_text(encoding="utf-8"))


def _error_watch(**overrides) -> dict:
    base = {
        "title": "価格チェック",
        "error_count": 3,
        "first_error_at": _stamp(NOW - timedelta(hours=25)),
        "last_error": "ページを取得できませんでした",
    }
    base.update(overrides)
    return _watch_record(**base)


# ---------------------------------------------------------- 印1: 失敗続き

def test_error_streak_mark(tmp_path: Path):
    _write_watches(tmp_path, [_error_watch()])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None
    assert [m.kind for m in result.marks] == ["watch_error_streak"]
    mark = result.marks[0]
    assert mark.detail["error_count"] == 3
    entries = _audit_entries(tmp_path)
    assert len(entries) == 1
    assert "価格チェック" in entries[0].body
    assert "3 回続けて失敗" in entries[0].body
    # 文面に対象（URL）そのものは写さない
    assert "example.com" not in entries[0].body
    assert _state(tmp_path)["last_audit_on"] == "2026-11-15"


def test_error_streak_boundaries(tmp_path: Path):
    # 回数が足りない
    _write_watches(tmp_path, [_error_watch(error_count=2)])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None and result.marks == []
    # 24 時間に届いていない
    _write_watches(tmp_path, [_error_watch(
        first_error_at=_stamp(DEC - timedelta(hours=23)))])
    result = audit_fleet(tmp_path, now=DEC)
    assert result is not None and result.marks == []
    # ちょうど 24 時間で印が付く
    _write_watches(tmp_path, [_error_watch(
        first_error_at=_stamp(DEC - timedelta(hours=24)))])
    result = audit_fleet(tmp_path, now=JAN)
    assert result is not None
    assert [m.kind for m in result.marks] == ["watch_error_streak"]


def test_error_streak_ignores_disabled_and_unrecorded(tmp_path: Path):
    _write_watches(tmp_path, [
        _error_watch(id="1", enabled=False),
        _error_watch(id="2", title="記録なし", first_error_at=""),
    ])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None and result.marks == []


# ---------------------------------------------------------- 印2: 変化なし

def test_no_change_mark(tmp_path: Path):
    _write_watches(tmp_path, [_watch_record(
        title="リリースノート", interval_hours=24,
        last_checked_at=_stamp(NOW - timedelta(hours=1)),
        last_changed_at=_stamp(NOW - timedelta(days=32)))])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None
    assert [m.kind for m in result.marks] == ["watch_no_change"]
    assert "32 日間変化がありません" in result.marks[0].text


def test_no_change_uses_created_at_when_never_changed(tmp_path: Path):
    _write_watches(tmp_path, [_watch_record(
        interval_hours=24,
        last_checked_at=_stamp(NOW - timedelta(hours=1)),
        last_changed_at="",
        created_at=_stamp(NOW - timedelta(days=31)))])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None
    assert [m.kind for m in result.marks] == ["watch_no_change"]


def test_no_change_boundaries(tmp_path: Path):
    base = {
        "interval_hours": 24,
        "last_checked_at": _stamp(NOW - timedelta(hours=1)),
    }
    # 29 日では足りない
    _write_watches(tmp_path, [_watch_record(
        **base, last_changed_at=_stamp(NOW - timedelta(days=29)))])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None and result.marks == []
    # 確認自体が止まっている（間隔 24 時間の窓は 49 時間）ものは対象外
    _write_watches(tmp_path, [_watch_record(
        **{**base, "last_checked_at": _stamp(NOW - timedelta(hours=50))},
        last_changed_at=_stamp(NOW - timedelta(days=40)))])
    result = audit_fleet(tmp_path, now=DEC)
    assert result is not None and result.marks == []
    # 直近の確認が失敗しているものは、変化なしではなく失敗側の担当
    _write_watches(tmp_path, [_watch_record(
        **base, last_error="失敗しました",
        last_changed_at=_stamp(NOW - timedelta(days=40)))])
    result = audit_fleet(tmp_path, now=JAN)
    assert result is not None and result.marks == []


# ---------------------------------------------------------- 印3: 見張りの重なり

def test_duplicate_target_mark(tmp_path: Path):
    _write_watches(tmp_path, [
        _watch_record(id="1", title="価格A"),
        _watch_record(id="2", title="価格B"),
        _watch_record(id="3", title="別対象",
                      target="https://example.com/other"),
    ])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None
    assert [m.kind for m in result.marks] == ["watch_duplicate_target"]
    mark = result.marks[0]
    assert "価格A" in mark.text and "価格B" in mark.text
    assert "間隔も届け方も同じです" in mark.text
    assert "example.com" not in mark.text  # 対象そのものは写さない


def test_duplicate_target_not_marked(tmp_path: Path):
    # 片方が止まっている
    _write_watches(tmp_path, [
        _watch_record(id="1"),
        _watch_record(id="2", enabled=False),
    ])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None and result.marks == []
    # 種別が違えば別の見張り
    _write_watches(tmp_path, [
        _watch_record(id="1"),
        _watch_record(id="2", kind="search"),
    ])
    result = audit_fleet(tmp_path, now=DEC)
    assert result is not None and result.marks == []


# ---------------------------------------------------------- 印4: 時刻の重なり

def test_job_time_clash_mark(tmp_path: Path):
    _write_jobs(tmp_path, [
        _job_record(id="1", title="朝の天気", prompt="天気を調べて"),
        _job_record(id="2", title="朝の予定", prompt="予定を確認して"),
        _job_record(id="3", title="昼の予定",
                    schedule={"kind": "daily", "time": "12:00"}),
    ])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None
    assert [m.kind for m in result.marks] == ["job_time_clash"]
    mark = result.marks[0]
    assert "朝の天気" in mark.text and "朝の予定" in mark.text
    assert "毎日 07:00" in mark.text
    # 指示文が違う組では「重複登録の疑い」は添えない
    assert "重複して登録" not in mark.text


def test_job_time_clash_same_prompt_is_stronger(tmp_path: Path):
    _write_jobs(tmp_path, [
        _job_record(id="1", title="天気A", prompt="天気を調べて"),
        _job_record(id="2", title="天気B", prompt="天気を調べて"),
    ])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None
    assert "重複して登録されている可能性があります" in result.marks[0].text
    # 指示文そのものは文面に写さない
    assert "天気を調べて" not in result.marks[0].text


def test_job_time_clash_variants(tmp_path: Path):
    # weekly は曜日＋時刻が同じときだけ
    _write_jobs(tmp_path, [
        _job_record(id="1", schedule={
            "kind": "weekly", "weekday": 0, "time": "09:00"}),
        _job_record(id="2", schedule={
            "kind": "weekly", "weekday": 1, "time": "09:00"}),
    ])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None and result.marks == []
    # interval は対象外、daily と weekly の間も対象外
    _write_jobs(tmp_path, [
        _job_record(id="1", schedule={"kind": "interval", "hours": 6}),
        _job_record(id="2", schedule={"kind": "interval", "hours": 6}),
        _job_record(id="3", schedule={
            "kind": "weekly", "weekday": 0, "time": "07:00"}),
        _job_record(id="4", schedule={"kind": "daily", "time": "07:00"}),
    ])
    result = audit_fleet(tmp_path, now=DEC)
    assert result is not None and result.marks == []
    # weekly 同士の重なり
    _write_jobs(tmp_path, [
        _job_record(id="1", schedule={
            "kind": "weekly", "weekday": 3, "time": "18:30"}),
        _job_record(id="2", schedule={
            "kind": "weekly", "weekday": 3, "time": "18:30"}),
    ])
    result = audit_fleet(tmp_path, now=JAN)
    assert result is not None
    assert [m.kind for m in result.marks] == ["job_time_clash"]


# ---------------------------------------------------------- 印5: 未配信の滞留

def test_queue_stale_mark(tmp_path: Path):
    _write_jobs(tmp_path, [_job_record()])  # 対象が 1 件は要る
    _enqueue(tmp_path, NOW - timedelta(days=4))
    _enqueue(tmp_path, NOW - timedelta(days=1), event_id="ev2")
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None
    assert [m.kind for m in result.marks] == ["queue_stale"]
    mark = result.marks[0]
    assert mark.detail["count"] == 2
    assert "2 件、まだ届いていません" in mark.text


def test_queue_stale_boundary(tmp_path: Path):
    _write_jobs(tmp_path, [_job_record()])
    _enqueue(tmp_path, NOW - timedelta(days=2))
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None and result.marks == []


def test_queue_stale_exactly_three_days(tmp_path: Path):
    _write_jobs(tmp_path, [_job_record()])
    _enqueue(tmp_path, NOW - timedelta(days=3))
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None
    assert [m.kind for m in result.marks] == ["queue_stale"]
    assert result.marks[0].detail["days"] == 3


# ---------------------------------------------------------- 頻度と状態

def test_monthly_frequency(tmp_path: Path):
    _write_watches(tmp_path, [_watch_record()])
    assert audit_fleet(tmp_path, now=NOW) is not None
    # 同じ月は二度実施しない
    assert audit_fleet(tmp_path, now=NOW + timedelta(days=1)) is None
    assert audit_fleet(tmp_path, now=datetime(2026, 11, 30)) is None
    # 月が変われば実施する
    assert audit_fleet(tmp_path, now=datetime(2026, 12, 1)) is not None


def test_no_targets_not_recorded(tmp_path: Path):
    assert audit_fleet(tmp_path, now=NOW) is None
    assert not (tmp_path / STATE_FILENAME).exists()


def test_no_marks_records_month_without_output(tmp_path: Path):
    _write_watches(tmp_path, [_watch_record()])
    _write_jobs(tmp_path, [_job_record()])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None
    assert result.marks == [] and result.feed_text == ""
    assert _audit_entries(tmp_path) == []
    assert _state(tmp_path)["last_audit_on"] == "2026-11-15"


def test_audit_disabled_by_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KUMA_FLEET_AUDIT", "0")
    _write_watches(tmp_path, [_error_watch()])
    assert audit_fleet(tmp_path, now=NOW) is None
    assert _audit_entries(tmp_path) == []


def test_fingerprint_suppression_and_worsening(tmp_path: Path):
    _write_watches(tmp_path, [_error_watch()])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None and len(result.marks) == 1
    assert len(_audit_entries(tmp_path)) == 1
    # 翌月、指紋（失敗回数）が同じなら繰り返さない
    _write_watches(tmp_path, [_error_watch(
        first_error_at=_stamp(DEC - timedelta(hours=25)))])
    result = audit_fleet(tmp_path, now=DEC)
    assert result is not None and result.marks == []
    assert len(_audit_entries(tmp_path)) == 1
    assert _state(tmp_path)["reported"][
        "watch_error_streak:1"]["on"] == "2026-11-15"
    # 悪化（回数が増えた）ら改めて出す
    _write_watches(tmp_path, [_error_watch(
        error_count=5,
        first_error_at=_stamp(JAN - timedelta(hours=25)))])
    result = audit_fleet(tmp_path, now=JAN)
    assert result is not None and len(result.marks) == 1
    assert len(_audit_entries(tmp_path)) == 2


def test_reported_cleanup_when_resolved(tmp_path: Path):
    _write_watches(tmp_path, [_error_watch()])
    assert audit_fleet(tmp_path, now=NOW) is not None
    assert _state(tmp_path)["reported"] != {}
    # 直った（失敗の記録が消えた）ら、記録も掃除される
    _write_watches(tmp_path, [_watch_record(title="価格チェック")])
    result = audit_fleet(tmp_path, now=DEC)
    assert result is not None and result.marks == []
    assert _state(tmp_path)["reported"] == {}


# ---------------------------------------------------------- 印ではない提示

def test_feed_includes_disabled_and_summary(tmp_path: Path):
    _write_watches(tmp_path, [
        _error_watch(id="1"),
        _watch_record(id="2", title="止めた見張り", enabled=False),
    ])
    _write_jobs(tmp_path, [
        _job_record(id="1", title="動くジョブ", delivery="digest"),
        _job_record(id="2", title="止めたジョブ", enabled=False),
    ])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None and len(result.marks) == 1
    body = _audit_entries(tmp_path)[0].body
    assert "止めたままのもの:" in body
    assert "見張り「止めた見張り」" in body
    assert "定期ジョブ「止めたジョブ」" in body
    assert "見張り 2 件（有効 1 件・止めている 1 件）" in body
    assert "定期ジョブ 2 件（有効 1 件・止めている 1 件）" in body
    assert "朝のまとめに寄せる 1 件" in body  # 届け方の内訳
    assert "未配信の知らせ: 0 件" in body


# ---------------------------------------------------------- 朝のまとめ材料

def test_pending_fleet_text_lifecycle(tmp_path: Path):
    _write_watches(tmp_path, [_error_watch()])
    result = audit_fleet(tmp_path, now=NOW)
    assert result is not None and result.briefing_text
    text = pending_fleet_text(tmp_path, now=NOW)
    assert text is not None
    assert "見直しをおすすめするものが 1 件あります" in text
    assert "詳細はフィードの「定期実行の棚卸し」" in text
    # 翌日は当日分でなくなる
    assert pending_fleet_text(
        tmp_path, now=NOW + timedelta(days=1)) is None
    # 取り下げたら当日でも出ない
    mark_fleet_briefing_used(tmp_path, now=NOW)
    assert pending_fleet_text(tmp_path, now=NOW) is None


# ---------------------------------------------------------- 道具とボード

def test_set_interval_preserves_baseline(tmp_path: Path):
    board = WatchBoard(tmp_path)
    watch = board.add("価格", "web", "https://example.com", 1)
    board.mark_checked(watch.id, NOW, hash="deadbeef",
                       summary="要約", changed=True)
    updated = board.set_interval(watch.id, 12)
    assert updated is not None and updated.interval_hours == 12
    saved = WatchBoard(tmp_path).get(watch.id)
    assert saved is not None
    assert saved.interval_hours == 12
    # 基準と確認・変化の記録はそのまま
    assert saved.last_hash == "deadbeef"
    assert saved.last_summary == "要約"
    assert saved.last_checked_at == _stamp(NOW)
    assert saved.last_changed_at == _stamp(NOW)
    assert board.set_interval("無い見張り", 3) is None


def test_set_schedule_preserves_last_run(tmp_path: Path):
    board = JobBoard(tmp_path)
    job = board.add("天気", "天気を調べる",
                    {"kind": "daily", "time": "07:00"})
    board.mark_run(job.id, when=NOW)
    updated = board.set_schedule(
        job.id, {"kind": "weekly", "weekday": 2, "time": "10:30"})
    assert updated is not None
    saved = JobBoard(tmp_path).get(job.id)
    assert saved is not None
    assert saved.schedule == {"kind": "weekly", "weekday": 2,
                              "time": "10:30"}
    # 前回実行の記録はそのまま（意図しない即時実行を招かない）
    assert saved.last_run_at == _stamp(NOW)
    assert board.set_schedule("無いジョブ",
                              {"kind": "daily", "time": "08:00"}) is None


def _tool_box(tmp_path: Path) -> ToolBox:
    return ToolBox(tmp_path, watch_board=WatchBoard(tmp_path),
                   job_board=JobBoard(tmp_path))


def test_tool_set_watch_interval(tmp_path: Path):
    board = WatchBoard(tmp_path)
    watch = board.add("価格", "web", "https://example.com", 1)
    board.mark_checked(watch.id, NOW, hash="deadbeef", summary="要約")
    box = _tool_box(tmp_path)
    result = box.call("set_watch_interval",
                      {"watch": "価格", "interval_hours": 6})
    assert "更新しました" in result and "6 時間ごと" in result
    saved = WatchBoard(tmp_path).get(watch.id)
    assert saved is not None and saved.interval_hours == 6
    assert saved.last_hash == "deadbeef"  # 基準は消えない
    # 間隔の検証（1 以上）は登録時と同じ
    assert box.call("set_watch_interval",
                    {"watch": "価格", "interval_hours": 0}
                    ).startswith("error:")
    assert box.call("set_watch_interval",
                    {"watch": "無い", "interval_hours": 3}
                    ).startswith("error:")


def test_tool_set_job_schedule(tmp_path: Path):
    board = JobBoard(tmp_path)
    job = board.add("天気", "天気を調べる",
                    {"kind": "daily", "time": "07:00"})
    board.mark_run(job.id, when=NOW)
    box = _tool_box(tmp_path)
    result = box.call("set_job_schedule",
                      {"job": "天気", "kind": "weekly",
                       "weekday": 2, "time": "10:30"})
    assert "更新しました" in result and "毎週水曜 10:30" in result
    saved = JobBoard(tmp_path).get(job.id)
    assert saved is not None
    assert saved.schedule["kind"] == "weekly"
    assert saved.last_run_at == _stamp(NOW)
    # 種別に合わない組み合わせはエラー
    assert box.call("set_job_schedule",
                    {"job": "天気", "kind": "daily", "weekday": 1}
                    ).startswith("error:")
    assert box.call("set_job_schedule",
                    {"job": "無い", "kind": "daily", "time": "08:00"}
                    ).startswith("error:")


def test_new_tools_are_not_read_only():
    # 書き込み系なので、調べ役の分類表には入れない（通常役のみ）
    assert "set_watch_interval" not in READ_ONLY_TOOLS
    assert "set_job_schedule" not in READ_ONLY_TOOLS


# ---------------------------------------------------------- 巡回への組み込み

class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "応答です")


class _SpyExecutor:
    """朝のまとめの子の差し替え。指示文を記録し、決めた結果を返す。"""

    def __init__(self, result: str = "おはようございます。今日のまとめです。"):
        self.result = result
        self.prompts: list[str] = []

    def __call__(self, parent, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.result


def test_check_and_post_includes_fleet_in_briefing(tmp_path: Path):
    now = datetime(2026, 11, 15, 9, 0, 0)
    _write_watches(tmp_path, [_watch_record(
        title="価格チェック", kind="file", target="x.txt",
        interval_hours=24,
        last_checked_at=_stamp(now),
        last_changed_at=_stamp(now),
        last_error="失敗しました",
        error_count=3,
        first_error_at=_stamp(now - timedelta(days=2)),
        created_at=_stamp(now - timedelta(days=10)))])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()
    text = check_and_post(cfg, now=now, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == spy.result
    assert len(spy.prompts) == 1
    assert "定期実行の棚卸し:" in spy.prompts[0]
    assert "見直しをおすすめするものが 1 件あります" in spy.prompts[0]
    # 詳細はフィードへ残り、材料は投稿されたので取り下げられる
    assert len(_audit_entries(tmp_path)) == 1
    assert pending_fleet_text(tmp_path, now=now) is None
    # 同じ日の 2 回目は、棚卸しもまとめも二度は出ない
    assert check_and_post(cfg, now=now,
                          session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert len(spy.prompts) == 1
    assert len(_audit_entries(tmp_path)) == 1


def test_check_and_post_survives_fleet_failure(
        tmp_path: Path, monkeypatch):
    def _boom(user_data_dir, now=None):
        raise RuntimeError("棚卸しの失敗")

    monkeypatch.setattr(proactive_mod, "audit_fleet", _boom)
    cfg = Config(data_dir=tmp_path)
    # 棚卸しが例外を出しても、巡回全体は落ちずに終わる
    assert check_and_post(cfg, now=NOW) is None
