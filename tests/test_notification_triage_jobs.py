"""通知の取捨 第3段（定期ジョブの成功結果の届け方、0.86.0）のテスト。

ジョブの delivery（immediate / digest / auto）の保存と検証、
道具での指定・変更・表示、digest にしたジョブの成功結果が
即時投稿されず置き場（notification_queue.jsonl）に溜まって
朝のまとめで届くこと、朝のまとめの上限（最大5件）が見張りと
ジョブを合わせた出来事の件数で数えられること、即時設定と
既定（旧形式）では現行どおりその場で届くこと、ジョブの失敗は
朝行き・おまかせの設定でも即時に届くこと、auto の判定が
使えない場合は理由を残して即時へ倒れること、置き場への保存
が失敗したら即時へ倒れること、長い結果は置き場で切り詰めて
そのことを記録することを確認する。

あわせて、見張りの確認失敗の連続記録（error_count /
first_error_at）を確認する。これは「繰り返す確認失敗を知ら
せるか」の判断材料として足した記録であり、この段では失敗が
繰り返しても知らせ自体は出ないことも固定する。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import kuma_agent.proactive as proactive_mod
import kuma_agent.watches as watches_mod
from kuma_agent.config import Config
from kuma_agent.feed import load_feed_entries
from kuma_agent.jobs import Job, JobBoard
from kuma_agent.llm import Message
from kuma_agent.notification_triage import (
    MAX_JOB_RESULT_CHARS,
    QueueEvent,
    build_digest_material,
    build_job_event,
    decide_job_route,
    enqueue_event,
    load_events,
    pending_events,
)
from kuma_agent.proactive import (
    build_job_triage_prompt,
    check_and_post,
    run_due_jobs,
    run_due_watches,
)
from kuma_agent.session import Session
from kuma_agent.tools import ToolBox, activity_label
from kuma_agent.watches import WatchBoard

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

DIGEST_JSON = json.dumps(
    {"route": "digest", "certain": True, "reason": "日常的な集計結果です"},
    ensure_ascii=False)
IMMEDIATE_JSON = json.dumps(
    {"route": "immediate", "certain": True,
     "reason": "異常な値が含まれています", "notice": "異常がありました"},
    ensure_ascii=False)
UNCERTAIN_JSON = json.dumps(
    {"route": "digest", "certain": False, "reason": "どちらとも言えません"},
    ensure_ascii=False)


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


class _FakeSession:
    """run_due_jobs が使う範囲だけを持つ Session の代わり。"""

    def __init__(self, model=None):
        self.model = model if model is not None else object()
        self.posted: list[str] = []

    def post_proactive_message(self, text: str) -> None:
        self.posted.append(text)


class _JobExecutor:
    """ジョブ実行役の差し替え。結果を記録し、決めた結果を返す。"""

    def __init__(self, result: str = "ジョブの結果本文",
                 exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.prompts: list[str] = []

    def __call__(self, parent, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.result


class _TriageSpy:
    """判定役の差し替え。指示文を記録し、決めた結果を返す。"""

    def __init__(self, result: str = DIGEST_JSON,
                 exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.prompts: list[str] = []

    def __call__(self, parent, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.result


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


def _write_job(user_dir: Path, delivery: str | None = "digest",
               title: str = "朝のニュース",
               prompt: str = "ニュースをまとめて",
               created_at: str = "2026-11-06T08:00:00") -> None:
    """期限の来た定期ジョブを jobs.json に用意する。

    delivery=None なら項目自体を書かない（旧形式の再現）。
    予定は毎日 09:00 で、NOW（同日 12:00）には期限が来ている。
    """
    user_dir.mkdir(parents=True, exist_ok=True)
    record = {"id": "1", "title": title, "prompt": prompt,
              "schedule": {"kind": "daily", "time": "09:00"},
              "enabled": True, "last_run_at": "",
              "created_at": created_at}
    if delivery is not None:
        record["delivery"] = delivery
    (user_dir / "jobs.json").write_text(
        json.dumps([record], ensure_ascii=False), encoding="utf-8")


def _write_tasks(user_dir: Path, titles: list[str]) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    stamp = MORNING.isoformat(timespec="seconds")
    tasks = [{"id": str(i + 1), "title": title, "status": "open",
              "memo": "", "created_at": stamp, "updated_at": stamp}
             for i, title in enumerate(titles)]
    (user_dir / "tasks.json").write_text(
        json.dumps(tasks, ensure_ascii=False), encoding="utf-8")


def _run_jobs(cfg: Config, session: _FakeSession, **kwargs):
    return run_due_jobs(cfg, NOW, lambda: session, **kwargs)


# ------------------------------------------------------------ 届け方の保存と検証

def test_stored_job_delivery_defaults(tmp_path: Path):
    path = tmp_path / "jobs.json"
    base = {"prompt": "仕事", "schedule": {"kind": "daily", "time": "09:00"},
            "enabled": True, "last_run_at": "", "created_at": "2026-11-06T08:00:00"}
    path.write_text(json.dumps([
        {"id": "1", "title": "旧形式", **base},
        {"id": "2", "title": "朝へ", **base, "delivery": "digest"},
        {"id": "3", "title": "壊れた値", **base, "delivery": "later"},
    ]), encoding="utf-8")
    jobs = JobBoard(tmp_path).list()
    # 旧形式は immediate、不正な値でもジョブ自体は落とさず immediate
    assert [(j.title, j.delivery) for j in jobs] == [
        ("旧形式", "immediate"), ("朝へ", "digest"), ("壊れた値", "immediate")]


def test_board_set_job_delivery(tmp_path: Path):
    board = JobBoard(tmp_path)
    board.add("集計", "集計する", {"kind": "interval", "hours": 6})
    updated = board.set_delivery("集計", "auto")
    assert updated is not None and updated.delivery == "auto"
    assert JobBoard(tmp_path).list()[0].delivery == "auto"
    assert board.set_delivery("なし", "digest") is None


def test_decide_job_route_matrix():
    def job(delivery: str) -> Job:
        return Job(id="1", title="ジョブ", prompt="仕事",
                   schedule={"kind": "daily", "time": "09:00"},
                   delivery=delivery)

    # 相棒モード＋有効: 設定どおり（auto は判定待ちを返す）
    assert decide_job_route(job("digest"), mode="ledger",
                            triage_enabled=True) == ("digest",
                                                     "job_setting_digest")
    assert decide_job_route(job("auto"), mode="ledger",
                            triage_enabled=True) == ("auto", "")
    assert decide_job_route(job("immediate"), mode="ledger",
                            triage_enabled=True) == ("immediate",
                                                     "job_setting_immediate")
    # スレッドモードでは朝の経路が無いため、指定にかかわらず即時
    for delivery in ("digest", "auto"):
        route, reason = decide_job_route(job(delivery), mode="wall",
                                         triage_enabled=True)
        assert (route, reason) == ("immediate", "no_digest_route_immediate")
    # 振り分けの無効化中も即時（判定もしない）
    for delivery in ("digest", "auto"):
        route, reason = decide_job_route(job(delivery), mode="ledger",
                                         triage_enabled=False)
        assert (route, reason) == ("immediate", "triage_disabled_immediate")


# ------------------------------------------------------------ 道具

def test_job_tools_delivery_roundtrip(tmp_path: Path):
    box = ToolBox(tmp_path, job_board=JobBoard(tmp_path))
    out = box.call("create_job", {
        "title": "朝の集計", "prompt": "集計する", "kind": "daily",
        "time": "07:00", "delivery": "digest"})
    assert "登録しました" in out and "朝のまとめに寄せる" in out
    assert JobBoard(tmp_path).list()[0].delivery == "digest"

    # 省略時はその場で知らせる
    out = box.call("create_job", {
        "title": "速報", "prompt": "確認する", "kind": "interval",
        "hours": 3})
    assert "その場で知らせる" in out

    # 一覧に届け方が出る
    listing = box.call("list_jobs", {})
    assert "朝の集計" in listing and "朝のまとめに寄せる" in listing
    assert "速報" in listing and "その場で知らせる" in listing

    # 名前で切り替え、ID でも切り替える
    out = box.call("set_job_delivery",
                   {"job": "朝の集計", "delivery": "auto"})
    assert "更新しました" in out and "おまかせ" in out
    job = JobBoard(tmp_path).find("速報")
    assert job is not None
    out = box.call("set_job_delivery",
                   {"job": job.id, "delivery": "digest"})
    assert "更新しました" in out
    assert JobBoard(tmp_path).find("速報").delivery == "digest"

    # 不正な届け方と、無いジョブは error
    assert box.call("create_job", {
        "title": "x", "prompt": "y", "kind": "daily", "time": "07:00",
        "delivery": "later"}).startswith("error:")
    assert box.call("set_job_delivery", {
        "job": "朝の集計", "delivery": "later"}).startswith("error:")
    assert box.call("set_job_delivery", {
        "job": "ないジョブ", "delivery": "digest"}).startswith("error:")
    assert activity_label("set_job_delivery") == "定期ジョブを記録中"


# ------------------------------------------------------------ digest の巡回

def test_digest_job_result_is_queued_not_posted(tmp_path: Path):
    _write_job(tmp_path, delivery="digest")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    executor = _JobExecutor(result="今日のニュースの要点です")
    notified: list[str] = []
    posted = _run_jobs(cfg, session, job_executor=executor,
                       notify=lambda t, u, x: notified.append(x))
    # その場では会話にも外部通知にも出ない
    assert posted == [] and session.posted == [] and notified == []
    assert executor.prompts == ["ニュースをまとめて"]
    pending = pending_events(tmp_path)
    assert len(pending) == 1
    event = pending[0]
    assert event.source_kind == "job" and event.source_id == "1"
    assert event.source_title == "朝のニュース"
    assert event.result_text == "今日のニュースの要点です"
    assert event.result_truncated is False
    assert event.decision == "digest"
    assert event.decision_by == "setting"
    assert event.decision_reason == "job_setting_digest"
    # 発生時の短い記録がフィードに残る
    feed = load_feed_entries(tmp_path)
    assert any("定期ジョブ「朝のニュース」の結果は、"
               "朝の知らせでまとめてお知らせします" in e.body
               for e in feed)
    # 実行時刻は届け方と独立に記録される（同じ予定で再実行しない）
    job = JobBoard(tmp_path).list()[0]
    assert job.last_run_at == NOW.isoformat(timespec="seconds")
    assert _run_jobs(cfg, session, job_executor=executor) == []
    assert len(pending_events(tmp_path)) == 1


def test_digest_job_delivered_by_next_briefing(tmp_path: Path):
    # 作成は朝のまとめより後（その日の朝の枠はまとめ側で使う）
    _write_job(tmp_path, delivery="digest", created_at="2026-11-07T08:30:00")
    _write_tasks(tmp_path, ["買い物"])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()
    executor = _JobExecutor(result="ニュースの要点です")
    # 1日目の朝のまとめ（ジョブはまだ期限でない）
    text = check_and_post(cfg, now=MORNING, session_provider=lambda: session,
                          job_executor=executor, briefing_executor=spy)
    assert text == spy.result and executor.prompts == []
    # 10時にジョブが実行され、結果は置き場へ（声かけ枠は使用済み）
    later = datetime(2026, 11, 7, 10, 0, 0)
    assert check_and_post(cfg, now=later, session_provider=lambda: session,
                          job_executor=executor,
                          briefing_executor=spy) is None
    assert executor.prompts == ["ニュースをまとめて"]
    assert len(pending_events(tmp_path)) == 1
    # 翌朝のまとめの材料に入り、投稿後に配信済みになる
    day2 = datetime(2026, 11, 8, 8, 0, 0)
    text = check_and_post(cfg, now=day2, session_provider=lambda: session,
                          job_executor=executor, briefing_executor=spy)
    assert text == spy.result
    prompt = spy.prompts[-1]
    assert "まとめて知らせる変化:" in prompt
    assert "定期ジョブ「朝のニュース」の結果: ニュースの要点です" in prompt
    assert "（11月7日）" in prompt  # 前日の結果は日付つき
    assert pending_events(tmp_path) == []
    delivered = load_events(tmp_path)[0]
    assert delivered.status == "delivered"
    assert delivered.delivered_via == "briefing"
    assert delivered.delivered_on == "2026-11-08"


def test_digest_job_delivered_by_daily_fallback(tmp_path: Path):
    # 朝のまとめが off でも、日報の組み立てに節が入って届く
    event = build_job_event(
        Job(id="1", title="集計", prompt="集計する",
            schedule={"kind": "daily", "time": "09:00"}, delivery="digest"),
        "集計の結果です", NOW, "job_setting_digest")
    assert enqueue_event(tmp_path, event)
    cfg = Config(data_dir=tmp_path, briefing_time="off")
    session = Session(cfg, _EchoModel())
    text = check_and_post(cfg, now=MORNING, session_provider=lambda: session)
    assert text is not None
    assert "まとめてのお知らせが 1 件あります。" in text
    assert "定期ジョブ「集計」の結果: 集計の結果です" in text
    assert pending_events(tmp_path) == []
    assert load_events(tmp_path)[0].delivered_via == "daily"


# ------------------------------------------------------------ 上限は見張りとジョブの合算

def _watch_event(event_id: str, title: str, recorded: datetime,
                 summary: str) -> QueueEvent:
    stamp = recorded.isoformat(timespec="seconds")
    return QueueEvent(
        id=event_id, recorded_at=stamp, occurred_at=stamp,
        source_kind="watch", source_id="1", source_title=title,
        title=f"見張り「{title}」で変化がありました",
        old_summary="前回の要約", new_summary=summary,
        decision="digest", decision_by="setting",
        decision_reason="watch_setting_digest")


def _job_event(title: str, recorded: datetime, result: str) -> QueueEvent:
    job = Job(id="1", title=title, prompt="仕事をする",
              schedule={"kind": "daily", "time": "09:00"},
              delivery="digest")
    return build_job_event(job, result, recorded, "job_setting_digest")


def test_briefing_cap_counts_jobs_and_watches_together(tmp_path: Path):
    _write_tasks(tmp_path, ["買い物"])
    # 見張りとジョブを交互に7件（古い順）。上限5件は出来事の
    # 件数で数えるため、種別ごとに5件にはならない
    entries = [
        _watch_event("watch:1:t:0", "価格A", NOW, "変化A"),
        _job_event("集計B", NOW + timedelta(minutes=1), "結果B"),
        _watch_event("watch:1:t:2", "価格C", NOW + timedelta(minutes=2),
                     "変化C"),
        _job_event("集計D", NOW + timedelta(minutes=3), "結果D"),
        _watch_event("watch:1:t:4", "価格E", NOW + timedelta(minutes=4),
                     "変化E"),
        _job_event("集計F", NOW + timedelta(minutes=5), "結果F"),
        _watch_event("watch:1:t:6", "価格G", NOW + timedelta(minutes=6),
                     "変化G"),
    ]
    for event in entries:
        assert enqueue_event(tmp_path, event)
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()
    text = check_and_post(cfg, now=MORNING, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == spy.result
    prompt = spy.prompts[0]
    # 古い順に5件（見張り3件＋ジョブ2件）だけが材料に入る
    for marker in ("変化A", "結果B", "変化C", "結果D", "変化E"):
        assert marker in prompt
    assert "結果F" not in prompt and "変化G" not in prompt
    # 溢れた分は消さず未配信のまま残る（ジョブ・見張りの順）
    remaining = pending_events(tmp_path)
    assert [e.source_title for e in remaining] == ["集計F", "価格G"]


# ------------------------------------------------------------ 即時と既定

@pytest.mark.parametrize("delivery", [None, "immediate"])
def test_immediate_and_default_jobs_post_immediately(
        tmp_path: Path, delivery):
    _write_job(tmp_path, delivery=delivery)
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    notified: list[str] = []
    posted = _run_jobs(cfg, session, job_executor=_JobExecutor("結果です"),
                       notify=lambda t, u, x: notified.append(x))
    assert len(posted) == 1
    assert "定期ジョブ「朝のニュース」の結果です。" in posted[0]
    assert "結果です" in posted[0]
    assert session.posted == posted and notified == posted
    assert pending_events(tmp_path) == []


# ------------------------------------------------------------ 失敗は即時固定

@pytest.mark.parametrize("delivery", ["digest", "auto"])
def test_job_failure_is_immediate_regardless_of_delivery(
        tmp_path: Path, delivery):
    _write_job(tmp_path, delivery=delivery)
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy()
    notified: list[str] = []
    posted = _run_jobs(
        cfg, session,
        job_executor=_JobExecutor(exc=RuntimeError("実行先で問題が起きました")),
        triage_executor=triage,
        notify=lambda t, u, x: notified.append(x))
    # 失敗の知らせはその場で届き、置き場には入らない。auto でも
    # 失敗には判定を使わない
    assert len(posted) == 1
    assert "定期ジョブ「朝のニュース」の実行に失敗しました。" in posted[0]
    assert session.posted == posted and notified == posted
    assert pending_events(tmp_path) == []
    assert triage.prompts == []
    # 失敗でも実行時刻は記録される
    assert JobBoard(tmp_path).list()[0].last_run_at != ""

    # error: の結果文字列でも同じ
    _write_job(tmp_path, delivery=delivery)
    session2 = _FakeSession()
    posted2 = _run_jobs(
        cfg, session2,
        job_executor=_JobExecutor(result="error: 子が失敗しました"))
    assert len(posted2) == 1 and "失敗しました" in posted2[0]
    assert pending_events(tmp_path) == []


# ------------------------------------------------------------ auto の判定

def test_auto_job_digest_verdict_queues_result(tmp_path: Path):
    _write_job(tmp_path, delivery="auto")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy(result=DIGEST_JSON)
    posted = _run_jobs(cfg, session, job_executor=_JobExecutor("集計結果です"),
                       triage_executor=triage)
    assert posted == [] and session.posted == []
    assert len(triage.prompts) == 1  # 判定は結果1件につき最大1回
    pending = pending_events(tmp_path)
    assert len(pending) == 1
    event = pending[0]
    assert event.source_kind == "job"
    assert event.result_text == "集計結果です"
    assert event.decision_by == "model"
    assert event.decision_reason == "model_digest"
    # 発生時のフィードに、おまかせ判定の短い理由が添えられる
    feed = load_feed_entries(tmp_path)
    assert any("おまかせ判定: 日常的な集計結果です" in e.body for e in feed)
    # 実行時刻は記録される
    assert JobBoard(tmp_path).list()[0].last_run_at != ""


def test_auto_job_immediate_verdict_posts_full_result(tmp_path: Path):
    _write_job(tmp_path, delivery="auto")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy(result=IMMEDIATE_JSON)
    posted = _run_jobs(cfg, session,
                       job_executor=_JobExecutor("詳しい結果の本文です"),
                       triage_executor=triage)
    # 即時の投稿は判定の知らせ文候補ではなく、通常の結果文で
    # 組み立てる（ジョブの結果そのものが届ける内容のため）
    assert len(posted) == 1
    assert "定期ジョブ「朝のニュース」の結果です。" in posted[0]
    assert "詳しい結果の本文です" in posted[0]
    assert "異常がありました" not in posted[0]
    assert pending_events(tmp_path) == []
    # 判定の記録はフィードに残る
    feed = load_feed_entries(tmp_path)
    assert any("おまかせ判定でその場でお知らせしました" in e.body
               and "model_immediate" in e.body for e in feed)


@pytest.mark.parametrize("verdict,marker", [
    ("判定は digest です", "model_invalid_immediate"),   # JSON でない
    (UNCERTAIN_JSON, "model_uncertain_immediate"),       # 確信なし
])
def test_auto_job_unusable_verdict_falls_back_to_immediate(
        tmp_path: Path, verdict, marker):
    _write_job(tmp_path, delivery="auto")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy(result=verdict)
    posted = _run_jobs(cfg, session, job_executor=_JobExecutor("結果です"),
                       triage_executor=triage)
    assert len(posted) == 1 and "結果です" in posted[0]
    assert len(triage.prompts) == 1
    assert pending_events(tmp_path) == []
    feed = load_feed_entries(tmp_path)
    assert any(marker in e.body for e in feed)


def test_auto_job_triage_exception_falls_back_to_immediate(tmp_path: Path):
    _write_job(tmp_path, delivery="auto")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy(exc=RuntimeError("判定役が落ちた"))
    posted = _run_jobs(cfg, session, job_executor=_JobExecutor("結果です"),
                       triage_executor=triage)
    assert len(posted) == 1 and "結果です" in posted[0]
    assert pending_events(tmp_path) == []
    feed = load_feed_entries(tmp_path)
    assert any("model_invalid_immediate" in e.body for e in feed)


def test_explicit_job_settings_are_not_judged(tmp_path: Path):
    # immediate / digest を明示したジョブでは判定自体を呼ばない
    for delivery in ("immediate", "digest"):
        _write_job(tmp_path, delivery=delivery)
        cfg = Config(data_dir=tmp_path)
        session = _FakeSession()
        triage = _TriageSpy()
        _run_jobs(cfg, session, job_executor=_JobExecutor("結果です"),
                  triage_executor=triage)
        assert triage.prompts == [], delivery


def test_job_triage_prompt_materials():
    job = Job(id="1", title="週次集計", prompt="売上を集計して",
              schedule={"kind": "weekly", "weekday": 0, "time": "09:00"},
              delivery="auto")
    prompt = build_job_triage_prompt(job, "結果の要点", NOW)
    assert "週次集計" in prompt
    assert "売上を集計して" in prompt
    assert "結果の要点" in prompt
    assert NOW.isoformat(timespec="seconds") in prompt
    assert '"route"' in prompt
    # 結果が長い場合は、全文ではなく切り詰めて渡す
    long_prompt = build_job_triage_prompt(job, "あ" * 5000, NOW)
    assert "あ" * 5000 not in long_prompt
    assert "あ" * 800 in long_prompt


# ------------------------------------------------------------ 倒し込み

def test_digest_job_in_wall_mode_posts_immediately(tmp_path: Path):
    _write_job(tmp_path, delivery="digest")
    cfg = Config(data_dir=tmp_path, mode="wall")
    session = _FakeSession()
    posted = _run_jobs(cfg, session, job_executor=_JobExecutor("結果です"))
    # スレッドモードでは朝の知らせの経路が無いため即時に倒す
    assert len(posted) == 1 and "結果です" in posted[0]
    assert pending_events(tmp_path) == []


def test_digest_job_with_triage_disabled_posts_immediately(
        tmp_path: Path):
    for delivery in ("digest", "auto"):
        _write_job(tmp_path, delivery=delivery)
        cfg = Config(data_dir=tmp_path, notification_triage_enabled=False)
        session = _FakeSession()
        triage = _TriageSpy()
        posted = _run_jobs(cfg, session, job_executor=_JobExecutor("結果です"),
                           triage_executor=triage)
        assert len(posted) == 1 and "結果です" in posted[0], delivery
        assert pending_events(tmp_path) == []
        assert triage.prompts == [], delivery


def test_job_queue_failure_falls_back_to_immediate(
        tmp_path: Path, monkeypatch):
    _write_job(tmp_path, delivery="digest")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    monkeypatch.setattr(proactive_mod, "enqueue_event",
                        lambda *a, **k: False)
    posted = _run_jobs(cfg, session, job_executor=_JobExecutor("結果です"))
    assert len(posted) == 1 and "結果です" in posted[0]
    assert session.posted == posted
    assert JobBoard(tmp_path).list()[0].last_run_at != ""


def test_auto_job_queue_failure_falls_back_to_immediate(
        tmp_path: Path, monkeypatch):
    _write_job(tmp_path, delivery="auto")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    monkeypatch.setattr(proactive_mod, "enqueue_event",
                        lambda *a, **k: False)
    posted = _run_jobs(cfg, session, job_executor=_JobExecutor("結果です"),
                       triage_executor=_TriageSpy(result=DIGEST_JSON))
    assert len(posted) == 1 and "結果です" in posted[0]
    feed = load_feed_entries(tmp_path)
    assert any("queue_failed_immediate" in e.body for e in feed)


# ------------------------------------------------------------ 結果の切り詰め

def test_long_job_result_is_truncated_in_queue(tmp_path: Path):
    _write_job(tmp_path, delivery="digest")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    long_result = "結果の本文です。" * 200  # 1600字
    posted = _run_jobs(cfg, session, job_executor=_JobExecutor(long_result))
    assert posted == []
    event = pending_events(tmp_path)[0]
    # 置き場には上限までだけを保存し、切り詰めたことを記録する
    assert len(event.result_text) == MAX_JOB_RESULT_CHARS
    assert event.result_truncated is True
    # 朝のまとめの材料でも、切り詰めたことが分かる
    material = build_digest_material([event], NOW + timedelta(days=1))
    assert material is not None
    assert "定期ジョブ「朝のニュース」の結果:" in material
    assert "（結果が長いため一部のみ）" in material


def test_job_event_roundtrip(tmp_path: Path):
    job = Job(id="1", title="集計", prompt="集計する",
              schedule={"kind": "daily", "time": "09:00"}, delivery="digest")
    event = build_job_event(job, "短い結果", NOW, "job_setting_digest")
    assert enqueue_event(tmp_path, event)
    loaded = load_events(tmp_path)[0]
    assert loaded.source_kind == "job"
    assert loaded.result_text == "短い結果"
    assert loaded.result_truncated is False
    assert loaded.id == "job:1:2026-11-07T12:00:00"
    # 第3段より前の行（result_text を持たない見張りの出来事）も
    # そのまま読める
    watch = _watch_event("watch:1:t:0", "価格", NOW, "変化")
    assert enqueue_event(tmp_path, watch)
    loaded_watch = [e for e in load_events(tmp_path)
                    if e.id == watch.id][0]
    assert loaded_watch.result_text == ""
    assert loaded_watch.result_truncated is False


# ------------------------------------------- 見張りの確認失敗の連続記録

def test_watch_error_streak_is_recorded(tmp_path: Path):
    board = WatchBoard(tmp_path)
    watch = board.add("ページ", "web", "https://example.com")
    t1 = datetime(2026, 11, 7, 10, 0, 0)
    t2 = datetime(2026, 11, 7, 11, 0, 0)
    t3 = datetime(2026, 11, 7, 12, 0, 0)
    board.mark_checked(watch.id, t1, error="取得できませんでした")
    board.mark_checked(watch.id, t2, error="取得できませんでした")
    saved = WatchBoard(tmp_path).list()[0]
    assert saved.error_count == 2
    # 連続が始まった時刻は最初の失敗のまま
    assert saved.first_error_at == t1.isoformat(timespec="seconds")
    assert saved.last_error == "取得できませんでした"
    assert saved.last_checked_at == t2.isoformat(timespec="seconds")
    # 成功したら連続の記録は戻る
    board.mark_checked(watch.id, t3, hash="abc", summary="本文")
    saved = WatchBoard(tmp_path).list()[0]
    assert saved.error_count == 0 and saved.first_error_at == ""
    assert saved.last_error == ""


def test_watch_error_fields_default_for_old_records(tmp_path: Path):
    path = tmp_path / "watches.json"
    base = {"kind": "search", "target": "猫"}
    path.write_text(json.dumps([
        {"id": "1", "title": "旧形式", **base},
        {"id": "2", "title": "記録あり", **base, "error_count": 3,
         "first_error_at": "2026-11-07T09:00:00"},
        {"id": "3", "title": "壊れた記録", **base, "error_count": "x",
         "first_error_at": 5},
        {"id": "4", "title": "負の回数", **base, "error_count": -2},
    ]), encoding="utf-8")
    watches = WatchBoard(tmp_path).list()
    # 壊れた記録でも見張り自体は落とさない
    assert [(w.title, w.error_count, w.first_error_at) for w in watches] == [
        ("旧形式", 0, ""),
        ("記録あり", 3, "2026-11-07T09:00:00"),
        ("壊れた記録", 0, "5"),
        ("負の回数", 0, ""),
    ]


def test_repeated_watch_failures_are_recorded_not_notified(
        tmp_path: Path, monkeypatch):
    # 確認失敗が繰り返しても、この段では知らせは出ない（記録だけ）
    state = {"fail": False}

    def fake_fetch(url: str) -> str:
        if state["fail"]:
            raise RuntimeError("取得できませんでした")
        return "最初の本文"

    monkeypatch.setattr(watches_mod, "fetch_page", fake_fetch)
    board = WatchBoard(tmp_path)
    board.add("ページ", "web", "https://example.com", interval_hours=1,
              delivery="digest")
    # NOW の時点で最初の確認が来ているよう、作成時刻を固定する
    _pin_created_at(tmp_path, NOW - timedelta(hours=2))
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    assert run_due_watches(cfg, NOW, lambda: session) == []  # 基準の記録
    state["fail"] = True
    for hours in (2, 4, 6):
        posted = run_due_watches(
            cfg, NOW + timedelta(hours=hours), lambda: session)
        assert posted == [] and session.posted == []
    saved = WatchBoard(tmp_path).list()[0]
    assert saved.error_count == 3
    assert saved.first_error_at == (
        NOW + timedelta(hours=2)).isoformat(timespec="seconds")
    assert saved.last_error != ""
    # 置き場にもフィードにも、失敗の知らせは残らない
    assert pending_events(tmp_path) == []
    assert load_feed_entries(tmp_path) == []
