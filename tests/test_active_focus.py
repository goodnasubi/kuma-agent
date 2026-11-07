"""Active Focus 第 1 段（AF-1・AF-2）のテスト。

AF-1: 道具の失敗が種類に分けられ、活動の記録（activity.jsonl）の
tool_call 記録に error_kind として残ること。成功には付かないこと。

AF-2: 道具一般は自動再試行されないこと（失敗した結果には立て
直しの促しが付き、活動の記録には促し前の結果が残ること。承認の
却下とキャンセルには促しが付かないこと）。委任（delegate）だけ
は、失敗時に理由を添えて 1 回まで自動で委ね直されること（時間
切れでは委ね直さないこと、retry=False で抑えられること）。
"""
import time
from datetime import datetime
from pathlib import Path

import pytest

from kuma_agent import delegate as delegate_module
from kuma_agent.activity import (
    ACTIVITY_FILE,
    classify_tool_error,
    load_activity_records,
    record_tool_call,
)
from kuma_agent.config import Config
from kuma_agent.delegate import DELEGATE_NOTICE, run_delegated_task
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.tools import APPROVAL_DENIED_RESULT, CANCELLED_RESULT, Tool

NOW = datetime(2026, 11, 7, 12, 0, 0)
TASK_TEXT = "資料を調べて要点をまとめて"


def _delegate_records(cfg: Config) -> list[dict]:
    return [r for r in load_activity_records(cfg.user_data_dir, limit=500)
            if r.get("tool") == "delegate"]


# ------------------------------------------------------- AF-1 分類

def test_classify_tool_error_kinds():
    assert classify_tool_error(APPROVAL_DENIED_RESULT) == "approval_denied"
    assert classify_tool_error(CANCELLED_RESULT) == "cancelled"
    assert classify_tool_error(
        "error: 用事 #3 が見つかりません") == "not_found"
    assert classify_tool_error(
        "error: ファイルが見つかりません: a.txt") == "not_found"
    # 委任の時間切れ
    assert classify_tool_error(
        "error: 委任先のエージェントが時間内に終わりませんでした"
        "（制限 600 秒）") == "timeout"
    # MCP の応答待ちの時間切れ
    assert classify_tool_error(
        "error: MCP サーバー「x」が 30 秒以内に応答しませんでした"
    ) == "timeout"
    # run_shell などの subprocess の時間切れ
    assert classify_tool_error(
        "error: Command 'sleep 999' timed out after 120 seconds"
    ) == "timeout"
    assert classify_tool_error("error: unknown tool foo") == "unknown_tool"
    assert classify_tool_error("error: 何か壊れた") == "error"
    # 失敗でない結果は分類しない（error で始まらない「見つかりま
    # せんでした」は検索の通常の結果で、失敗扱いにしない）
    assert classify_tool_error("成功しました") is None
    assert classify_tool_error("見つかりませんでした") is None


@pytest.mark.parametrize("result,kind", [
    (APPROVAL_DENIED_RESULT, "approval_denied"),
    (CANCELLED_RESULT, "cancelled"),
    ("error: 用事 #3 が見つかりません", "not_found"),
    ("error: 委任先のエージェントが時間内に終わりませんでした"
     "（制限 600 秒）", "timeout"),
    ("error: unknown tool foo", "unknown_tool"),
    ("error: 何か壊れた", "error"),
])
def test_record_tool_call_adds_error_kind(tmp_path, result, kind):
    record_tool_call(tmp_path, "main", "ledger", "some_tool", "",
                     result, now=NOW)
    (record,) = load_activity_records(tmp_path)
    assert record["ok"] is False
    assert record["error_kind"] == kind


def test_record_tool_call_success_has_no_error_kind(tmp_path):
    record_tool_call(tmp_path, "main", "ledger", "now", "", "結果",
                     now=NOW)
    (record,) = load_activity_records(tmp_path)
    assert record["ok"] is True
    assert "error_kind" not in record


# ------------------------------------------------------- AF-2 促しと自動再試行の線引き

class _EchoToolModel:
    """1 回目に指定の道具を呼び、2 回目は道具結果をそのまま答える。"""

    def __init__(self, name: str, args: dict) -> None:
        self.rounds = 0
        self.name = name
        self.args = args

    def chat(self, system_prompt, messages, tools):
        self.rounds += 1
        if self.rounds == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": self.name, "args": self.args}])
        return Message("assistant", messages[-1].content)


def test_not_found_result_gets_hint_and_record_keeps_original(tmp_path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoToolModel("update_task", {"id": "999"}))
    answer = session.say("用事を更新して")
    # モデルへ返す結果には「名前や条件を変えて探す」方向の促しが付く
    assert "error: 用事 #999 が見つかりません" in answer
    assert "名前や条件を変えて探してみてください" in answer
    # 活動の記録には促し前の結果が残り、error_kind が付く
    (record,) = [r for r in load_activity_records(tmp_path)
                 if r["tool"] == "update_task"]
    assert record["ok"] is False
    assert record["error_kind"] == "not_found"
    assert "名前や条件を変えて" not in record["result"]


def test_failed_tool_is_not_retried_automatically(tmp_path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoToolModel("flaky", {}))
    calls: list[int] = []

    def flaky() -> str:
        calls.append(1)
        return "error: 壊れています"

    session.tools.register(Tool(
        "flaky", "失敗する道具", {"type": "object", "properties": {}},
        flaky))
    answer = session.say("やってみて")
    # 仕組みによる自動再試行は無い（呼ばれるのはモデルの 1 回だけ）
    assert calls == [1]
    # 汎用の立て直しの促しが付く
    assert "失敗しました。別の方法があれば試し" in answer
    (record,) = [r for r in load_activity_records(tmp_path)
                 if r["tool"] == "flaky"]
    assert record["error_kind"] == "error"


def test_approval_denied_gets_no_hint(tmp_path):
    cfg = Config(data_dir=tmp_path)
    session = Session(
        cfg, _EchoToolModel("run_shell", {"command": "echo hi"}),
        approver=lambda name, args: False)
    answer = session.say("実行して")
    # 承認の却下は利用者の意思なので、促しで覆さない（結果は定数のまま）
    assert answer == APPROVAL_DENIED_RESULT
    (record,) = [r for r in load_activity_records(tmp_path)
                 if r["kind"] == "tool_call" and r["tool"] == "run_shell"]
    assert record["error_kind"] == "approval_denied"


# ------------------------------------------------------- AF-2 委任の立て直し

class _RetryFlowModel:
    """親は delegate を呼び、子は fail_times 回まで例外で落ちる。"""

    def __init__(self, fail_times: int) -> None:
        self.parent_calls = 0
        self.child_attempts = 0
        self.child_tasks: list[str] = []
        self.fail_times = fail_times

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            self.child_attempts += 1
            self.child_tasks.append(messages[-1].content)
            if self.child_attempts <= self.fail_times:
                raise RuntimeError("子の頭脳が落ちた")
            return Message("assistant", "立て直した子の報告")
        self.parent_calls += 1
        if self.parent_calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "d1", "name": "delegate",
                 "args": {"task": TASK_TEXT}}])
        return Message("assistant", messages[-1].content)


def _make_parent(tmp_path: Path, monkeypatch, model,
                 **cfg_kwargs) -> tuple[Session, Config]:
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path / "data", **cfg_kwargs)
    return Session(cfg, model), cfg


def test_delegate_retries_once_with_reason_and_succeeds(
        tmp_path, monkeypatch):
    model = _RetryFlowModel(fail_times=1)
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    answer = session.say("この仕事を頼む")
    # 1 回だけ委ね直され、成功した結果が返る（頭に委ね直しの明記）
    assert model.child_attempts == 2
    assert answer.startswith("（1 回目の委任が失敗したため")
    assert "立て直した子の報告" in answer
    # 委ね直しの指示には、失敗の理由と元の依頼が含まれる
    retry_task = model.child_tasks[1]
    assert "前回の試みはこう失敗しました" in retry_task
    assert "子の頭脳が落ちた" in retry_task
    assert "別の進め方で" in retry_task
    assert TASK_TEXT in retry_task
    # 活動の記録: delegate の記録は成功で、結果に委ね直しが残る
    (record,) = _delegate_records(cfg)
    assert record["ok"] is True
    assert "error_kind" not in record
    assert "委ね直しました" in record["result"]


def test_delegate_retry_is_only_once(tmp_path, monkeypatch):
    model = _RetryFlowModel(fail_times=99)
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    answer = session.say("この仕事を頼む")
    # 委ね直しは 1 回まで。2 回目の失敗は、委ね直しの明記つきの
    # error としてそのまま返る
    assert model.child_attempts == 2
    assert answer.startswith("error:")
    assert "再試行も失敗しました" in answer
    assert "子の頭脳が落ちた" in answer
    (record,) = _delegate_records(cfg)
    assert record["ok"] is False
    assert record["error_kind"] == "error"
    assert "再試行も失敗しました" in record["result"]


def test_delegate_retry_can_be_disabled(tmp_path, monkeypatch):
    model = _RetryFlowModel(fail_times=99)
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    result = run_delegated_task(session, TASK_TEXT, retry=False)
    assert model.child_attempts == 1
    assert result == "error: 委任先のエージェントで失敗しました: 子の頭脳が落ちた"


class _SlowChildModel(_RetryFlowModel):
    """子として呼ばれると制限時間より長く眠るスタブ。"""

    def __init__(self) -> None:
        super().__init__(fail_times=0)

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            self.child_attempts += 1
            time.sleep(2)
            return Message("assistant", "遅い報告")
        return super().chat(system_prompt, messages, tools)


def test_delegate_timeout_is_not_retried(tmp_path, monkeypatch):
    model = _SlowChildModel()
    session, cfg = _make_parent(
        tmp_path, monkeypatch, model, delegate_timeout_seconds=1)
    answer = session.say("この仕事を頼む")
    # 時間切れは従来どおりの error が返り、委ね直しはされない
    assert "時間内に終わりませんでした" in answer
    assert "再試行も失敗しました" not in answer
    # 子のスレッドが終わるまで待ってから、試行回数が 1 回だけ
    # だったことを確かめる
    assert delegate_module.wait_child_threads(5)
    assert model.child_attempts == 1
    # 活動の記録では delegate の失敗が timeout として残る
    (record,) = _delegate_records(cfg)
    assert record["ok"] is False
    assert record["error_kind"] == "timeout"
    assert "別の方法があれば試し" not in record["result"]
