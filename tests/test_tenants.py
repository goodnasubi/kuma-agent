"""テナント方式（一番外側の区切り）のテスト。

テナントごとに人格・長期記憶・部屋・台帳の置き場が分かれ、互いに
見えないこと、既定テナントが従来の data_dir をそのまま使うことを
確認する。
"""
import json
import threading
import urllib.request
from pathlib import Path

import pytest

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import (
    list_tenant_names,
    normalize_tenant_name,
    tenant_dir,
)
from kuma_agent.session import Session
from kuma_agent.tasks import TaskBoard
from kuma_agent.web import build_server


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


# ---------------------------------------------------------------- 名前と置き場

def test_normalize_tenant_name():
    assert normalize_tenant_name("default") == "default"
    assert normalize_tenant_name(" 仕事 用 ") == "仕事-用"
    assert normalize_tenant_name("../etc/passwd").endswith("etc-passwd")
    assert normalize_tenant_name("") == "default"
    assert normalize_tenant_name(None) == "default"
    assert normalize_tenant_name("///") == "default"


def test_tenant_dir_default_is_data_dir(tmp_path: Path):
    assert tenant_dir(tmp_path, "default") == tmp_path
    assert tenant_dir(tmp_path, None) == tmp_path
    assert tenant_dir(tmp_path, "work") == tmp_path / "tenants" / "work"


def test_config_tenant_data_dir(tmp_path: Path):
    assert Config(data_dir=tmp_path).tenant_data_dir == tmp_path
    cfg = Config(data_dir=tmp_path, tenant="work")
    assert cfg.tenant_data_dir == tmp_path / "tenants" / "work"
    # 既存のパスプロパティも実効置き場を指す
    assert cfg.memory_file == tmp_path / "tenants" / "work" / "MEMORY.md"
    assert cfg.conversations_dir == tmp_path / "tenants" / "work" / "conversations"


def test_config_tenant_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KUMA_TENANT", "env-tenant")
    cfg = Config(data_dir=tmp_path)
    assert cfg.tenant == "env-tenant"
    assert cfg.tenant_data_dir == tmp_path / "tenants" / "env-tenant"


def test_list_tenant_names(tmp_path: Path):
    assert list_tenant_names(tmp_path) == ["default"]
    (tmp_path / "tenants" / "beta").mkdir(parents=True)
    (tmp_path / "tenants" / "alpha").mkdir(parents=True)
    (tmp_path / "tenants" / "file.txt").write_text("x", encoding="utf-8")
    assert list_tenant_names(tmp_path) == ["default", "alpha", "beta"]


# ---------------------------------------------------------------- 既定テナントの互換性

def test_default_tenant_uses_legacy_layout(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    assert session.tenant == "default"
    session.say("従来どおり")
    # 記録は data_dir 直下（tenants/ を挟まない）
    assert (tmp_path / "conversations" / "main.jsonl").exists()
    assert not (tmp_path / "tenants").exists()


# ---------------------------------------------------------------- 隔離

def test_tenants_are_isolated(tmp_path: Path):
    cfg_a = Config(data_dir=tmp_path, tenant="alpha")
    cfg_b = Config(data_dir=tmp_path, tenant="beta")

    dir_a = tmp_path / "tenants" / "alpha"
    dir_b = tmp_path / "tenants" / "beta"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    # 人格ファイルをテナントごとに用意する
    (dir_a / "SOUL.md").write_text("# 魂\nA専用の人格", encoding="utf-8")
    (dir_b / "SOUL.md").write_text("# 魂\nB専用の人格", encoding="utf-8")

    a = Session(cfg_a, _EchoModel(), "room")
    b = Session(cfg_b, _EchoModel(), "room")
    assert "A専用の人格" in a.system_prompt
    assert "B専用の人格" not in a.system_prompt
    assert "B専用の人格" in b.system_prompt

    # 長期記憶: A に覚えさせても B からは見えない
    a.memory.remember("Aだけの事実")
    a2 = Session(cfg_a, _EchoModel(), "room")
    b2 = Session(cfg_b, _EchoModel(), "room")
    assert "Aだけの事実" in a2.system_prompt
    assert "Aだけの事実" not in b2.system_prompt
    assert (dir_a / "MEMORY.md").exists()
    assert not (dir_b / "MEMORY.md").exists()
    assert not (tmp_path / "MEMORY.md").exists()  # 既定テナント側にも漏れない

    # 部屋の記録: テナントが違えば混ざらない（既定の寄り添いモードでは
    # 部屋は main に固定される）
    a.say("Aの相談")
    b_sessions = Session(cfg_b, _EchoModel(), "room")
    assert all("Aの相談" not in m.content for m in b_sessions.history)
    assert (dir_a / "conversations" / "main.jsonl").exists()
    assert not (dir_b / "conversations" / "main.jsonl").exists()
    assert not (tmp_path / "conversations").exists()

    # 台帳: A の用事は B の台帳・プロンプトに見えない
    a.task_board.add("Aの用事", "Aのメモ")
    assert TaskBoard(dir_b).open_tasks() == []
    b3 = Session(cfg_b, _EchoModel(), "room")
    assert "Aの用事" not in b3.system_prompt
    a3 = Session(cfg_a, _EchoModel(), "room")
    assert "Aの用事" in a3.system_prompt
    # 台帳はユーザーごとに 1 冊: A の台帳は tasks.json に置かれる
    assert (dir_a / "tasks.json").exists()
    assert not (dir_b / "tasks.json").exists()
    assert not (dir_b / "tasks").exists()


# ---------------------------------------------------------------- CLI

def test_cli_tenant_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured: dict = {}

    class _FakeSession:
        def __init__(self, cfg, model, session_id="main"):
            self.session_id = session_id
            captured["tenant"] = cfg.tenant
            captured["tenant_data_dir"] = cfg.tenant_data_dir

        def say(self, text):
            return ""

    def _eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(cli, "Session", _FakeSession)
    monkeypatch.setattr(cli, "create_client", lambda *args: object())
    monkeypatch.setenv("KUMA_LLM_PROVIDER", "claude-code")  # API キー不要の系統
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("builtins.input", _eof)

    assert cli.main(["chat", "--tenant", "仕事"]) == 0
    assert captured["tenant"] == "仕事"
    assert captured["tenant_data_dir"] == tmp_path / "tenants" / "仕事"


def test_cli_tenant_flag_overrides_env(monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Path):
    captured: dict = {}

    class _FakeSession:
        def __init__(self, cfg, model, session_id="main"):
            self.session_id = session_id
            captured["tenant"] = cfg.tenant

        def say(self, text):
            return ""

    def _eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(cli, "Session", _FakeSession)
    monkeypatch.setattr(cli, "create_client", lambda *args: object())
    monkeypatch.setenv("KUMA_LLM_PROVIDER", "claude-code")
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KUMA_TENANT", "env-tenant")
    monkeypatch.setattr("builtins.input", _eof)

    assert cli.main(["chat", "--tenant", "cli-tenant"]) == 0
    assert captured["tenant"] == "cli-tenant"


# ---------------------------------------------------------------- Web

@pytest.fixture()
def web_server(tmp_path: Path):
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


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_web_tenants_are_separate(web_server):
    base, tmp_path = web_server
    reply = _post_json(base + "/api/chat",
                       {"message": "Aの話", "tenant": "alpha"})
    assert reply == {"reply": "返答: Aの話"}
    _post_json(base + "/api/chat", {"message": "既定の話"})

    # テナントごとに履歴が分かれる
    hist_a = _get_json(base + "/api/history?tenant=alpha")["messages"]
    assert [m["content"] for m in hist_a] == ["Aの話", "返答: Aの話"]
    hist_default = _get_json(base + "/api/history")["messages"]
    assert all("Aの話" not in m["content"] for m in hist_default)
    assert any("既定の話" in m["content"] for m in hist_default)
    hist_beta = _get_json(base + "/api/history?tenant=beta")["messages"]
    assert hist_beta == []

    # 部屋一覧もテナントごと
    sessions_a = _get_json(base + "/api/sessions?tenant=alpha")["sessions"]
    assert sessions_a == ["main"]

    # テナント一覧（current は起動時テナント）
    data = _get_json(base + "/api/tenants")
    assert data["current"] == "default"
    assert "default" in data["tenants"] and "alpha" in data["tenants"]

    # 記録ファイルもテナントの置き場に分かれる
    assert (tmp_path / "tenants" / "alpha" / "conversations" / "main.jsonl").exists()
    assert (tmp_path / "conversations" / "main.jsonl").exists()


def test_web_page_has_tenant_switcher(web_server):
    base, _tmp_path = web_server
    with urllib.request.urlopen(base + "/", timeout=10) as resp:
        page = resp.read().decode("utf-8")
    assert "tenantSelect" in page and "createTenant" in page
    assert "/api/tenants" in page
