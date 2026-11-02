"""MCP クライアント対応（mcp.json の読み込み・道具の発見と実行）のテスト。

道具の発見と実行のテストには、tests/fake_mcp_server.py（stdio で
答える小さな偽サーバー）を子プロセスとして起動する。
"""
import json
import sys
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.mcp_client import (
    McpManager,
    McpServer,
    load_server_configs,
)
from kuma_agent.session import Session
from kuma_agent.tools import activity_label

FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"


def _write_mcp_json(data_dir: Path, servers: object) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "mcp.json").write_text(
        json.dumps({"servers": servers}, ensure_ascii=False),
        encoding="utf-8")


def _fake_server_entry(**overrides: object) -> dict:
    entry: dict = {"name": "fake", "command": sys.executable,
                   "args": [str(FAKE_SERVER)]}
    entry.update(overrides)
    return entry


@pytest.fixture()
def manager(tmp_path: Path):
    """偽サーバー 1 件を設定したマネージャ。用後はプロセスを止める。"""
    _write_mcp_json(tmp_path, [_fake_server_entry(
        env={"FAKE_MCP_MARKER": "hello-mcp"})])
    mgr = McpManager(tmp_path, timeout_seconds=10)
    try:
        yield mgr
    finally:
        mgr.close()


# ---------------------------------------------------------------- 設定

def test_load_configs_missing_file(tmp_path: Path):
    assert load_server_configs(tmp_path) == []


def test_load_configs_broken_json(tmp_path: Path):
    (tmp_path / "mcp.json").write_text("{壊れた JSON", encoding="utf-8")
    assert load_server_configs(tmp_path) == []


def test_load_configs_valid_and_invalid_entries(tmp_path: Path):
    _write_mcp_json(tmp_path, [
        _fake_server_entry(env={"K": "V"}),
        {"name": "no-command"},                       # command が無い
        {"command": "x"},                             # name が無い
        {"name": "bad-args", "command": "x", "args": "not-a-list"},
        {"name": "bad-env", "command": "x", "env": {"K": 1}},
        "not-a-dict",
        _fake_server_entry(name="fake"),              # 同名は最初の 1 件だけ
    ])
    configs = load_server_configs(tmp_path)
    assert len(configs) == 1
    config = configs[0]
    assert config.name == "fake"
    assert config.command == sys.executable
    assert config.args == (str(FAKE_SERVER),)
    assert config.env == {"K": "V"}


def test_load_configs_servers_not_a_list(tmp_path: Path):
    (tmp_path / "mcp.json").write_text(
        json.dumps({"servers": {"name": "x"}}), encoding="utf-8")
    assert load_server_configs(tmp_path) == []


# ---------------------------------------------------------------- 発見と実行

def test_tools_discovered_with_prefixed_names(manager: McpManager):
    schemas = manager.tool_schemas()
    names = {s["name"] for s in schemas}
    assert names == {"fake__add", "fake__fail", "fake__marker"}
    add = next(s for s in schemas if s["name"] == "fake__add")
    assert add["description"] == "2 つの数を足す"
    # 入力スキーマはサーバーのものをそのまま使う
    assert add["parameters"]["required"] == ["a", "b"]
    assert set(add["parameters"]["properties"]) == {"a", "b"}


def test_tool_call_reaches_server(manager: McpManager):
    manager.tool_schemas()
    assert manager.call("fake__add", {"a": 2, "b": 3}) == "5"
    assert manager.call("fake__add", {"a": 10, "b": -4}) == "6"


def test_tool_call_passes_configured_env(manager: McpManager):
    manager.tool_schemas()
    assert manager.call("fake__marker", {}) == "hello-mcp"


def test_error_tool_returns_failure_text(manager: McpManager):
    manager.tool_schemas()
    result = manager.call("fake__fail", {})
    assert result.startswith("error:")
    assert "失敗しました" in result


def test_unknown_prefixed_tool_is_error(manager: McpManager):
    manager.tool_schemas()
    assert manager.call("fake__nope", {}).startswith("error:")


def test_jsonrpc_error_for_unknown_tool_on_server(tmp_path: Path):
    """存在しない道具への JSON-RPC エラー（v2 世代の -32602）も失敗になる。"""
    _write_mcp_json(tmp_path, [_fake_server_entry()])
    configs = load_server_configs(tmp_path)
    server = McpServer(configs[0], timeout=10)
    try:
        assert server.ensure_tools()  # 起動できる
        result = server.call("nope", {})
        assert result.startswith("error:")
        assert "-32602" in result
        assert "Unknown tool" in result
    finally:
        server.close()


def test_unstartable_server_yields_no_tools(tmp_path: Path):
    """起動できないサーバーは道具が出ないだけ（例外にしない）。"""
    _write_mcp_json(tmp_path, [
        {"name": "ghost", "command": "/nonexistent/mcp-server-xyz"},
    ])
    mgr = McpManager(tmp_path, timeout_seconds=10)
    try:
        assert mgr.tool_schemas() == []
        assert mgr.call("ghost__add", {}) == "error: unknown tool ghost__add"
    finally:
        mgr.close()


def test_activity_label_for_mcp_tool():
    assert activity_label("fake__add") == "道具を使っています（fake）"
    # 組み込み・未知の道具の表示は従来どおり
    assert activity_label("search_web") == "道具を実行中（search_web）"


# ---------------------------------------------------------------- Session 統合

class _AddModel:
    """1 回目で fake__add を呼び、2 回目で道具結果を使って答えるスタブ。"""

    def __init__(self) -> None:
        self.calls = 0
        self.tool_names: list[str] = []

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        self.tool_names = [t["name"] for t in tools]
        if self.calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": "fake__add", "args": {"a": 1, "b": 2}}])
        tool_result = messages[-1].content
        return Message("assistant", f"答えは{tool_result}です")


class _PlainModel:
    def chat(self, system_prompt, messages, tools):
        self.tool_names = [t["name"] for t in tools]
        return Message("assistant", "はい")


def test_session_exposes_and_calls_mcp_tools(tmp_path: Path):
    _write_mcp_json(tmp_path, [_fake_server_entry()])
    model = _AddModel()
    mgr = McpManager(tmp_path, timeout_seconds=10)
    try:
        session = Session(Config(data_dir=tmp_path), model, mcp_manager=mgr)
        assert session.say("足して") == "答えは3です"
        assert "fake__add" in model.tool_names
    finally:
        mgr.close()


def test_session_without_mcp_json_has_no_mcp_tools(tmp_path: Path):
    model = _PlainModel()
    session = Session(Config(data_dir=tmp_path), model)
    session.say("こんにちは")
    assert not any("__" in name for name in model.tool_names)
    assert session.mcp_manager is not None
    assert not session.mcp_manager.has_servers()


def test_session_with_unstartable_server_still_works(tmp_path: Path):
    """MCP サーバーが起動できなくても会話自体は落ちない。"""
    _write_mcp_json(tmp_path, [
        {"name": "ghost", "command": "/nonexistent/mcp-server-xyz"},
    ])
    model = _PlainModel()
    session = Session(Config(data_dir=tmp_path), model)
    assert session.say("こんにちは") == "はい"
    assert not any(name.startswith("ghost__") for name in model.tool_names)


def test_session_cli_provider_has_no_mcp_tools(tmp_path: Path):
    """CLI 系の頭脳は道具を自前管理するため、MCP 道具も出さない。"""
    _write_mcp_json(tmp_path, [_fake_server_entry()])
    model = _PlainModel()
    cfg = Config(data_dir=tmp_path, provider="claude-code")
    session = Session(cfg, model)
    assert session.mcp_manager is None
    assert session.say("こんにちは") == "はい"
    assert "fake__add" not in model.tool_names
