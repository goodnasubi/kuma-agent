"""承認（危険な道具の実行前確認）のテスト。

対象の分類、ToolBox の承認フック、テナント設定（settings.json）、
Session の配線（承認・却下・承認者なし・設定 OFF・状態通知）、
Web の保留→決定の流れ、CLI と TUI の承認者を検証する。
"""
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kuma_agent import cli as cli_module
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import APPROVAL_DENIED, Session, approval_waiting_text
from kuma_agent.settings import (
    approvals_enabled_for,
    load_tenant_settings,
    proactive_enabled_for,
    set_approvals_enabled,
    set_proactive_enabled,
)
from kuma_agent.tools import (
    APPROVAL_DENIED_RESULT,
    Tool,
    ToolBox,
    requires_approval,
    summarize_tool_args,
)
from kuma_agent.tui import TuiApprover, approval_prompt_text
from kuma_agent.web import ApprovalBroker, build_server, render_page


# ---------------------------------------------------------------- 分類と要約

def test_requires_approval_targets():
    assert requires_approval("run_shell") is True
    # MCP 道具（<サーバー名>__<道具名>）はすべて対象
    assert requires_approval("fake__add") is True
    assert requires_approval("github__create_issue") is True


def test_requires_approval_leaves_normal_tools():
    for name in ("read_file", "write_file", "edit_file", "now",
                 "list_tasks", "create_task", "update_task", "remember"):
        assert requires_approval(name) is False


def test_summarize_tool_args():
    # run_shell はコマンド文字列そのものが判断材料
    assert summarize_tool_args("run_shell", {"command": "echo hi"}) == "echo hi"
    assert summarize_tool_args("fake__add", {"a": 1}) == '{"a": 1}'
    assert summarize_tool_args("read_file", {}) == ""
    # 改行は潰して 1 行にし、長すぎる場合は切り詰める
    assert summarize_tool_args("run_shell", {"command": "echo a\necho b"}) \
        == "echo a echo b"
    long = summarize_tool_args("run_shell", {"command": "x" * 500})
    assert len(long) <= 201 and long.endswith("…")


# ---------------------------------------------------------------- テナント設定

def test_approvals_enabled_defaults_to_true(tmp_path: Path):
    assert approvals_enabled_for(tmp_path, "default") is True
    assert approvals_enabled_for(tmp_path, "acme") is True


def test_set_approvals_enabled_roundtrip(tmp_path: Path):
    set_approvals_enabled(tmp_path, "acme", False)
    assert approvals_enabled_for(tmp_path, "acme") is False
    assert approvals_enabled_for(tmp_path, "other") is True
    assert load_tenant_settings(tmp_path) == {
        "acme": {"approvals_enabled": False}}
    set_approvals_enabled(tmp_path, "acme", True)
    assert approvals_enabled_for(tmp_path, "acme") is True


def test_approvals_setting_preserves_proactive(tmp_path: Path):
    # 能動発信と承認は同じ settings.json の別項目として共存する
    set_proactive_enabled(tmp_path, "acme", False)
    set_approvals_enabled(tmp_path, "acme", False)
    assert proactive_enabled_for(tmp_path, "acme") is False
    assert approvals_enabled_for(tmp_path, "acme") is False
    assert load_tenant_settings(tmp_path) == {
        "acme": {"proactive_enabled": False, "approvals_enabled": False}}
    set_approvals_enabled(tmp_path, "acme", True)
    assert proactive_enabled_for(tmp_path, "acme") is False


def test_approvals_setting_ignores_non_bool(tmp_path: Path):
    (tmp_path / "settings.json").write_text(json.dumps(
        {"tenants": {"acme": {"approvals_enabled": "no"}}}), encoding="utf-8")
    assert approvals_enabled_for(tmp_path, "acme") is True  # 既定（安全側）
    assert load_tenant_settings(tmp_path) == {}


# ---------------------------------------------------------------- ToolBox のフック

def test_toolbox_without_hook_runs_shell(tmp_path: Path):
    # フックが差し込まれていない ToolBox は従来どおり実行する
    box = ToolBox(tmp_path)
    assert "hi" in box.call("run_shell", {"command": "echo hi"})


def test_toolbox_hook_approves_and_denies(tmp_path: Path):
    box = ToolBox(tmp_path)
    marker = tmp_path / "ran.txt"
    command = f"echo ran > {marker.name}"
    box.approval_check = lambda name, args: True
    assert "error" not in box.call("run_shell", {"command": command})
    assert marker.exists()

    marker.unlink()
    seen: list[tuple[str, dict]] = []

    def deny(name: str, args: dict) -> bool:
        seen.append((name, args))
        return False

    box.approval_check = deny
    assert box.call("run_shell", {"command": command}) == APPROVAL_DENIED_RESULT
    assert not marker.exists()  # 却下されたら実行されていない
    assert seen == [("run_shell", {"command": command})]


def test_toolbox_hook_exception_denies(tmp_path: Path):
    box = ToolBox(tmp_path)

    def broken(name: str, args: dict) -> bool:
        raise RuntimeError("承認側の失敗")

    box.approval_check = broken
    assert box.call("run_shell", {"command": "echo hi"}) == APPROVAL_DENIED_RESULT


def test_toolbox_hook_not_called_for_normal_tools(tmp_path: Path):
    box = ToolBox(tmp_path)
    calls: list[str] = []
    box.approval_check = lambda name, args: calls.append(name) or True
    assert "wrote" in box.call("write_file", {"path": "a.txt", "content": "x"})
    assert calls == []


# ---------------------------------------------------------------- Session の配線

class _ShellModel:
    """1 回目で run_shell を呼び、2 回目で道具結果を使って答えるスタブ。"""

    def __init__(self, command: str = "echo hi-from-shell") -> None:
        self.calls = 0
        self.command = command
        self.tool_result: str | None = None

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": "run_shell",
                 "args": {"command": self.command}}])
        self.tool_result = messages[-1].content
        return Message("assistant", f"結果: {messages[-1].content}")


@pytest.fixture()
def shell_session(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # run_shell の作業場所を tmp にする
    model = _ShellModel()
    session = Session(Config(data_dir=tmp_path / "data"), model)
    return session, model


def test_session_approved_shell_runs(shell_session):
    session, model = shell_session
    seen: list[tuple[str, dict]] = []

    def approver(name: str, args: dict) -> bool:
        seen.append((name, args))
        return True

    session.approver = approver
    assert session.say("実行して") == "結果: hi-from-shell"
    assert seen == [("run_shell", {"command": "echo hi-from-shell"})]


def test_session_denied_shell_returns_error_and_continues(shell_session):
    session, model = shell_session
    session.approver = lambda name, args: False
    # 却下は例外ではなく道具結果としてモデルへ返り、会話は続く
    assert session.say("実行して") == f"結果: {APPROVAL_DENIED_RESULT}"
    assert model.tool_result == APPROVAL_DENIED_RESULT


def test_session_without_approver_denies(shell_session):
    # 承認者が付いていない経路では安全側に倒して却下する
    session, _model = shell_session
    assert session.approver is None
    assert session.say("実行して") == f"結果: {APPROVAL_DENIED_RESULT}"


def test_session_approvals_disabled_skips_approver(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "settings.json").write_text(json.dumps(
        {"tenants": {"default": {"approvals_enabled": False}}}),
        encoding="utf-8")
    model = _ShellModel()
    session = Session(Config(data_dir=data_dir), model)
    calls: list[str] = []
    session.approver = lambda name, args: calls.append(name) or False
    # 設定 OFF では承認者を呼ばず、そのまま実行される
    assert session.approvals_enabled is False
    assert session.say("実行して") == "結果: hi-from-shell"
    assert calls == []


def test_session_notifies_approval_waiting(shell_session):
    session, _model = shell_session
    statuses: list[str] = []
    session.on_status = statuses.append
    session.approver = lambda name, args: True
    session.say("実行して")
    assert approval_waiting_text("run_shell") in statuses
    assert statuses[-1] == ""


def test_session_notifies_approval_denied(shell_session):
    session, _model = shell_session
    statuses: list[str] = []
    session.on_status = statuses.append
    session.approver = lambda name, args: False
    session.say("実行して")
    assert approval_waiting_text("run_shell") in statuses
    assert APPROVAL_DENIED in statuses


def test_session_gates_mcp_named_tools(tmp_path: Path):
    # MCP 道具（__ を含む名前）も Session の道具として承認対象になる
    model = _NamedToolModel()
    session = Session(Config(data_dir=tmp_path), model)
    session.tools.register(Tool("fake__thing", "偽の MCP 道具",
                                {"type": "object", "properties": {}},
                                lambda: "動きました"))
    assert session.say("使って") == f"結果: {APPROVAL_DENIED_RESULT}"
    session.approver = lambda name, args: True
    model.calls = 0
    assert session.say("使って") == "結果: 動きました"


class _NamedToolModel:
    """1 回目で fake__thing を呼び、2 回目で道具結果を使って答えるスタブ。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": "fake__thing", "args": {}}])
        return Message("assistant", f"結果: {messages[-1].content}")


# ---------------------------------------------------------------- ブローカー

def test_broker_decide_releases_waiter():
    broker = ApprovalBroker()
    outcomes: list[bool] = []

    def wait() -> None:
        outcomes.append(broker.request(
            "default", "default", "run_shell", {"command": "echo x"},
            timeout=10))

    waiter = threading.Thread(target=wait)
    waiter.start()
    try:
        deadline = time.monotonic() + 5
        pending: list[dict] = []
        while time.monotonic() < deadline:
            pending = broker.list_pending("default", "default")
            if pending:
                break
            time.sleep(0.02)
        assert len(pending) == 1
        entry = pending[0]
        assert entry["tool"] == "run_shell"
        assert entry["summary"] == "echo x"
        assert entry["time"]
        # 別の組み合わせからは見えない・決められない
        assert broker.list_pending("other", "default") == []
        assert broker.decide("other", "default", entry["id"], True) is False
        assert broker.decide("default", "default", entry["id"], True) is True
    finally:
        waiter.join(timeout=5)
    assert outcomes == [True]
    assert broker.list_pending("default", "default") == []


def test_broker_timeout_denies():
    broker = ApprovalBroker()
    started = time.monotonic()
    assert broker.request("default", "default", "run_shell",
                          {"command": "echo x"}, timeout=0.2) is False
    assert time.monotonic() - started < 5
    # 時間切れの保留は残らない
    assert broker.list_pending("default", "default") == []


def test_broker_decide_unknown_id():
    broker = ApprovalBroker()
    assert broker.decide("default", "default", "nope", True) is False


# ---------------------------------------------------------------- Web の流れ

@pytest.fixture()
def shell_server(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    model = _ShellModel()
    session = Session(Config(data_dir=tmp_path / "data"), model)
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", model, tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post_chat_async(base: str) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def post() -> None:
        status, data = _post(base + "/api/chat", {"message": "実行して"})
        result["status"] = status
        result.update(data)

    thread = threading.Thread(target=post)
    thread.start()
    return thread, result


def _wait_pending(base: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        _status, data = _get(base + "/api/approvals")
        if data["approvals"]:
            return data["approvals"][0]
        time.sleep(0.05)
    raise AssertionError("承認の保留が現れませんでした")


def test_web_approval_flow_approve(shell_server):
    base, _model, _tmp = shell_server
    thread, result = _post_chat_async(base)
    try:
        entry = _wait_pending(base)
        assert entry["tool"] == "run_shell"
        assert entry["summary"] == "echo hi-from-shell"
        status, data = _post(base + "/api/approvals/decide",
                             {"id": entry["id"], "approved": True})
        assert status == 200 and data["decided"] is True
    finally:
        thread.join(timeout=15)
    # 許可で会話が進み、コマンドの出力が道具結果になっている
    assert result["reply"] == "結果: hi-from-shell"
    _status, data = _get(base + "/api/approvals")
    assert data["approvals"] == []


def test_web_approval_flow_deny(shell_server):
    base, _model, _tmp = shell_server
    thread, result = _post_chat_async(base)
    try:
        entry = _wait_pending(base)
        status, _data = _post(base + "/api/approvals/decide",
                              {"id": entry["id"], "approved": False})
        assert status == 200
    finally:
        thread.join(timeout=15)
    # 却下は error としてモデルへ返り、会話は応答まで進む
    assert result["reply"] == f"結果: {APPROVAL_DENIED_RESULT}"


def test_web_decide_rejects_bad_requests(shell_server):
    base, _model, _tmp = shell_server
    status, _ = _post(base + "/api/approvals/decide",
                      {"id": "x", "approved": "yes"})
    assert status == 400
    status, _ = _post(base + "/api/approvals/decide",
                      {"id": "missing", "approved": True})
    assert status == 404


def test_web_tenant_settings_include_approvals(shell_server):
    base, _model, tmp_path = shell_server
    status, data = _get(base + "/api/tenant_settings?tenant=acme")
    assert status == 200 and data["approvals_enabled"] is True
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "approvals_enabled": False})
    assert status == 200 and data["approvals_enabled"] is False
    assert approvals_enabled_for(tmp_path / "data", "acme") is False
    # 能動発信の設定には触っていない
    assert data["proactive_enabled"] is True
    status, _ = _post(base + "/api/tenant_settings",
                      {"tenant": "acme", "approvals_enabled": "no"})
    assert status == 400


def test_page_contains_approval_panel():
    page = render_page("muse", avatar_svg="<svg></svg>")
    assert "approvalPanel" in page
    assert "/api/approvals" in page and "/api/approvals/decide" in page
    assert "承認の確認" in page and "承認確認" in page


# ---------------------------------------------------------------- CLI の承認者

@pytest.mark.parametrize("answer,expected", [
    ("y", True), ("Y", True), ("yes", True),
    ("n", False), ("", False), ("はい", False),
])
def test_cli_approver_answers(monkeypatch, answer, expected):
    monkeypatch.setattr("builtins.input", lambda prompt="": answer)
    assert cli_module._cli_approver(
        "run_shell", {"command": "echo hi"}) is expected


def test_cli_approver_eof_denies(monkeypatch):
    def raise_eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert cli_module._cli_approver("run_shell", {"command": "x"}) is False


# ---------------------------------------------------------------- TUI の承認者

def _run_approver(approver: TuiApprover) -> tuple[threading.Thread, list]:
    outcomes: list[bool] = []
    thread = threading.Thread(
        target=lambda: outcomes.append(
            approver("run_shell", {"command": "echo hi"})))
    thread.start()
    return thread, outcomes


def _wait_tui_pending(approver: TuiApprover) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        pending = approver.pending()
        if pending is not None:
            return pending
        time.sleep(0.02)
    raise AssertionError("承認待ちになりませんでした")


def test_tui_approver_yes_and_no():
    approver = TuiApprover(timeout=10)
    thread, outcomes = _run_approver(approver)
    pending = _wait_tui_pending(approver)
    assert pending == {"name": "run_shell", "summary": "echo hi"}
    assert approver.answer("y") is True
    thread.join(timeout=5)
    assert outcomes == [True]
    assert approver.pending() is None

    thread, outcomes = _run_approver(approver)
    _wait_tui_pending(approver)
    assert approver.answer("n") is True
    thread.join(timeout=5)
    assert outcomes == [False]


def test_tui_approver_timeout_denies():
    approver = TuiApprover(timeout=0.2)
    assert approver("run_shell", {"command": "echo hi"}) is False
    assert approver.pending() is None


def test_tui_approver_answer_without_pending():
    assert TuiApprover().answer("y") is False


def test_tui_approval_prompt_text():
    text = approval_prompt_text({"name": "run_shell", "summary": "echo hi"})
    assert "run_shell" in text and "echo hi" in text and "y" in text


# ---------------------------------------------------------------- 設定値

def test_config_approval_timeout_default_and_env(monkeypatch):
    monkeypatch.delenv("KUMA_APPROVAL_TIMEOUT_SECONDS", raising=False)
    assert Config().approval_timeout_seconds == 300
    monkeypatch.setenv("KUMA_APPROVAL_TIMEOUT_SECONDS", "12")
    assert Config().approval_timeout_seconds == 12
    # 0 以下は既定値として扱う
    assert Config(approval_timeout_seconds=0).approval_timeout_seconds == 300
