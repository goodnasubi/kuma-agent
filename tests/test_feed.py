"""フィード（能動発信と用事の進み具合の時系列）のテスト。

記録先（ユーザーの置き場の feed.jsonl）への追記と読み出し
（新しい順・件数の上限・種別の絞り込み、壊れた行の読み飛ばし）、
能動発信（日報・朝のまとめ・リマインダー・定期ジョブの結果）と
用事の変化でエントリが残ること、GET /api/feed の形とフィード
タブの表示マークアップを確認する。過去分は取り込まず、この版
から新規に起きた分だけが貯まる。
"""
import json
import threading
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.feed import (
    FEED_FILE,
    KIND_BRIEFING,
    KIND_PROACTIVE,
    KIND_TASK,
    append_feed_entry,
    load_feed_entries,
)
from kuma_agent.llm import Message
from kuma_agent.proactive import check_and_post
from kuma_agent.session import Session
from kuma_agent.tasks import TaskBoard
from kuma_agent.web import build_server, render_page

T1 = datetime(2026, 11, 3, 8, 0, 0)
T2 = datetime(2026, 11, 3, 9, 0, 0)
T3 = datetime(2026, 11, 3, 10, 0, 0)


# ------------------------------------------------------------ 記録先

def test_append_and_load_newest_first(tmp_path: Path):
    append_feed_entry(tmp_path, KIND_PROACTIVE, "一つ目", now=T1)
    append_feed_entry(tmp_path, KIND_TASK, "二つ目",
                      title="見出し", now=T2)
    append_feed_entry(tmp_path, KIND_BRIEFING, "三つ目", now=T3)

    entries = load_feed_entries(tmp_path)
    assert [e.body for e in entries] == ["三つ目", "二つ目", "一つ目"]
    assert entries[0].kind == KIND_BRIEFING
    assert entries[0].ts == "2026-11-03T10:00:00"
    assert entries[1].title == "見出し"
    assert entries[2].title == ""
    # 読み直しても残る
    assert len(load_feed_entries(tmp_path)) == 3


def test_load_limit_and_kind_filter(tmp_path: Path):
    for i in range(5):
        kind = KIND_TASK if i % 2 else KIND_PROACTIVE
        append_feed_entry(tmp_path, kind, f"本文{i}", now=T1)
    assert len(load_feed_entries(tmp_path, limit=2)) == 2
    assert len(load_feed_entries(tmp_path, limit=99999)) == 5
    assert len(load_feed_entries(tmp_path, limit="abc")) == 5
    tasks = load_feed_entries(tmp_path, kind=KIND_TASK)
    assert [e.body for e in tasks] == ["本文3", "本文1"]
    # 既知でない種別の指定は絞り込みとして扱わない
    assert len(load_feed_entries(tmp_path, kind="unknown")) == 5


def test_load_skips_broken_lines(tmp_path: Path):
    path = tmp_path / FEED_FILE
    path.write_text("\n".join([
        "壊れた行",
        json.dumps({"ts": "2026-11-03T08:00:00", "kind": "memo",
                    "title": "", "body": "種別が違う"}),
        json.dumps({"ts": "2026-11-03T08:00:00", "kind": "task",
                    "title": "", "body": ""}),
        json.dumps({"ts": "2026-11-03T08:00:00", "kind": "task",
                    "title": "", "body": "有効な行"}),
    ]) + "\n", encoding="utf-8")
    entries = load_feed_entries(tmp_path)
    assert [e.body for e in entries] == ["有効な行"]
    # ファイルが無ければ空
    assert load_feed_entries(tmp_path / "なし") == []


def test_append_validates_and_never_raises(tmp_path: Path):
    append_feed_entry(tmp_path, "unknown", "書かれない")
    append_feed_entry(tmp_path, KIND_TASK, "")
    assert load_feed_entries(tmp_path) == []
    # 置き場が作れない場所でも例外にしない（本体を落とさない）
    blocker = tmp_path / "ファイル"
    blocker.write_text("x", encoding="utf-8")
    append_feed_entry(blocker, KIND_TASK, "書けないはず")
    assert load_feed_entries(tmp_path) == []


# ------------------------------------------------------------ 用事の変化

def test_task_changes_are_recorded(tmp_path: Path):
    board = TaskBoard(tmp_path)
    task = board.add("買い物", memo="牛乳")
    entries = load_feed_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].kind == KIND_TASK
    assert entries[0].body == "用事「買い物」を追加しました"

    board.update(task.id, memo="牛乳と卵")
    entries = load_feed_entries(tmp_path)
    assert entries[0].body == "用事「買い物」のメモを更新しました"

    # 実際に変わらない更新では残さない
    board.update(task.id, memo="牛乳と卵")
    assert len(load_feed_entries(tmp_path)) == 2

    board.update(task.id, remind_at="2026-11-05T09:00:00")
    entries = load_feed_entries(tmp_path)
    assert entries[0].body == "用事「買い物」の通知時刻を 11/5 9:00 に設定しました"

    board.update(task.id, status="done")
    entries = load_feed_entries(tmp_path)
    assert entries[0].body == "用事「買い物」を完了しました"

    board.update(task.id, status="open")
    entries = load_feed_entries(tmp_path)
    assert entries[0].body == "用事「買い物」を進行中に戻しました"

    # 通知済みの印付けは進み具合ではないので残さない
    board.mark_reminded(task.id)
    assert len(load_feed_entries(tmp_path)) == 5


def test_task_add_with_remind_at_mentions_time(tmp_path: Path):
    board = TaskBoard(tmp_path)
    board.add("病院", remind_at="2026-11-06T14:30:00")
    entries = load_feed_entries(tmp_path)
    assert entries[0].body == "用事「病院」を追加しました（11/6 14:30 に通知）"


# ------------------------------------------------------------ 能動発信

class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


def _write_tasks(user_dir: Path, tasks: list[dict]) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "tasks.json").write_text(
        json.dumps(tasks, ensure_ascii=False), encoding="utf-8")


def _task_record(task_id: str, title: str, **extra) -> dict:
    record = {"id": task_id, "title": title, "status": "open", "memo": "",
              "created_at": "2026-11-03T07:00:00",
              "updated_at": "2026-11-03T07:00:00"}
    record.update(extra)
    return record


def test_daily_post_is_recorded_as_proactive(tmp_path: Path):
    _write_tasks(tmp_path, [_task_record("1", "買い物")])
    cfg = Config(data_dir=tmp_path)
    # セッション指定なし（モデルなしの一時 Session）では日報に倒れる
    text = check_and_post(cfg, now=T2)
    assert text is not None
    entries = load_feed_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].kind == KIND_PROACTIVE
    assert entries[0].body == text
    # 同じ日の二巡目では投稿も記録も増えない
    assert check_and_post(cfg, now=T3) is None
    assert len(load_feed_entries(tmp_path)) == 1


def test_briefing_is_recorded_as_briefing(tmp_path: Path):
    _write_tasks(tmp_path, [_task_record("1", "買い物")])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    summary = "おはようございます。今日のまとめです。"

    def executor(parent, prompt):
        return summary

    text = check_and_post(cfg, now=T2, session_provider=lambda: session,
                          briefing_executor=executor)
    assert text == summary
    entries = load_feed_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].kind == KIND_BRIEFING
    assert entries[0].body == summary


def test_reminder_is_recorded_as_proactive(tmp_path: Path):
    _write_tasks(tmp_path, [_task_record(
        "1", "病院", remind_at="2026-11-03T06:00:00", reminded_at="")])
    cfg = Config(data_dir=tmp_path)
    # 朝のまとめの時刻（07:30）より前なら日報は出ず、リマインダーだけ
    text = check_and_post(cfg, now=datetime(2026, 11, 3, 7, 0, 0))
    assert text is not None and "通知の時刻" in text
    entries = load_feed_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].kind == KIND_PROACTIVE
    assert entries[0].body == text


def test_job_result_is_recorded_as_proactive(tmp_path: Path):
    jobs = [{"id": "1", "title": "朝の確認", "prompt": "ニュースを確認して",
             "schedule": {"kind": "daily", "time": "09:00"},
             "enabled": True, "last_run_at": "",
             "created_at": "2026-11-01T08:00:00"}]
    (tmp_path / "jobs.json").write_text(
        json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())

    def executor(parent, prompt):
        return "ジョブの実行結果"

    text = check_and_post(cfg, now=datetime(2026, 11, 2, 9, 30, 0),
                          session_provider=lambda: session,
                          job_executor=executor)
    assert text is not None and "定期ジョブ「朝の確認」の結果です。" in text
    entries = load_feed_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].kind == KIND_PROACTIVE
    assert entries[0].body == text
    # 実行済みになったジョブは二重に記録しない
    assert check_and_post(cfg, now=datetime(2026, 11, 2, 9, 30, 0),
                          session_provider=lambda: session,
                          job_executor=executor) is None
    assert len(load_feed_entries(tmp_path)) == 1


def test_no_past_import(tmp_path: Path):
    # 過去の能動発信（会話記録の proactive の印）だけでは取り込まない
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    session.post_proactive_message("過去のお知らせです")
    assert load_feed_entries(tmp_path) == []


# ------------------------------------------------------------ Web API と画面

@contextmanager
def _running(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(base: str, path: str) -> dict:
    with urllib.request.urlopen(base + path) as res:
        return json.loads(res.read().decode("utf-8"))


def test_feed_over_http(tmp_path: Path):
    append_feed_entry(tmp_path, KIND_PROACTIVE, "お知らせ本文", now=T1)
    append_feed_entry(tmp_path, KIND_TASK, "用事の本文", now=T2)
    append_feed_entry(tmp_path, KIND_BRIEFING, "まとめ本文",
                      title="朝のまとめ", now=T3)
    with _running(tmp_path) as base:
        data = _get(base, "/api/feed")
        assert data["tenant"] == "default"
        assert data["user"] == "default"
        assert [e["body"] for e in data["entries"]] == [
            "まとめ本文", "用事の本文", "お知らせ本文"]
        assert data["entries"][0]["kind"] == "briefing"
        assert data["entries"][0]["title"] == "朝のまとめ"
        assert data["entries"][0]["ts"] == "2026-11-03T10:00:00"

        filtered = _get(base, "/api/feed?kind=task")
        assert [e["body"] for e in filtered["entries"]] == ["用事の本文"]

        limited = _get(base, "/api/feed?limit=1")
        assert [e["body"] for e in limited["entries"]] == ["まとめ本文"]


def test_render_page_has_feed_ui():
    page = render_page("muse")
    assert 'id="feedPanel"' in page
    assert 'id="feedRows"' in page
    assert "loadFeed" in page
    assert "/api/feed" in page
    assert "朝のまとめ" in page  # 種別の印の文言
    # フィードの「準備中」表示は無くなっている（アイデアは残る）
    assert "朝のまとめや声かけ、用事の進み具合をここに並べます" not in page
    assert 'id="ideasPanel"' in page
