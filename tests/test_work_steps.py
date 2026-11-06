"""作業の手順（エージェント自身の作業メモ）のテスト。

保存と読み出し（会話ごとの分離、壊れたファイル）、置き換え時の
検証（件数の上限・「進行中」は 1 つだけ・不正な状態）、道具の
登録と実行（両モード）、前回の手順のプロンプトへの注入、
/api/status への露出と画面の表示部品を確認する。
"""
import json
import threading
import urllib.request
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.tools import activity_label
from kuma_agent.web import build_server, render_page
from kuma_agent.worksteps import (
    MAX_STEPS,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    WorkSteps,
    format_steps,
)


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


# ------------------------------------------------------------ 保存と読み出し

def test_set_and_reload(tmp_path: Path):
    steps = WorkSteps(tmp_path)
    steps.set_items([("調べる", STATUS_IN_PROGRESS), ("まとめる", STATUS_PENDING)])

    loaded = WorkSteps(tmp_path).items()
    assert [(s.text, s.status) for s in loaded] == [
        ("調べる", "in_progress"), ("まとめる", "pending")]
    assert (tmp_path / "work-steps" / "main.json").exists()


def test_scopes_are_separate(tmp_path: Path):
    WorkSteps(tmp_path, "main").set_items([("本流の手順", STATUS_PENDING)])
    WorkSteps(tmp_path, "work").set_items([("仕事の手順", STATUS_PENDING)])

    assert [s.text for s in WorkSteps(tmp_path, "main").items()] == ["本流の手順"]
    assert [s.text for s in WorkSteps(tmp_path, "work").items()] == ["仕事の手順"]


def test_broken_file_starts_empty(tmp_path: Path):
    path = tmp_path / "work-steps" / "main.json"
    path.parent.mkdir(parents=True)
    path.write_text("{壊れた", encoding="utf-8")
    assert WorkSteps(tmp_path).items() == []

    path.write_text('[{"text": 123}, {"text": "有効", "status": "変"}]',
                    encoding="utf-8")
    loaded = WorkSteps(tmp_path).items()
    # 形が不正な行は読み飛ばし、不正な状態は未着手に倒す
    assert [(s.text, s.status) for s in loaded] == [("有効", "pending")]


def test_replace_and_clear(tmp_path: Path):
    steps = WorkSteps(tmp_path)
    steps.set_items([("一つ目", STATUS_PENDING)])
    steps.set_items([("二つ目", STATUS_COMPLETED)])
    assert [s.text for s in WorkSteps(tmp_path).items()] == ["二つ目"]
    steps.set_items([])
    assert WorkSteps(tmp_path).items() == []
    assert not steps.has_open()


def test_has_open(tmp_path: Path):
    steps = WorkSteps(tmp_path)
    assert not steps.has_open()
    steps.set_items([("済んだ", STATUS_COMPLETED), ("残り", STATUS_PENDING)])
    assert steps.has_open()


# ------------------------------------------------------------ 検証

def test_validation_keeps_previous(tmp_path: Path):
    steps = WorkSteps(tmp_path)
    steps.set_items([("元の手順", STATUS_PENDING)])
    bad_inputs = [
        [("x", STATUS_PENDING)] * (MAX_STEPS + 1),          # 件数の上限
        [("a", STATUS_IN_PROGRESS), ("b", STATUS_IN_PROGRESS)],  # 進行中が複数
        [("a", "unknown")],                                  # 不正な状態
        [("  ", STATUS_PENDING)],                            # 空の文面
    ]
    for bad in bad_inputs:
        with pytest.raises(ValueError):
            steps.set_items(bad)
    assert [s.text for s in WorkSteps(tmp_path).items()] == ["元の手順"]


def test_format_steps():
    steps = WorkSteps(Path("/nonexistent"))
    assert format_steps([]) == "（作業の手順はありません）"
    text = format_steps(steps.set_items([
        ("調べる", STATUS_COMPLETED), ("書く", STATUS_IN_PROGRESS)]))
    assert text == "1. [完了] 調べる\n2. [進行中] 書く"


# ------------------------------------------------------------ 道具

def test_tool_registered_in_both_modes(tmp_path: Path):
    ledger = Session(Config(data_dir=tmp_path / "a"), _EchoModel())
    wall = Session(Config(data_dir=tmp_path / "b", mode="wall"),
                   _EchoModel(), "work")
    for session in (ledger, wall):
        names = {s["name"] for s in session.tools.schemas()}
        assert "set_work_steps" in names


def test_tool_call_roundtrip(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    result = session.tools.call("set_work_steps", {"steps": [
        {"text": "資料を読む", "status": "in_progress"},
        {"text": "要約する"}]})
    assert result.startswith("作業の手順を記録しました:")
    assert "1. [進行中] 資料を読む" in result
    assert "2. [未着手] 要約する" in result
    assert [s.text for s in session.work_steps.items()] == ["資料を読む", "要約する"]

    assert session.tools.call("set_work_steps", {"steps": []}) == \
        "作業の手順を消しました"
    assert session.work_steps.items() == []


def test_tool_call_errors(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    result = session.tools.call("set_work_steps", {"steps": [
        {"text": "a", "status": "in_progress"},
        {"text": "b", "status": "in_progress"}]})
    assert result.startswith("error:")
    assert session.work_steps.items() == []
    result = session.tools.call("set_work_steps", {"steps": "手順"})
    assert result.startswith("error:")


def test_activity_label():
    assert activity_label("set_work_steps") == "作業の手順を記録中"


def test_wall_mode_scopes_steps_per_thread(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, mode="wall")
    work = Session(cfg, _EchoModel(), "work")
    work.tools.call("set_work_steps",
                    {"steps": [{"text": "仕事の手順", "status": "pending"}]})
    private = Session(cfg, _EchoModel(), "private")
    assert private.work_steps.items() == []
    assert [s.text for s in Session(cfg, _EchoModel(), "work").work_steps.items()] \
        == ["仕事の手順"]


# ------------------------------------------------------------ プロンプトへの注入

def test_open_steps_injected_into_prompt(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    first = Session(cfg, _EchoModel())
    first.tools.call("set_work_steps", {"steps": [
        {"text": "続きの作業", "status": "in_progress"}]})

    second = Session(cfg, _EchoModel())
    assert "# 作業の手順（前回の続き）" in second.system_prompt
    assert "続きの作業" in second.system_prompt


def test_finished_steps_not_injected(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    first = Session(cfg, _EchoModel())
    first.tools.call("set_work_steps", {"steps": [
        {"text": "終わった作業", "status": "completed"}]})

    second = Session(cfg, _EchoModel())
    assert "作業の手順（前回の続き）" not in second.system_prompt


# ------------------------------------------------------------ Web

@pytest.fixture()
def server(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    session.work_steps.set_items([
        ("資料を読む", STATUS_IN_PROGRESS), ("要約する", STATUS_PENDING)])
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_status_endpoint_includes_steps(server):
    with urllib.request.urlopen(server + "/api/status", timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    assert data["steps"] == [
        {"text": "資料を読む", "status": "in_progress"},
        {"text": "要約する", "status": "pending"}]


def test_page_has_steps_area():
    html = render_page("muse")
    assert 'id="statusSteps"' in html
    assert "renderStatusSteps" in html
