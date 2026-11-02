"""知らせる・知らせないの好み（notify_prefs）のテスト。

好みの読み書きと壊れたファイルへの態度、ミュートが日報と
リマインダーを抑えること（ミュートされたリマインダーは印を
付けずに保留されること）、静かな時間帯が日報だけを止めて
時間帯が明ければその日の日報が出ること、道具 3 つの配線、
Web API の取得と更新を確認する。
"""
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.notify_prefs import (
    NotifyPrefs,
    add_muted_topic,
    clear_quiet_hours,
    load_notify_prefs,
    remove_muted_topic,
    set_quiet_hours,
)
from kuma_agent.proactive import check_and_post
from kuma_agent.session import Session
from kuma_agent.tasks import TaskBoard
from kuma_agent.tools import ToolBox, activity_label
from kuma_agent.web import build_server

NOW = datetime(2026, 11, 2, 9, 0, 0)
DUE = "2026-11-02T08:30:00"  # NOW より前（到来済み）


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


def _write_tasks(user_dir: Path, tasks: list[dict]) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "tasks.json").write_text(
        json.dumps(tasks, ensure_ascii=False), encoding="utf-8")


def _task(task_id: str, title: str, memo: str = "",
          remind_at: str = "") -> dict:
    stamp = NOW.isoformat(timespec="seconds")
    return {"id": task_id, "title": title, "status": "open", "memo": memo,
            "created_at": stamp, "updated_at": stamp,
            "remind_at": remind_at, "reminded_at": ""}


def _records(user_dir: Path) -> list[dict]:
    path = user_dir / "conversations" / "main.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_prefs(user_dir: Path, prefs: dict) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "notify_prefs.json").write_text(
        json.dumps(prefs, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 読み書き

def test_load_defaults_when_no_file(tmp_path: Path):
    prefs = load_notify_prefs(tmp_path)
    assert prefs.muted_topics == []
    assert prefs.quiet_start is None and prefs.quiet_end is None
    assert prefs.quiet_enabled is False


def test_load_broken_file_is_no_prefs(tmp_path: Path):
    (tmp_path / "notify_prefs.json").write_text("{壊れている", encoding="utf-8")
    assert load_notify_prefs(tmp_path).muted_topics == []
    (tmp_path / "notify_prefs.json").write_text('["配列"]', encoding="utf-8")
    assert load_notify_prefs(tmp_path).muted_topics == []


def test_load_cleans_topics_and_partial_quiet(tmp_path: Path):
    _write_prefs(tmp_path, {
        "muted_topics": [" 買い物 ", "", "買い物", 42, "ABC", "abc"],
        "quiet_start": "22:00",  # 終了が無いので時間帯は無効
    })
    prefs = load_notify_prefs(tmp_path)
    # 空白除去・重複（大小文字を区別しない）除去・文字列以外は捨てる
    assert prefs.muted_topics == ["買い物", "ABC"]
    assert prefs.quiet_enabled is False
    assert prefs.quiet_start is None and prefs.quiet_end is None


def test_add_and_remove_topics_roundtrip(tmp_path: Path):
    prefs = add_muted_topic(tmp_path, "買い物")
    assert prefs.muted_topics == ["買い物"]
    # 大小文字違いの重複は増えない
    prefs = add_muted_topic(tmp_path, "買い物")
    assert prefs.muted_topics == ["買い物"]
    raw = json.loads((tmp_path / "notify_prefs.json").read_text("utf-8"))
    assert raw["muted_topics"] == ["買い物"]

    prefs = remove_muted_topic(tmp_path, "買い物")
    assert prefs.muted_topics == []
    # 登録されていない語を外しても何も変わらない
    prefs = remove_muted_topic(tmp_path, "存在しない")
    assert prefs.muted_topics == []
    assert load_notify_prefs(tmp_path).muted_topics == []


def test_quiet_hours_roundtrip_and_clear(tmp_path: Path):
    prefs = set_quiet_hours(tmp_path, "22:00", "7:00")
    assert prefs.quiet_start == "22:00" and prefs.quiet_end == "07:00"
    assert load_notify_prefs(tmp_path).quiet_enabled is True
    try:
        set_quiet_hours(tmp_path, "夜", "07:00")
    except ValueError:
        pass
    else:
        raise AssertionError("不正な時間帯が通ってしまった")
    prefs = clear_quiet_hours(tmp_path)
    assert prefs.quiet_enabled is False
    assert load_notify_prefs(tmp_path).quiet_start is None


# ---------------------------------------------------------------- 時間帯の判定

def test_in_quiet_hours_crossing_midnight():
    prefs = NotifyPrefs(quiet_start="22:00", quiet_end="07:00")
    assert prefs.in_quiet_hours(datetime(2026, 11, 2, 23, 30)) is True
    assert prefs.in_quiet_hours(datetime(2026, 11, 3, 6, 30)) is True
    assert prefs.in_quiet_hours(datetime(2026, 11, 2, 22, 0)) is True   # 開始を含む
    assert prefs.in_quiet_hours(datetime(2026, 11, 3, 7, 0)) is False   # 終了を含まない
    assert prefs.in_quiet_hours(datetime(2026, 11, 2, 12, 0)) is False


def test_in_quiet_hours_same_day_and_degenerate():
    prefs = NotifyPrefs(quiet_start="09:00", quiet_end="17:00")
    assert prefs.in_quiet_hours(datetime(2026, 11, 2, 9, 0)) is True
    assert prefs.in_quiet_hours(datetime(2026, 11, 2, 16, 59)) is True
    assert prefs.in_quiet_hours(datetime(2026, 11, 2, 17, 0)) is False
    assert prefs.in_quiet_hours(datetime(2026, 11, 2, 8, 59)) is False
    # 開始 == 終了は時間帯なしとして扱う
    same = NotifyPrefs(quiet_start="09:00", quiet_end="09:00")
    assert same.in_quiet_hours(datetime(2026, 11, 2, 9, 0)) is False
    # 片方だけでは無効
    half = NotifyPrefs(quiet_start="09:00")
    assert half.in_quiet_hours(datetime(2026, 11, 2, 10, 0)) is False


# ---------------------------------------------------------------- ミュートと日報

def test_muted_task_hidden_from_digest(tmp_path: Path):
    _write_tasks(tmp_path, [
        _task("1", "買い物に行く"),
        _task("2", "申込書を出す", memo="買い物のついでに投函"),
        _task("3", "掃除をする"),
    ])
    _write_prefs(tmp_path, {"muted_topics": ["買い物"]})
    cfg = Config(data_dir=tmp_path)
    text = check_and_post(cfg, now=NOW)
    assert text is not None
    # 題名に含む用事も、メモにだけ含む用事も出ない。件数も残りだけ
    assert "買い物に行く" not in text
    assert "申込書を出す" not in text
    assert "掃除をする" in text
    assert "1 件" in text


def test_mute_matching_is_case_insensitive(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "ABC の手続き")])
    _write_prefs(tmp_path, {"muted_topics": ["abc"]})
    cfg = Config(data_dir=tmp_path)
    assert check_and_post(cfg, now=NOW) is None
    assert _records(tmp_path) == []


def test_all_muted_no_digest_no_state(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物に行く")])
    _write_prefs(tmp_path, {"muted_topics": ["買い物"]})
    cfg = Config(data_dir=tmp_path)
    assert check_and_post(cfg, now=NOW) is None
    assert _records(tmp_path) == []
    # 日報を出していないので、投稿済みの記録も作られない
    assert not (tmp_path / "proactive_state.json").exists()


def test_muted_reminder_is_held_without_mark(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物の薬", remind_at=DUE)])
    _write_prefs(tmp_path, {"muted_topics": ["買い物"]})
    cfg = Config(data_dir=tmp_path)
    assert check_and_post(cfg, now=NOW) is None
    assert _records(tmp_path) == []
    # 通知済みの印は付いていない（保留されている）
    board = TaskBoard(tmp_path)
    task = board.get("1")
    assert task is not None and task.reminded_at == ""

    # ミュートを外せば、後の巡回で通知され、印が付く
    remove_muted_topic(tmp_path, "買い物")
    text = check_and_post(cfg, now=NOW)
    assert text is not None and "買い物の薬" in text
    task = TaskBoard(tmp_path).get("1")
    assert task is not None and task.reminded_at != ""


# ---------------------------------------------------------------- 静かな時間帯

def test_quiet_hours_suppress_digest_only(tmp_path: Path):
    _write_tasks(tmp_path, [
        _task("1", "薬を飲む", remind_at=DUE),
        _task("2", "掃除をする"),
    ])
    _write_prefs(tmp_path, {"quiet_start": "08:00", "quiet_end": "10:00"})
    cfg = Config(data_dir=tmp_path)
    text = check_and_post(cfg, now=NOW)  # 9:00 は時間帯の中
    # リマインダーは時間帯でも出る
    assert text is not None and "薬を飲む" in text
    assert "通知の時刻になった用事" in text
    # 日報は出ない
    assert "進行中の用事が" not in text
    assert len(_records(tmp_path)) == 1
    # 日報の投稿済み記録は消費されていない
    assert not (tmp_path / "proactive_state.json").exists()


def test_digest_comes_after_quiet_hours(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "掃除をする")])
    _write_prefs(tmp_path, {"quiet_start": "08:00", "quiet_end": "09:30"})
    cfg = Config(data_dir=tmp_path)
    # 時間帯の中: 日報は出ない
    assert check_and_post(cfg, now=NOW) is None
    assert _records(tmp_path) == []
    # 時間帯が明ければ、同じ日の日報が出る
    later = NOW + timedelta(hours=1)  # 10:00
    text = check_and_post(cfg, now=later)
    assert text is not None and "掃除をする" in text
    assert len(_records(tmp_path)) == 1


# ---------------------------------------------------------------- 道具

def _toolbox(tmp_path: Path) -> ToolBox:
    board = TaskBoard(tmp_path)
    return ToolBox(tmp_path, task_board=board)


def test_notify_tools_registered_with_task_tools(tmp_path: Path):
    names = {s["name"] for s in _toolbox(tmp_path).schemas()}
    assert {"set_notify_mute", "clear_notify_mute",
            "list_notify_prefs"} <= names
    # 用事系の道具が無い構成（task_board なし）では出ない
    plain_names = {s["name"] for s in ToolBox(tmp_path).schemas()}
    assert "set_notify_mute" not in plain_names
    assert "list_notify_prefs" not in plain_names


def test_notify_tools_roundtrip(tmp_path: Path):
    box = _toolbox(tmp_path)
    result = box.call("set_notify_mute", {"topic": "買い物"})
    assert "買い物" in result
    assert load_notify_prefs(tmp_path).muted_topics == ["買い物"]

    listing = box.call("list_notify_prefs", {})
    assert "買い物" in listing

    # 道具で付けたミュートが巡回にも効く
    board = TaskBoard(tmp_path)
    board.add("買い物に行く")
    cfg = Config(data_dir=tmp_path)
    assert check_and_post(cfg, now=NOW) is None

    result = box.call("clear_notify_mute", {"topic": "買い物"})
    assert "買い物" in result
    assert load_notify_prefs(tmp_path).muted_topics == []
    # 登録されていない語を外すと、その旨が返る（error ではない）
    result = box.call("clear_notify_mute", {"topic": "買い物"})
    assert not result.startswith("error")
    assert "なっていません" in result


def test_notify_tools_activity_labels():
    assert activity_label("set_notify_mute") == "知らせ方の設定を更新中"
    assert activity_label("clear_notify_mute") == "知らせ方の設定を更新中"
    assert activity_label("list_notify_prefs") == "知らせ方の設定を確認中"


# ---------------------------------------------------------------- Web API

@pytest.fixture()
def server(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.read().decode("utf-8")


def _post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_notify_prefs_api_defaults_and_topics(server):
    base, tmp_path = server
    status, data = _get(base + "/api/notify_prefs")
    assert status == 200
    assert data == {"tenant": "default", "user": "default",
                    "muted_topics": [], "quiet_start": None,
                    "quiet_end": None}

    status, data = _post(base + "/api/notify_prefs",
                         {"add_topic": "買い物"})
    assert status == 200
    assert data["muted_topics"] == ["買い物"]
    assert load_notify_prefs(tmp_path).muted_topics == ["買い物"]

    status, data = _get(base + "/api/notify_prefs?tenant=default&user=default")
    assert data["muted_topics"] == ["買い物"]

    status, data = _post(base + "/api/notify_prefs",
                         {"remove_topic": "買い物"})
    assert status == 200
    assert data["muted_topics"] == []
    assert load_notify_prefs(tmp_path).muted_topics == []

    status, _data = _post(base + "/api/notify_prefs", {"add_topic": "  "})
    assert status == 400


def test_notify_prefs_api_quiet_hours(server):
    base, tmp_path = server
    status, data = _post(base + "/api/notify_prefs",
                         {"quiet_start": "22:00", "quiet_end": "07:00"})
    assert status == 200
    assert data["quiet_start"] == "22:00" and data["quiet_end"] == "07:00"
    assert load_notify_prefs(tmp_path).quiet_enabled is True

    # 片方だけ・形式不正は 400
    status, _data = _post(base + "/api/notify_prefs",
                          {"quiet_start": "22:00"})
    assert status == 400
    status, _data = _post(base + "/api/notify_prefs",
                          {"quiet_start": "夜", "quiet_end": "07:00"})
    assert status == 400

    # 両方 null で解除
    status, data = _post(base + "/api/notify_prefs",
                         {"quiet_start": None, "quiet_end": None})
    assert status == 200
    assert data["quiet_start"] is None and data["quiet_end"] is None
    assert load_notify_prefs(tmp_path).quiet_enabled is False


def test_notify_prefs_are_per_user(server):
    base, _tmp_path = server
    _post(base + "/api/notify_prefs", {"user": "alice", "add_topic": "買い物"})
    _status, data = _get(base + "/api/notify_prefs?user=alice")
    assert data["muted_topics"] == ["買い物"]
    _status, data = _get(base + "/api/notify_prefs?user=default")
    assert data["muted_topics"] == []


def test_page_has_notify_prefs_section(server):
    base, _tmp_path = server
    html = _get_text(base + "/")
    assert "知らせ方の好み" in html
    assert 'id="notifyMutedRows"' in html
    assert "api/notify_prefs" in html
