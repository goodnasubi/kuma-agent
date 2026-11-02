"""テナント別 MCP 設定（テナントの置き場の mcp.json）のテスト。

テナントの置き場に mcp.json があれば、そのテナントの会話では
その設定だけを使い（ルートの設定とは混ぜない）、無ければ従来
どおりデータ置き場ルートの設定を使うことを確認する。偽サーバー
は tests/fake_mcp_server.py で、サーバー名と FAKE_MCP_MARKER の
値で「どの設定の・どのプロセスか」を区別する。
"""
import json
import sys
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.mcp_client import close_shared_managers, resolve_config_dir
from kuma_agent.memory import tenant_dir
from kuma_agent.session import Session

FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"


@pytest.fixture(autouse=True)
def _close_managers():
    """共有マネージャ経由で起動した偽サーバーをテストごとに止める。"""
    yield
    close_shared_managers()


def _write_mcp_json(place: Path, name: str, marker: str) -> None:
    place.mkdir(parents=True, exist_ok=True)
    (place / "mcp.json").write_text(json.dumps({"servers": [
        {"name": name, "command": sys.executable,
         "args": [str(FAKE_SERVER)],
         "env": {"FAKE_MCP_MARKER": marker}},
    ]}, ensure_ascii=False), encoding="utf-8")


def _disable_approvals(data_dir: Path, *tenants: str) -> None:
    """MCP 道具を実行まで通すテスト用に、対象テナントの承認を切る。"""
    (data_dir / "settings.json").write_text(json.dumps(
        {"tenants": {t: {"approvals_enabled": False} for t in tenants}}),
        encoding="utf-8")


class _RecordingModel:
    """モデルへ渡された道具名を記録するだけのスタブ。"""

    def __init__(self) -> None:
        self.tool_names: list[str] = []

    def chat(self, system_prompt, messages, tools):
        self.tool_names = [t["name"] for t in tools]
        return Message("assistant", "はい")


class _CallModel:
    """1 回目で指定の道具を呼び、2 回目で結果を使って答えるスタブ。"""

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.calls = 0
        self.tool_names: list[str] = []

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        self.tool_names = [t["name"] for t in tools]
        if self.calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": self.tool_name,
                 "args": {"a": 1, "b": 2}}])
        return Message("assistant", f"答えは{messages[-1].content}です")


def _tool_names(session: Session, model: _RecordingModel) -> list[str]:
    session.say("こんにちは")
    return model.tool_names


# ---------------------------------------------------------------- 解決

def test_resolve_config_dir_prefers_tenant_file(tmp_path: Path):
    assert resolve_config_dir(tmp_path, "acme") == tmp_path
    _write_mcp_json(tenant_dir(tmp_path, "acme"), "acmefake", "A")
    assert (resolve_config_dir(tmp_path, "acme")
            == tenant_dir(tmp_path, "acme"))
    # 別テナントには影響しない
    assert resolve_config_dir(tmp_path, "beta") == tmp_path


def test_resolve_config_dir_default_tenant_is_root(tmp_path: Path):
    _write_mcp_json(tmp_path, "rootfake", "R")
    assert resolve_config_dir(tmp_path, "default") == tmp_path
    assert resolve_config_dir(tmp_path, None) == tmp_path


# ---------------------------------------------------------------- Session 統合

def test_tenant_config_replaces_root_for_that_tenant(tmp_path: Path):
    """テナント別設定があるテナントでは、その道具だけが出て実行できる。"""
    _write_mcp_json(tmp_path, "rootfake", "root-marker")
    _write_mcp_json(tenant_dir(tmp_path, "acme"), "acmefake", "acme-marker")
    _disable_approvals(tmp_path, "acme")
    model = _CallModel("acmefake__add")
    session = Session(Config(data_dir=tmp_path, tenant="acme"), model)

    assert session.say("足して") == "答えは3です"
    assert "acmefake__add" in model.tool_names
    # ルートの設定の道具は混ざらない
    assert not any(n.startswith("rootfake__") for n in model.tool_names)
    # マネージャはテナントの置き場のもので、プロセスもテナント側
    assert session.mcp_manager is not None
    assert session.mcp_manager.data_dir == tenant_dir(tmp_path, "acme")
    assert session.mcp_manager.call("acmefake__marker", {}) == "acme-marker"


def test_tenant_without_config_uses_root(tmp_path: Path):
    """テナント別設定の無いテナントは、ルートの設定の道具が出る。"""
    _write_mcp_json(tmp_path, "rootfake", "root-marker")
    _write_mcp_json(tenant_dir(tmp_path, "acme"), "acmefake", "acme-marker")
    beta_model = _RecordingModel()
    beta = Session(Config(data_dir=tmp_path, tenant="beta"), beta_model)
    names = _tool_names(beta, beta_model)
    assert any(n.startswith("rootfake__") for n in names)
    assert not any(n.startswith("acmefake__") for n in names)
    assert beta.mcp_manager is not None
    assert beta.mcp_manager.data_dir == tmp_path
    assert beta.mcp_manager.call("rootfake__marker", {}) == "root-marker"

    # 既定テナントも従来どおりルートの設定で、マネージャは
    # ルートを使う組み合わせ同士で共有される
    default_model = _RecordingModel()
    default = Session(Config(data_dir=tmp_path), default_model)
    names = _tool_names(default, default_model)
    assert any(n.startswith("rootfake__") for n in names)
    assert default.mcp_manager is beta.mcp_manager


def test_two_tenants_do_not_share_servers(tmp_path: Path):
    """別々のテナント別設定を持つ 2 テナントで、道具もプロセスも混ざらない。"""
    _write_mcp_json(tmp_path, "rootfake", "root-marker")
    _write_mcp_json(tenant_dir(tmp_path, "acme"), "acmefake", "acme-marker")
    _write_mcp_json(tenant_dir(tmp_path, "globex"),
                    "globexfake", "globex-marker")
    acme_model = _RecordingModel()
    acme = Session(Config(data_dir=tmp_path, tenant="acme"), acme_model)
    globex_model = _RecordingModel()
    globex = Session(Config(data_dir=tmp_path, tenant="globex"),
                     globex_model)

    acme_names = _tool_names(acme, acme_model)
    globex_names = _tool_names(globex, globex_model)
    assert acme_names and all(
        n.startswith("acmefake__") or "__" not in n for n in acme_names)
    assert any(n.startswith("acmefake__") for n in acme_names)
    assert not any(n.startswith(("globexfake__", "rootfake__"))
                   for n in acme_names)
    assert any(n.startswith("globexfake__") for n in globex_names)
    assert not any(n.startswith(("acmefake__", "rootfake__"))
                   for n in globex_names)

    # マネージャ（＝サーバープロセス群）はテナントごとに別で、
    # それぞれの設定の env がそれぞれのプロセスに渡っている
    assert acme.mcp_manager is not None and globex.mcp_manager is not None
    assert acme.mcp_manager is not globex.mcp_manager
    assert acme.mcp_manager.call("acmefake__marker", {}) == "acme-marker"
    assert globex.mcp_manager.call(
        "globexfake__marker", {}) == "globex-marker"
