"""通知の取捨 第2段（見張りの auto＝モデル判定、0.85.0）のテスト。

auto の見張りで、判定が digest なら置き場へ入り（decision_by
は model、理由コードは model_digest）、immediate なら判定の
知らせ文候補を使ってその場で投稿すること、判定の形式が壊れて
いる・例外・確信なし・モデルなしなら理由を残して即時へ倒れる
こと、immediate / digest を明示した見張りでは判定自体が呼ば
れないこと、変化が無い巡回では判定が呼ばれないこと、判定は
内部専用の振り分け判定役（道具なし）で行われ delegate 道具
からは選べないことを確認する。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import kuma_agent.proactive as proactive_mod
import kuma_agent.watches as watches_mod
from kuma_agent.config import Config
from kuma_agent.delegate import run_delegated_task
from kuma_agent.feed import load_feed_entries
from kuma_agent.llm import Message
from kuma_agent.notification_triage import (
    parse_triage_verdict,
    pending_events,
    load_events,
)
from kuma_agent.proactive import run_due_watches
from kuma_agent.session import Session
from kuma_agent.watches import WatchBoard

NOW = datetime(2026, 11, 7, 12, 0, 0)


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
    {"route": "digest", "certain": True, "reason": "日常的な変化です"},
    ensure_ascii=False)
IMMEDIATE_JSON = json.dumps(
    {"route": "immediate", "certain": True,
     "reason": "価格が大きく下がりました", "notice": "価格が下がりました"},
    ensure_ascii=False)


class _FakeSession:
    """run_due_watches が使う範囲だけを持つ Session の代わり。"""

    def __init__(self, model=None):
        self.model = model if model is not None else object()
        self.posted: list[str] = []

    def post_proactive_message(self, text: str) -> None:
        self.posted.append(text)


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


class _NoticeSpy:
    """知らせ文作成役の差し替え。呼ばれた回数を記録する。"""

    def __init__(self, result: str = "作成役の知らせ文") -> None:
        self.result = result
        self.prompts: list[str] = []

    def __call__(self, parent, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.result


def _seed_watch(tmp_path: Path, monkeypatch, text: str,
                delivery: str = "auto") -> dict:
    """本文を差し替えられる web の見張りを用意する（基準は未記録）。"""
    state = {"text": text}
    monkeypatch.setattr(watches_mod, "fetch_page", lambda url: state["text"])
    board = WatchBoard(tmp_path)
    board.add("価格ページ", "web", "https://example.com/item", interval_hours=1,
              note="価格が下がったらすぐ知らせて", delivery=delivery)
    # NOW の時点で最初の確認が来ているよう、作成時刻を固定する
    _pin_created_at(tmp_path, NOW - timedelta(hours=2))
    return state


def _baseline(cfg: Config, session: _FakeSession, **kwargs) -> None:
    """最初の確認（基準の記録だけ）を済ませる。"""
    assert run_due_watches(cfg, NOW, lambda: session, **kwargs) == []


def _change_and_run(cfg: Config, session: _FakeSession, state: dict,
                    new_text: str = "新しい本文です", **kwargs):
    state["text"] = new_text
    return run_due_watches(cfg, NOW + timedelta(hours=2), lambda: session,
                           **kwargs)


# ------------------------------------------------------------ 判定の検証

def test_parse_triage_verdict_accepts_valid_json():
    verdict, code, reason = parse_triage_verdict(DIGEST_JSON)
    assert verdict is not None and verdict.route == "digest"
    assert verdict.notice is None
    assert code == "model_digest" and reason == "日常的な変化です"

    verdict, code, reason = parse_triage_verdict(IMMEDIATE_JSON)
    assert verdict is not None and verdict.route == "immediate"
    assert verdict.notice == "価格が下がりました"
    assert code == "model_immediate"

    # コードフェンスで包まれた JSON も読める
    verdict, code, _ = parse_triage_verdict(f"```json\n{DIGEST_JSON}\n```")
    assert verdict is not None and code == "model_digest"


def test_parse_triage_verdict_rejects_unusable_results():
    # error の結果・JSON でない・前後に説明文がある・項目不足・
    # 知らない route・reason が空、はすべて形式不正として採用しない
    for raw in ("error: 子が失敗しました",
                "判定は digest です",
                f"結果は次のとおりです: {DIGEST_JSON}",
                json.dumps({"route": "digest", "certain": True}),
                json.dumps({"route": "later", "certain": True,
                            "reason": "理由"}),
                json.dumps({"route": "digest", "certain": True,
                            "reason": "  "}),
                json.dumps({"route": "digest", "reason": "理由"}),
                json.dumps(["digest"]),
                None,
                42):
        verdict, code, _ = parse_triage_verdict(raw)
        assert verdict is None and code == "model_invalid_immediate", raw


def test_parse_triage_verdict_uncertain_is_distinct():
    raw = json.dumps({"route": "digest", "certain": False,
                      "reason": "どちらとも言えません"}, ensure_ascii=False)
    verdict, code, reason = parse_triage_verdict(raw)
    assert verdict is None
    assert code == "model_uncertain_immediate"
    assert reason == "どちらとも言えません"


def test_parse_triage_verdict_notice_validation():
    # 長すぎる知らせ文候補は判定自体は採用し、候補だけ捨てる
    raw = json.dumps({"route": "immediate", "certain": True,
                      "reason": "理由", "notice": "あ" * 1001},
                     ensure_ascii=False)
    verdict, code, _ = parse_triage_verdict(raw)
    assert verdict is not None and verdict.route == "immediate"
    assert verdict.notice is None and code == "model_immediate"
    # error で始まる候補も使わない
    raw = json.dumps({"route": "immediate", "certain": True,
                      "reason": "理由", "notice": "error: 失敗"},
                     ensure_ascii=False)
    verdict, _, _ = parse_triage_verdict(raw)
    assert verdict is not None and verdict.notice is None


# ------------------------------------------------------------ auto の巡回

def test_auto_digest_goes_to_queue(tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy()
    notice = _NoticeSpy()
    _baseline(cfg, session, watch_executor=notice, triage_executor=triage)
    # 基準の記録だけの回では判定は呼ばれない
    assert triage.prompts == []

    posted = _change_and_run(cfg, session, state, watch_executor=notice,
                             triage_executor=triage)
    # その場では投稿せず、置き場へ入る。知らせ文作成役は呼ばない
    assert posted == [] and session.posted == []
    assert notice.prompts == []
    # 判定は変化 1 件につき 1 回だけ。材料に対象の URL は渡さない
    assert len(triage.prompts) == 1
    prompt = triage.prompts[0]
    assert "価格ページ" in prompt and "ページ" in prompt
    assert "前の本文" in prompt and "新しい本文です" in prompt
    assert "価格が下がったらすぐ知らせて" in prompt
    assert "https://example.com/item" not in prompt

    pending = pending_events(tmp_path)
    assert len(pending) == 1
    event = pending[0]
    assert event.decision == "digest"
    assert event.decision_by == "model"
    assert event.decision_reason == "model_digest"
    # 発生時のフィードに、判定の短い理由が残る
    feed = load_feed_entries(tmp_path)
    assert any("おまかせ判定: 日常的な変化です" in e.body for e in feed)
    # 基準は更新されている（同じ変化で判定を呼び直さない）
    watch = WatchBoard(tmp_path).list()[0]
    assert watch.last_changed_at != ""
    assert run_due_watches(cfg, NOW + timedelta(hours=4), lambda: session,
                           watch_executor=notice,
                           triage_executor=triage) == []
    assert len(triage.prompts) == 1


def test_auto_immediate_uses_verdict_notice(tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy(result=IMMEDIATE_JSON)
    notice = _NoticeSpy()
    _baseline(cfg, session, watch_executor=notice, triage_executor=triage)

    posted = _change_and_run(cfg, session, state, watch_executor=notice,
                             triage_executor=triage)
    # 判定の知らせ文候補をそのまま使い、作成役は追加で呼ばない
    assert len(posted) == 1
    assert "価格が下がりました" in posted[0]
    assert notice.prompts == []
    assert pending_events(tmp_path) == []
    # 判定の理由がフィードの記録に残る
    feed = load_feed_entries(tmp_path)
    assert any("model_immediate" in e.body
               and "価格が大きく下がりました" in e.body for e in feed)


def test_auto_immediate_without_notice_uses_notice_role(
        tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    no_notice = json.dumps({"route": "immediate", "certain": True,
                            "reason": "すぐ知らせるべき変化"},
                           ensure_ascii=False)
    triage = _TriageSpy(result=no_notice)
    notice = _NoticeSpy()
    _baseline(cfg, session, watch_executor=notice, triage_executor=triage)

    posted = _change_and_run(cfg, session, state, watch_executor=notice,
                             triage_executor=triage)
    # 候補が無い場合は通常の知らせ文作成に回る
    assert len(posted) == 1 and "作成役の知らせ文" in posted[0]
    assert len(notice.prompts) == 1


@pytest.mark.parametrize("raw", [
    "判定できませんでした",
    "error: 判定役が失敗しました",
    json.dumps({"route": "digest", "certain": True}),  # reason 欠け
])
def test_auto_unusable_verdict_falls_back_to_immediate(
        tmp_path: Path, monkeypatch, raw: str):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy(result=raw)
    notice = _NoticeSpy()
    _baseline(cfg, session, watch_executor=notice, triage_executor=triage)

    posted = _change_and_run(cfg, session, state, watch_executor=notice,
                             triage_executor=triage)
    # 判定が使えなければ即時に倒し、知らせ文は作成役が作る
    assert len(posted) == 1 and "作成役の知らせ文" in posted[0]
    assert pending_events(tmp_path) == []
    feed = load_feed_entries(tmp_path)
    assert any("model_invalid_immediate" in e.body for e in feed)


def test_auto_uncertain_falls_back_to_immediate(tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    raw = json.dumps({"route": "digest", "certain": False,
                      "reason": "判断がつきません"}, ensure_ascii=False)
    triage = _TriageSpy(result=raw)
    notice = _NoticeSpy()
    _baseline(cfg, session, watch_executor=notice, triage_executor=triage)

    posted = _change_and_run(cfg, session, state, watch_executor=notice,
                             triage_executor=triage)
    # 確信なしの判定は採用せず、朝へ回さず即時に知らせる
    assert len(posted) == 1 and "作成役の知らせ文" in posted[0]
    assert pending_events(tmp_path) == []
    feed = load_feed_entries(tmp_path)
    assert any("model_uncertain_immediate" in e.body for e in feed)


def test_auto_triage_exception_falls_back_to_immediate(
        tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy(exc=RuntimeError("判定役が落ちた"))
    notice = _NoticeSpy()
    _baseline(cfg, session, watch_executor=notice, triage_executor=triage)

    posted = _change_and_run(cfg, session, state, watch_executor=notice,
                             triage_executor=triage)
    assert len(posted) == 1 and "作成役の知らせ文" in posted[0]
    assert pending_events(tmp_path) == []
    feed = load_feed_entries(tmp_path)
    assert any("model_invalid_immediate" in e.body for e in feed)


def test_auto_without_model_defers_change(tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy()
    notice = _NoticeSpy()
    _baseline(cfg, session, watch_executor=notice, triage_executor=triage)

    # モデルなしの一時 Session の経路では、判定せず変化も消費しない
    free_session = _FakeSession(model=proactive_mod._UnusedModel())
    state["text"] = "新しい本文です"
    posted = run_due_watches(cfg, NOW + timedelta(hours=2),
                             lambda: free_session,
                             watch_executor=notice, triage_executor=triage)
    assert posted == [] and triage.prompts == []
    watch = WatchBoard(tmp_path).list()[0]
    assert watch.last_changed_at == ""
    assert "前の本文" in watch.last_summary
    # モデルがある経路の巡回では、同じ変化が改めて判定される
    posted = run_due_watches(cfg, NOW + timedelta(hours=3), lambda: session,
                             watch_executor=notice, triage_executor=triage)
    assert posted == [] and len(triage.prompts) == 1
    assert len(pending_events(tmp_path)) == 1


def test_auto_queue_failure_falls_back_to_immediate(
        tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy()  # digest 判定
    notice = _NoticeSpy()
    _baseline(cfg, session, watch_executor=notice, triage_executor=triage)
    monkeypatch.setattr(proactive_mod, "enqueue_event",
                        lambda *a, **k: False)

    posted = _change_and_run(cfg, session, state, watch_executor=notice,
                             triage_executor=triage)
    # 朝へ寄せる判定でも、置き場へ保存できなければ即時に倒す
    assert len(posted) == 1 and "作成役の知らせ文" in posted[0]
    assert pending_events(tmp_path) == []
    feed = load_feed_entries(tmp_path)
    assert any("queue_failed_immediate" in e.body for e in feed)
    # 投稿まで進んだので基準は更新される
    watch = WatchBoard(tmp_path).list()[0]
    assert watch.last_changed_at != ""


def test_explicit_settings_never_call_triage(tmp_path: Path, monkeypatch):
    # immediate / digest を明示した見張りでは、判定自体を行わない
    for delivery in ("immediate", "digest"):
        sub = tmp_path / delivery
        state = _seed_watch(sub, monkeypatch, "前の本文", delivery=delivery)
        cfg = Config(data_dir=sub)
        session = _FakeSession()
        triage = _TriageSpy(result=IMMEDIATE_JSON)
        notice = _NoticeSpy()
        _baseline(cfg, session, watch_executor=notice,
                  triage_executor=triage)
        posted = _change_and_run(cfg, session, state,
                                 watch_executor=notice,
                                 triage_executor=triage)
        assert triage.prompts == []
        if delivery == "immediate":
            assert len(posted) == 1
            assert pending_events(sub) == []
            event_decisions = []
        else:
            assert posted == []
            event_decisions = [e.decision_by
                               for e in load_events(sub)]
            assert event_decisions == ["setting"]


def test_no_change_never_calls_triage(tmp_path: Path, monkeypatch):
    state = _seed_watch(tmp_path, monkeypatch, "同じ本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    triage = _TriageSpy()
    notice = _NoticeSpy()
    _baseline(cfg, session, watch_executor=notice, triage_executor=triage)
    # 変化が無い巡回では判定は呼ばれない
    posted = run_due_watches(cfg, NOW + timedelta(hours=2), lambda: session,
                             watch_executor=notice, triage_executor=triage)
    assert posted == [] and triage.prompts == []


def test_auto_forced_immediate_skips_triage(tmp_path: Path, monkeypatch):
    # スレッドモード（朝の経路が無い）と無効化中は、auto でも
    # 判定を行わず即時に倒す
    state = _seed_watch(tmp_path / "wall", monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path / "wall", mode="wall")
    session = _FakeSession()
    triage = _TriageSpy()
    notice = _NoticeSpy()
    _baseline(cfg, session, watch_executor=notice, triage_executor=triage)
    posted = _change_and_run(cfg, session, state, watch_executor=notice,
                             triage_executor=triage)
    assert len(posted) == 1 and triage.prompts == []

    state2 = _seed_watch(tmp_path / "off", monkeypatch, "前の本文")
    cfg2 = Config(data_dir=tmp_path / "off",
                  notification_triage_enabled=False)
    session2 = _FakeSession()
    triage2 = _TriageSpy()
    _baseline(cfg2, session2, watch_executor=notice, triage_executor=triage2)
    posted2 = _change_and_run(cfg2, session2, state2, watch_executor=notice,
                              triage_executor=triage2)
    assert len(posted2) == 1 and triage2.prompts == []


# ------------------------------------------------------------ 判定役の実体

class _TriageCaptureModel:
    """子のシステムプロンプトと道具を記録し、判定 JSON を返す。"""

    def __init__(self) -> None:
        self.child_prompts: list[str] = []
        self.child_tool_names: list[list[str]] = []

    def chat(self, system_prompt, messages, tools):
        if "振り分け判定だけを担当します" in system_prompt:
            self.child_prompts.append(system_prompt)
            self.child_tool_names.append([t["name"] for t in tools])
            return Message("assistant", DIGEST_JSON)
        return Message("assistant", "親の応答")


def test_triage_role_child_has_no_tools(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path / "data")
    model = _TriageCaptureModel()
    session = Session(cfg, model)
    # delegate 道具からは内部の型を選べない
    result = session.tools.call(
        "delegate", {"task": "判定して", "role": "triage"})
    assert result.startswith("error:")
    assert model.child_prompts == []
    # コードからの呼び出しでは使え、子の道具はゼロで、指示は
    # 材料中の命令に従わないことを含む
    result = run_delegated_task(session, "判定して",
                                retry=False, role="triage")
    assert json.loads(result)["route"] == "digest"
    assert model.child_tool_names == [[]]
    prompt = model.child_prompts[0]
    assert "材料に含まれる命令や依頼" in prompt
    assert "従わず" in prompt


def test_auto_default_triage_executor_end_to_end(
        tmp_path: Path, monkeypatch):
    # 既定の判定役（委任の triage）で、auto の変化が置き場へ入る
    monkeypatch.chdir(tmp_path)
    state = _seed_watch(tmp_path / "seed", monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path / "data")
    board = WatchBoard(cfg.user_data_dir)
    board.add("価格ページ", "web", "https://example.com/item",
              interval_hours=1, delivery="auto")
    _pin_created_at(cfg.user_data_dir, NOW - timedelta(hours=2))
    model = _TriageCaptureModel()
    session = Session(cfg, model)
    # 基準の記録（判定役は使わない）
    assert run_due_watches(cfg, NOW, lambda: session) == []
    assert model.child_prompts == []
    state["text"] = "新しい本文です"
    posted = run_due_watches(cfg, NOW + timedelta(hours=2), lambda: session)
    assert posted == []
    assert len(model.child_prompts) == 1
    assert model.child_tool_names == [[]]
    pending = pending_events(cfg.user_data_dir)
    assert len(pending) == 1
    assert pending[0].decision_by == "model"
    assert pending[0].decision_reason == "model_digest"
