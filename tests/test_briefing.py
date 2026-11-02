"""朝のまとめ（日報の拡張）のテスト。

KUMA_BRIEFING_TIME の時刻までは日報も出ないこと、時刻を過ぎたら
委任の子が組み立てたまとめが日報の代わりに投稿されて声かけ枠を
使うこと、子の失敗では日報に倒れること、「なし」では投稿せず
同日に子を再実行しないこと、off では従来のタイミングに戻ること、
モデルなし巡回では子を動かさないことを確認する。子の実行は
briefing_executor の差し替え（記録するだけのスタブ）で検証し、
ネットワークや実モデルには出ない。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.notify_prefs import add_muted_topic, set_quiet_hours
from kuma_agent.proactive import _UnusedModel, check_and_post, run_sweep
from kuma_agent.session import Session
from kuma_agent.settings import set_proactive_enabled
from kuma_agent.tools import Tool

# 2026-11-03 は火曜日。まとめの既定時刻は 07:30
BEFORE = datetime(2026, 11, 3, 7, 0, 0)
AFTER = datetime(2026, 11, 3, 8, 0, 0)
SUMMARY = "おはようございます。今日のまとめです。"


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


class _SpyExecutor:
    """朝のまとめの子の差し替え。指示文を記録し、決めた結果を返す。"""

    def __init__(self, result: str = SUMMARY,
                 exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.prompts: list[str] = []

    def __call__(self, parent, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.result


def _write_tasks(user_dir: Path, tasks: list[dict]) -> Path:
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / "tasks.json"
    path.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
    return path


def _task(task_id: str, title: str, created: datetime,
          memo: str = "") -> dict:
    stamp = created.isoformat(timespec="seconds")
    return {"id": task_id, "title": title, "status": "open", "memo": memo,
            "created_at": stamp, "updated_at": stamp}


def _records(user_dir: Path) -> list[dict]:
    path = user_dir / "conversations" / "main.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _state(user_dir: Path) -> dict:
    path = user_dir / "proactive_state.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _session_with_calendar(cfg: Config) -> Session:
    """カレンダーらしい道具（gcal__list-events）を足したセッション。"""
    session = Session(cfg, _EchoModel())
    session.tools.register(Tool(
        "gcal__list-events", "予定の一覧を取る",
        {"type": "object", "properties": {}}, lambda **_kw: ""))
    return session


# ------------------------------------------------------------ 時刻の前後

def test_before_briefing_time_no_post_then_briefing_after(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    # まとめの時刻より前は、日報もまとめも出ない（枠も消費しない）
    assert check_and_post(cfg, now=BEFORE, session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert _records(tmp_path) == []
    assert spy.prompts == []
    assert "last_posted_on" not in _state(tmp_path)

    # 時刻を過ぎた最初の巡回でまとめが投稿される
    text = check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == SUMMARY
    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["content"] == SUMMARY
    assert records[0]["proactive"] is True
    # 子への指示には日付と用事が含まれる
    assert len(spy.prompts) == 1
    assert "2026年11月3日" in spy.prompts[0]
    assert "買い物" in spy.prompts[0]
    assert _state(tmp_path)["last_posted_on"] == "2026-11-03"


def test_briefing_consumes_daily_slot(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    assert check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy) == SUMMARY
    # 同じ日の後続の巡回では、まとめも日報も出ず、子も再実行しない
    assert check_and_post(cfg, now=AFTER + timedelta(hours=4),
                          session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert len(spy.prompts) == 1
    assert len(_records(tmp_path)) == 1


def test_briefing_text_goes_to_notify(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    delivered: list[tuple[str, str, str]] = []

    text = check_and_post(
        cfg, now=AFTER, session_provider=lambda: session,
        briefing_executor=_SpyExecutor(),
        notify=lambda t, u, s: delivered.append((t, u, s)))
    assert text == SUMMARY
    assert delivered == [("default", "default", SUMMARY)]


def test_run_sweep_passes_briefing_executor(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    posted = run_sweep(cfg, now=AFTER,
                       session_getter=lambda _t, _u: session,
                       briefing_executor=spy)
    assert posted == [("default", "default", SUMMARY)]
    assert len(spy.prompts) == 1


# ------------------------------------------------------------ 子の結果

def test_briefing_error_falls_back_to_digest(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor(result="error: 子の側で問題が起きました")

    text = check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy)
    # 声かけ自体は欠かさず、従来の日報の文面で投稿される
    assert text is not None
    assert "お知らせです。進行中の用事が 1 件あります。" in text
    assert "買い物" in text
    assert _state(tmp_path)["last_posted_on"] == "2026-11-03"
    assert len(_records(tmp_path)) == 1


def test_briefing_exception_falls_back_to_digest(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor(exc=RuntimeError("爆発しました"))

    text = check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text is not None and "お知らせです" in text
    assert len(_records(tmp_path)) == 1


def test_briefing_error_without_tasks_posts_nothing(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = _session_with_calendar(cfg)
    spy = _SpyExecutor(result="error: 駄目でした")

    # 用事が無ければ、倒す先の日報も無いので何も投稿しない
    assert check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert _records(tmp_path) == []
    assert "last_posted_on" not in _state(tmp_path)


def test_briefing_nothing_marks_checked_and_no_rerun(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = _session_with_calendar(cfg)
    spy = _SpyExecutor(result="なし")

    # 予定も用事も無いので投稿せず、確認済みだけが記録される
    assert check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert _records(tmp_path) == []
    state = _state(tmp_path)
    assert state["briefing_checked_on"] == "2026-11-03"
    assert "last_posted_on" not in state  # 声かけ枠は消費していない

    # 同じ日は子を二度と動かさない
    assert check_and_post(cfg, now=AFTER + timedelta(hours=4),
                          session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert len(spy.prompts) == 1

    # 翌日になれば改めて確認する
    assert check_and_post(cfg, now=AFTER + timedelta(days=1),
                          session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert len(spy.prompts) == 2
    assert _state(tmp_path)["briefing_checked_on"] == "2026-11-04"


# ------------------------------------------------------------ 子を動かす条件

def test_no_material_no_child(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    # 用事が無く、カレンダーらしい道具も無ければ子は動かない
    assert check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert spy.prompts == []
    assert _records(tmp_path) == []


def test_calendar_tool_without_tasks_runs_child(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = _session_with_calendar(cfg)
    spy = _SpyExecutor(
        result="おはようございます。本日は 10:00 から会議があります。")

    text = check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text is not None and "会議" in text
    assert len(spy.prompts) == 1
    # 指示には用事が無い旨も含まれる
    assert "進行中の用事はありません" in spy.prompts[0]
    assert _state(tmp_path)["last_posted_on"] == "2026-11-03"


def test_briefing_prompt_excludes_muted_tasks(tmp_path: Path):
    _write_tasks(tmp_path, [
        _task("1", "買い物", AFTER),
        _task("2", "銀行の手続き", AFTER),
    ])
    add_muted_topic(tmp_path, "銀行")
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    assert check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy) == SUMMARY
    assert "買い物" in spy.prompts[0]
    assert "銀行の手続き" not in spy.prompts[0]


# ------------------------------------------------------------ off と従来動作

def test_briefing_off_traditional_timing(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    cfg = Config(data_dir=tmp_path, briefing_time="off")

    # off なら、まとめの時刻より前でもその日の最初の巡回で日報が出る
    early = datetime(2026, 11, 3, 6, 0, 0)
    text = check_and_post(cfg, now=early)
    assert text is not None and "お知らせです" in text
    assert _state(tmp_path)["last_posted_on"] == "2026-11-03"


def test_briefing_off_never_runs_child(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    cfg = Config(data_dir=tmp_path, briefing_time="off")
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    text = check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text is not None and "お知らせです" in text
    assert spy.prompts == []


# ------------------------------------------------------------ 抑制の規則

def test_briefing_quiet_hours(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    set_quiet_hours(tmp_path, "07:00", "09:00")
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    # 静かな時間帯の間は止まり、枠も消費しない
    assert check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert spy.prompts == []
    assert _state(tmp_path) == {}

    # 時間帯が明ければその日のまとめが出る
    text = check_and_post(cfg, now=datetime(2026, 11, 3, 9, 30, 0),
                          session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == SUMMARY


def test_briefing_tenant_disabled(tmp_path: Path):
    _write_tasks(tmp_path / "tenants" / "acme",
                 [_task("1", "acmeの用事", AFTER)])
    set_proactive_enabled(tmp_path, "acme", False)
    cfg = Config(data_dir=tmp_path, tenant="acme")
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    assert check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert spy.prompts == []
    assert _records(tmp_path / "tenants" / "acme") == []


def test_model_less_sweep_never_runs_child(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    cfg = Config(data_dir=tmp_path)

    # session_provider が無い巡回（CLI の proactive）は一時 Session
    # （モデルなし）で回る。子は動かず、従来の日報だけが投稿される
    text = check_and_post(cfg, now=AFTER)
    assert text is not None and "お知らせです" in text
    children = sorted((tmp_path / "conversations").glob("delegate-*.jsonl"))
    assert children == []  # 子セッションは作られていない


def test_model_less_session_with_calendar_tool_does_nothing(
        tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _UnusedModel())
    session.tools.register(Tool(
        "gcal__list-events", "予定の一覧を取る",
        {"type": "object", "properties": {}}, lambda **_kw: ""))
    spy = _SpyExecutor()

    # モデルなしセッションでは、カレンダー道具があっても子を
    # 動かさず、枠も確認済みの記録も残さない
    assert check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert spy.prompts == []
    assert _state(tmp_path) == {}
    assert _records(tmp_path) == []


# ------------------------------------------------------------ 設定の読み方

def test_briefing_time_from_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KUMA_BRIEFING_TIME", "off")
    assert Config(data_dir=tmp_path).briefing_time == "off"
    monkeypatch.setenv("KUMA_BRIEFING_TIME", "08:15")
    assert Config(data_dir=tmp_path).briefing_time == "08:15"
    monkeypatch.delenv("KUMA_BRIEFING_TIME")
    assert Config(data_dir=tmp_path).briefing_time == "07:30"


def test_briefing_invalid_time_falls_back_to_default(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", AFTER)])
    cfg = Config(data_dir=tmp_path, briefing_time="25:99")
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    # 不正な値は既定の 07:30 として扱う（07:00 では出ない）
    assert check_and_post(cfg, now=BEFORE, session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy) == SUMMARY


def test_manual_run_posts_digest_before_briefing_time(tmp_path: Path):
    """手動実行（CLI の proactive）は時刻より前でも日報を出す。

    自動巡回はまとめの時刻まで待つが、運用者がコマンドを打った
    ときは「今すぐ出す」判断を優先する。モデルなし経路なので
    子は動かず、従来の日報になる（時刻に依らず動くことの固定）。
    """
    _write_tasks(tmp_path, [_task("1", "買い物", BEFORE)])
    cfg = Config(data_dir=tmp_path)
    spy = _SpyExecutor()

    # 自動巡回（manual でない）は時刻前では何も出さない
    assert check_and_post(cfg, now=BEFORE, briefing_executor=spy) is None
    # 手動実行ではその場で日報が出て、子は動かない
    text = check_and_post(cfg, now=BEFORE, briefing_executor=spy,
                          manual=True)
    assert text is not None and "買い物" in text
    assert spy.prompts == []
    assert len(_records(tmp_path)) == 1
