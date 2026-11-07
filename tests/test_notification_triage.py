"""通知の取捨（見張りの届け方の振り分け、0.84.0）のテスト。

見張りの delivery（immediate / digest）の保存と検証、道具での
指定・変更・表示、digest の変化が即時投稿されず置き場
（notification_queue.jsonl）に溜まること、朝のまとめの材料に
古い順最大5件入って投稿後に配信済みになること、溢れた分が
未配信で残ること、朝のまとめの失敗・「なし」・無効・モデル
なしでは日報の組み立てに節が入ること、置き場への保存が失敗
したら即時へ倒れて投稿できた場合だけ基準が進むこと、即時の
知らせ文は内部専用の知らせ文作成役（道具なし）が作ること、
内部の型を delegate 道具からは選べないことを確認する。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import kuma_agent.proactive as proactive_mod
import kuma_agent.watches as watches_mod
from kuma_agent.config import Config
from kuma_agent.delegate import DELEGATE_NOTICE, run_delegated_task
from kuma_agent.feed import load_feed_entries
from kuma_agent.llm import Message
from kuma_agent.notification_triage import (
    QueueEvent,
    auto_cleanup_queue,
    build_digest_material,
    cleanup_queue,
    enqueue_event,
    load_events,
    mark_delivered,
    pending_events,
)
from kuma_agent.proactive import check_and_post, run_due_watches
from kuma_agent.session import Session
from kuma_agent.tools import activity_label
from kuma_agent.watches import WatchBoard, validate_delivery

NOW = datetime(2026, 11, 7, 12, 0, 0)
MORNING = datetime(2026, 11, 7, 8, 0, 0)


def _pin_created_at(data_dir: Path, when: datetime) -> None:
    """保存済みの見張りの作成時刻を固定する。

    WatchBoard.add は実時計で作成時刻を刻むため、そのままだと
    固定した NOW との期限判定（作成時刻＋間隔）が実時計の進み
    方に依存してしまう。種を作った直後に作成時刻を NOW 基準
    へ寄せ、判定を実時計から切り離す（定期ジョブのテストが
    jobs.json に作成時刻を明示して用意する流儀と同じ）。
    """
    path = data_dir / "watches.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    for record in records:
        record["created_at"] = when.isoformat(timespec="seconds")
    path.write_text(json.dumps(records, ensure_ascii=False),
                    encoding="utf-8")


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


class _FakeSession:
    """run_due_watches が使う範囲だけを持つ Session の代わり。"""

    def __init__(self, model=None):
        self.model = model if model is not None else object()
        self.posted: list[str] = []

    def post_proactive_message(self, text: str) -> None:
        self.posted.append(text)


class _SpyExecutor:
    """朝のまとめの子の差し替え。指示文を記録し、決めた結果を返す。"""

    def __init__(self, result: str = "おはようございます。朝の知らせです。",
                 exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.prompts: list[str] = []

    def __call__(self, parent, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.result


def _seed_watch(tmp_path: Path, monkeypatch, text: str,
                delivery: str = "immediate") -> dict:
    """本文を差し替えられる web の見張りを用意する（基準は未記録）。"""
    state = {"text": text}
    monkeypatch.setattr(watches_mod, "fetch_page", lambda url: state["text"])
    board = WatchBoard(tmp_path)
    board.add("ページ", "web", "https://example.com", interval_hours=1,
              delivery=delivery)
    # NOW の時点で最初の確認が来ているよう、作成時刻を固定する
    _pin_created_at(tmp_path, NOW - timedelta(hours=2))
    return state


def _write_tasks(user_dir: Path, titles: list[str]) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    stamp = MORNING.isoformat(timespec="seconds")
    tasks = [{"id": str(i + 1), "title": title, "status": "open",
              "memo": "", "created_at": stamp, "updated_at": stamp}
             for i, title in enumerate(titles)]
    (user_dir / "tasks.json").write_text(
        json.dumps(tasks, ensure_ascii=False), encoding="utf-8")


def _make_event(event_id: str, title: str, recorded: datetime,
                new_summary: str = "今回の要約") -> QueueEvent:
    stamp = recorded.isoformat(timespec="seconds")
    return QueueEvent(
        id=event_id, recorded_at=stamp, occurred_at=stamp,
        source_kind="watch", source_id="1", source_title=title,
        title=f"見張り「{title}」で変化がありました",
        old_summary="前回の要約", new_summary=new_summary,
        decision="digest", decision_by="setting",
        decision_reason="watch_setting_digest")


# ------------------------------------------------------------ 届け方の保存と検証

def test_validate_delivery():
    assert validate_delivery("immediate") == "immediate"
    assert validate_delivery(" digest ") == "digest"
    # auto は第2段（0.85.0）で受け付けるようになった
    assert validate_delivery("auto") == "auto"
    assert validate_delivery(None) == "immediate"
    assert validate_delivery("") == "immediate"
    for bad in ("later", "DIGESTS"):
        with pytest.raises(ValueError):
            validate_delivery(bad)


def test_stored_delivery_defaults(tmp_path: Path):
    path = tmp_path / "watches.json"
    # 旧形式（delivery なし）は immediate として読む
    path.write_text(json.dumps([
        {"id": "1", "title": "旧形式", "kind": "search", "target": "猫"},
        {"id": "2", "title": "朝へ", "kind": "search", "target": "犬",
         "delivery": "digest"},
        {"id": "3", "title": "壊れた値", "kind": "search", "target": "鳥",
         "delivery": "later"},
    ]), encoding="utf-8")
    watches = WatchBoard(tmp_path).list()
    # 不正な値でも見張り自体は落とさず、immediate として読む
    assert [(w.title, w.delivery) for w in watches] == [
        ("旧形式", "immediate"), ("朝へ", "digest"), ("壊れた値", "immediate")]


def test_board_set_delivery(tmp_path: Path):
    board = WatchBoard(tmp_path)
    board.add("価格", "web", "https://example.com")
    updated = board.set_delivery("価格", "digest")
    assert updated is not None and updated.delivery == "digest"
    assert WatchBoard(tmp_path).list()[0].delivery == "digest"
    assert board.set_delivery("なし", "digest") is None


# ------------------------------------------------------------ 道具

def test_tools_delivery_roundtrip(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    names = {s["name"] for s in session.tools.schemas()}
    assert "set_watch_delivery" in names

    result = session.tools.call("create_watch", {
        "title": "価格", "kind": "web", "target": "https://example.com",
        "delivery": "digest"})
    assert result.startswith("登録しました: #1 価格")
    assert "朝のまとめに寄せる" in result
    # auto は第2段（0.85.0）から道具でも受け付ける
    auto_result = session.tools.call("create_watch", {
        "title": "天気", "kind": "web", "target": "https://example.com",
        "delivery": "auto"})
    assert auto_result.startswith("登録しました")
    assert "おまかせ" in auto_result
    # 知らない届け方は error のまま
    assert session.tools.call("create_watch", {
        "title": "x", "kind": "web", "target": "https://example.com",
        "delivery": "later"}).startswith("error:")

    listing = session.tools.call("list_watches", {})
    assert "知らせ方: 朝のまとめに寄せる" in listing
    assert "知らせ方: おまかせ" in listing

    updated = session.tools.call(
        "set_watch_delivery", {"watch": "価格", "delivery": "immediate"})
    assert updated.startswith("更新しました")
    assert "その場で知らせる" in updated
    updated_auto = session.tools.call(
        "set_watch_delivery", {"watch": "価格", "delivery": "auto"})
    assert updated_auto.startswith("更新しました")
    assert "おまかせ" in updated_auto
    assert session.tools.call(
        "set_watch_delivery", {"watch": "価格", "delivery": "later"}
    ).startswith("error:")
    assert session.tools.call(
        "set_watch_delivery", {"watch": "なし", "delivery": "digest"}
    ).startswith("error:")
    assert activity_label("set_watch_delivery") == "見張りを記録中"


# ------------------------------------------------------------ 置き場の部品

def test_queue_enqueue_dedupe_and_deliver(tmp_path: Path):
    event = _make_event("watch:1:t:h1", "価格", NOW)
    assert enqueue_event(tmp_path, event)
    # 同じ変化の再検出（同じ ID）は二重に入らない
    assert enqueue_event(tmp_path, _make_event("watch:1:t:h1", "価格", NOW))
    assert len(load_events(tmp_path)) == 1
    assert [e.id for e in pending_events(tmp_path)] == ["watch:1:t:h1"]
    assert mark_delivered(tmp_path, ["watch:1:t:h1"], via="briefing",
                          now=NOW) == 1
    assert pending_events(tmp_path) == []
    delivered = load_events(tmp_path)[0]
    assert delivered.status == "delivered"
    assert delivered.delivered_via == "briefing"
    assert delivered.delivered_on == "2026-11-07"
    # 既に配信済みのものへは重ねない
    assert mark_delivered(tmp_path, ["watch:1:t:h1"], via="daily",
                          now=NOW) == 0


def test_queue_cleanup_keeps_pending(tmp_path: Path):
    old = NOW - timedelta(days=200)
    enqueue_event(tmp_path, _make_event("watch:1:t:old-pending", "古い未配信", old))
    enqueue_event(tmp_path, _make_event("watch:1:t:old-done", "古い配信済み", old))
    mark_delivered(tmp_path, ["watch:1:t:old-done"], via="daily", now=old)
    enqueue_event(tmp_path, _make_event("watch:1:t:new-done", "新しい配信済み", NOW))
    mark_delivered(tmp_path, ["watch:1:t:new-done"], via="daily", now=NOW)
    removed = cleanup_queue(tmp_path, now=NOW)
    assert removed == 1
    remaining = {e.id for e in load_events(tmp_path)}
    # 未配信はどれだけ古くても残り、新しい配信済みも残る
    assert remaining == {"watch:1:t:old-pending", "watch:1:t:new-done"}


def test_queue_auto_cleanup_once_a_day(tmp_path: Path):
    old = NOW - timedelta(days=200)
    enqueue_event(tmp_path, _make_event("watch:1:t:old-done", "古い", old))
    mark_delivered(tmp_path, ["watch:1:t:old-done"], via="daily", now=old)
    assert auto_cleanup_queue(tmp_path, now=NOW) == 1
    # 同じ日は二度掃除しない
    assert auto_cleanup_queue(tmp_path, now=NOW) == 0


def test_digest_material_dates_and_clipping(tmp_path: Path):
    long_summary = "あ" * 400
    recent = _make_event("e1", "今日の見張り", NOW, long_summary)
    material = build_digest_material([recent], NOW)
    assert material is not None
    assert "今日の見張り" in material
    # 材料では要約は160字へ切り詰める（置き場の原本は保持する）
    assert "あ" * 160 in material and "あ" * 161 not in material

    two_days = _make_event("e2", "一昨日の見張り", NOW - timedelta(days=2))
    material = build_digest_material([two_days], NOW) or ""
    assert "（11月5日）" in material
    eight_days = _make_event("e3", "先週の見張り", NOW - timedelta(days=8))
    material = build_digest_material([eight_days], NOW) or ""
    assert "（8日前）" in material
    assert build_digest_material([], NOW) is None


# ------------------------------------------------------------ digest の巡回

def test_digest_change_is_queued_not_posted(tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文", delivery="digest")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    notified: list[str] = []
    # 最初の確認は基準の記録だけ（置き場にも入らない）
    assert run_due_watches(cfg, NOW, lambda: session,
                           watch_executor=lambda s, p: "知らせ文",
                           notify=lambda t, u, x: notified.append(x)) == []
    assert pending_events(tmp_path) == []
    # 変化なしでも置き場には入らない
    assert run_due_watches(cfg, NOW + timedelta(hours=2), lambda: session,
                           watch_executor=lambda s, p: "知らせ文") == []
    assert pending_events(tmp_path) == []

    state["text"] = "新しい本文です"
    later = NOW + timedelta(hours=4)
    posted = run_due_watches(cfg, later, lambda: session,
                             watch_executor=lambda s, p: "知らせ文",
                             notify=lambda t, u, x: notified.append(x))
    # その場では会話にも外部通知にも出ない
    assert posted == [] and session.posted == [] and notified == []
    pending = pending_events(tmp_path)
    assert len(pending) == 1
    event = pending[0]
    assert event.source_title == "ページ"
    assert "前の本文" in event.old_summary
    assert "新しい本文です" in event.new_summary
    assert event.decision_reason == "watch_setting_digest"
    # 発生時の短い記録がフィードに残る
    feed = load_feed_entries(tmp_path)
    assert any("朝の知らせでまとめてお知らせします" in e.body for e in feed)
    # 基準は更新されている（同じ変化は二重に入らない）
    watch = WatchBoard(tmp_path).list()[0]
    assert watch.last_changed_at == later.isoformat(timespec="seconds")
    assert run_due_watches(cfg, later + timedelta(hours=2), lambda: session,
                           watch_executor=lambda s, p: "知らせ文") == []
    assert len(pending_events(tmp_path)) == 1


def test_queue_failure_falls_back_to_immediate(tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文", delivery="digest")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    run_due_watches(cfg, NOW, lambda: session,
                    watch_executor=lambda s, p: "知らせ文")
    state["text"] = "新しい本文です"
    monkeypatch.setattr(proactive_mod, "enqueue_event",
                        lambda *a, **k: False)
    later = NOW + timedelta(hours=2)
    posted = run_due_watches(cfg, later, lambda: session,
                             watch_executor=lambda s, p: "値段が下がりました")
    # 置き場へ保存できなければ即時に倒し、投稿できたので基準は進む
    assert len(posted) == 1
    assert "値段が下がりました" in posted[0]
    assert pending_events(tmp_path) == []
    watch = WatchBoard(tmp_path).list()[0]
    assert watch.last_changed_at == later.isoformat(timespec="seconds")


def test_wall_mode_and_disabled_fall_back_to_immediate(
        tmp_path: Path, monkeypatch):
    # スレッドモードでは朝の知らせの経路が無いため即時に倒す
    state = _seed_watch(tmp_path, monkeypatch, "前の本文", delivery="digest")
    cfg = Config(data_dir=tmp_path, mode="wall")
    session = _FakeSession()
    run_due_watches(cfg, NOW, lambda: session,
                    watch_executor=lambda s, p: "知らせ文")
    state["text"] = "新しい本文です"
    posted = run_due_watches(cfg, NOW + timedelta(hours=2), lambda: session,
                             watch_executor=lambda s, p: "知らせ文")
    assert len(posted) == 1 and pending_events(tmp_path) == []

    # 振り分けの無効化中（KUMA_NOTIFICATION_TRIAGE=0）も即時に倒す
    state2 = _seed_watch(tmp_path / "off", monkeypatch, "前の本文",
                         delivery="digest")
    cfg2 = Config(data_dir=tmp_path / "off",
                  notification_triage_enabled=False)
    session2 = _FakeSession()
    run_due_watches(cfg2, NOW, lambda: session2,
                    watch_executor=lambda s, p: "知らせ文")
    state2["text"] = "新しい本文です"
    posted2 = run_due_watches(cfg2, NOW + timedelta(hours=2),
                              lambda: session2,
                              watch_executor=lambda s, p: "知らせ文")
    assert len(posted2) == 1
    assert pending_events(tmp_path / "off") == []


# ------------------------------------------------------------ 朝の知らせでの配信

def test_digest_delivered_by_next_briefing(tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文", delivery="digest")
    _write_tasks(tmp_path, ["買い物"])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()
    day1 = NOW  # 作成時刻は固定済みで、まとめの時刻も過ぎている
    # 1日目の朝のまとめ: 基準の記録と、用事の朝のまとめ
    check_and_post(cfg, now=day1, session_provider=lambda: session,
                   watch_executor=lambda s, p: "知らせ文",
                   briefing_executor=spy)
    assert len(spy.prompts) == 1
    # 夕方に変化 → 置き場へ（その日の声かけ枠は使用済みなので出ない）
    state["text"] = "新しい本文です"
    evening = datetime(2026, 11, 7, 20, 0, 0)
    assert check_and_post(cfg, now=evening, session_provider=lambda: session,
                          watch_executor=lambda s, p: "知らせ文",
                          briefing_executor=spy) is None
    assert len(pending_events(tmp_path)) == 1
    # 翌朝のまとめの材料に入り、投稿後に配信済みになる
    day2 = datetime(2026, 11, 8, 8, 0, 0)
    text = check_and_post(cfg, now=day2, session_provider=lambda: session,
                          watch_executor=lambda s, p: "知らせ文",
                          briefing_executor=spy)
    assert text == spy.result
    assert len(spy.prompts) == 2
    assert "まとめて知らせる変化:" in spy.prompts[1]
    assert "新しい本文です" in spy.prompts[1]
    assert "（11月7日）" in spy.prompts[1]  # 前日の変化は日付つき
    assert pending_events(tmp_path) == []
    delivered = load_events(tmp_path)[0]
    assert delivered.status == "delivered"
    assert delivered.delivered_via == "briefing"
    assert delivered.delivered_on == "2026-11-08"


def test_digest_overflow_stays_pending(tmp_path: Path):
    _write_tasks(tmp_path, ["買い物"])
    for i in range(7):
        enqueue_event(tmp_path, _make_event(
            f"watch:1:t:{i}", f"見張り{i + 1}", NOW + timedelta(minutes=i),
            new_summary=f"変化の内容{i + 1}"))
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()
    text = check_and_post(cfg, now=MORNING, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == spy.result
    prompt = spy.prompts[0]
    # 古い順に最大5件だけが材料に入る
    for i in range(1, 6):
        assert f"見張り{i}" in prompt
    assert "見張り6" not in prompt and "見張り7" not in prompt
    # 溢れた分は消さず未配信のまま残る
    remaining = pending_events(tmp_path)
    assert [e.source_title for e in remaining] == ["見張り6", "見張り7"]


def test_digest_daily_fallback_paths(tmp_path: Path):
    enqueue_event(tmp_path, _make_event("watch:1:t:1", "価格", NOW))
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    # 子が失敗したら、日報の組み立てに節が入って届く（用事なしでも）
    spy = _SpyExecutor(exc=RuntimeError("子が落ちた"))
    text = check_and_post(cfg, now=MORNING, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text is not None
    assert "まとめてのお知らせが 1 件あります。" in text
    assert "見張り「価格」" in text
    assert pending_events(tmp_path) == []
    assert load_events(tmp_path)[0].delivered_via == "daily"


def test_digest_child_says_none_falls_back(tmp_path: Path):
    enqueue_event(tmp_path, _make_event("watch:1:t:1", "価格", NOW))
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor(result="なし")
    text = check_and_post(cfg, now=MORNING, session_provider=lambda: session,
                          briefing_executor=spy)
    # 寄せた変化がある場合の「なし」は採用せず、日報で届ける
    assert text is not None and "まとめてのお知らせが 1 件あります。" in text
    assert pending_events(tmp_path) == []
    state = json.loads(
        (tmp_path / "proactive_state.json").read_text(encoding="utf-8"))
    assert "briefing_checked_on" not in state
    assert state["last_posted_on"] == "2026-11-07"


def test_digest_briefing_off_and_model_free(tmp_path: Path):
    enqueue_event(tmp_path, _make_event("watch:1:t:1", "価格", NOW))
    # 朝のまとめが off でも、日報の組み立てで届く
    cfg = Config(data_dir=tmp_path, briefing_time="off")
    session = Session(cfg, _EchoModel())
    text = check_and_post(cfg, now=MORNING, session_provider=lambda: session)
    assert text is not None and "まとめてのお知らせが 1 件あります。" in text
    assert pending_events(tmp_path) == []

    # モデルなしの一時 Session の経路（CLI の手動巡回）でも届く
    enqueue_event(tmp_path, _make_event("watch:1:t:2", "天気", NOW))
    cfg2 = Config(data_dir=tmp_path)
    text2 = check_and_post(cfg2, now=MORNING + timedelta(days=1))
    assert text2 is not None and "見張り「天気」" in text2
    assert pending_events(tmp_path) == []


def test_digest_pending_survives_quiet_hours_and_used_slot(
        tmp_path: Path, monkeypatch):
    # その日の声かけ枠が使用済みの日は、配信済みにせず持ち越す
    enqueue_event(tmp_path, _make_event("watch:1:t:1", "価格", NOW))
    state_path = tmp_path / "proactive_state.json"
    state_path.write_text(json.dumps({"last_posted_on": "2026-11-07"}),
                          encoding="utf-8")
    _write_tasks(tmp_path, ["買い物"])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()
    assert check_and_post(cfg, now=MORNING, session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert len(pending_events(tmp_path)) == 1
    # 翌日には届く
    text = check_and_post(cfg, now=MORNING + timedelta(days=1),
                          session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == spy.result
    assert pending_events(tmp_path) == []


def test_digest_delivered_even_when_triage_disabled(tmp_path: Path):
    # 無効化中でも、既にある未配信分は読み続けて届ける
    enqueue_event(tmp_path, _make_event("watch:1:t:1", "価格", NOW))
    _write_tasks(tmp_path, ["買い物"])
    cfg = Config(data_dir=tmp_path, notification_triage_enabled=False)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()
    text = check_and_post(cfg, now=MORNING, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == spy.result
    assert "まとめて知らせる変化:" in spy.prompts[0]
    assert pending_events(tmp_path) == []


# ------------------------------------------------------------ 知らせ文作成役

class _NoticeCaptureModel:
    """子のシステムプロンプトと道具を記録し、知らせ本文を返す。"""

    def __init__(self) -> None:
        self.child_prompts: list[str] = []
        self.child_tool_names: list[list[str]] = []

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            self.child_prompts.append(system_prompt)
            self.child_tool_names.append([t["name"] for t in tools])
            return Message("assistant", "知らせ本文です")
        return Message("assistant", "親の応答")


def test_immediate_notice_uses_notice_role(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path / "data")
    # 見張りの置き場は cfg のユーザーの置き場（tmp_path ではない）
    board = WatchBoard(cfg.user_data_dir)
    board.add("ページ", "web", "https://example.com", interval_hours=1)
    _pin_created_at(cfg.user_data_dir, NOW - timedelta(hours=2))
    model = _NoticeCaptureModel()
    session = Session(cfg, model)
    # 基準の記録（実行役は使わない）
    run_due_watches(cfg, NOW, lambda: session,
                    watch_executor=lambda s, p: "知らせ文")
    state["text"] = "新しい本文です"
    # 既定の実行役（知らせ文作成役）で知らせる
    posted = run_due_watches(cfg, NOW + timedelta(hours=2), lambda: session)
    assert len(posted) == 1 and "知らせ本文です" in posted[0]
    # 子には道具が1つも出ず、指示は計画役と分かれている
    assert model.child_tool_names == [[]]
    prompt = model.child_prompts[0]
    assert "知らせ文の作成だけを担当します" in prompt
    assert "計画だけを担当します" not in prompt


def test_internal_role_not_selectable_via_delegate_tool(
        tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path / "data")
    model = _NoticeCaptureModel()
    session = Session(cfg, model)
    # 道具の入り口では内部の型を弾く（スキーマに無いだけに頼らない）
    result = session.tools.call(
        "delegate", {"task": "知らせを作って", "role": "notice"})
    assert result.startswith("error:")
    assert model.child_prompts == []
    # コードからの呼び出しでは使える（子の道具はゼロ）
    result = run_delegated_task(session, "知らせを作って",
                                retry=False, role="notice")
    assert result == "知らせ本文です"
    assert model.child_tool_names == [[]]
