"""テナントごとの頭脳（第3段: CLI 系）のテスト。

運用者スイッチ（環境変数 KUMA_ALLOW_TENANT_BRAIN_CLI /
Config.allow_tenant_brain_cli）が有効なときだけ、テナントの
頭脳に CLI 系（claude-code / gemini-cli / codex）を指定できる
ことを確認する。

- スイッチ無効: 保存 API は CLI 系を 400 で断り、候補にも出ない。
  設定ファイルに直接書かれていても解決で無視され、そのテナント
  は全体の既定に従う（全体の既定が CLI 系の場合はその既定に
  従う従来の挙動も壊れない）
- スイッチ有効: CLI 系のテナントが動き（Session の実体が CLI
  クライアントになる）、注意表示（/api/meta の brain_cli）は
  そのテナントにだけ出る。起動時の注意にもスイッチが有効で
  ある旨が出る
- テナント固定（0.68.0）とは干渉しない

実クライアントは生成だけ行い、呼び出しはしない。
"""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kuma_agent.agent_cli import CliAgentClient
from kuma_agent.brain import (cli_brain_tenants, effective_brain,
                              resolve_brain, scoped_config,
                              tenant_brain_providers, tenant_brain_setting)
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.settings import set_brain_setting
from kuma_agent.web import build_server, tenant_cli_brain_note


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    # 実クライアントの「生成だけ」は行うため、httpx が環境の
    # proxy 変数を解釈して落ちないよう外しておく（呼び出しはしない）
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                 "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)


def _start(session: Session):
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"http://127.0.0.1:{httpd.server_address[1]}"


def _stop(httpd, thread) -> None:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def server_off(tmp_path: Path):
    """スイッチ無効（既定）のサーバー。"""
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    httpd, thread, base = _start(session)
    try:
        yield base, tmp_path, httpd
    finally:
        _stop(httpd, thread)


@pytest.fixture()
def server_on(tmp_path: Path):
    """スイッチ有効のサーバー。"""
    cfg = Config(data_dir=tmp_path, allow_tenant_brain_cli=True)
    session = Session(cfg, _EchoModel())
    httpd, thread, base = _start(session)
    try:
        yield base, tmp_path, httpd
    finally:
        _stop(httpd, thread)


# 環境の proxy 設定に引きずられないよう、proxy を使わない
# 専用の opener でローカルのテストサーバーへつなぐ
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str) -> tuple[int, dict]:
    try:
        with _OPENER.open(url, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _OPENER.open(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ------------------------------------------------------- スイッチ自体

def test_switch_comes_from_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("KUMA_ALLOW_TENANT_BRAIN_CLI", raising=False)
    assert Config(data_dir=tmp_path).allow_tenant_brain_cli is False
    monkeypatch.setenv("KUMA_ALLOW_TENANT_BRAIN_CLI", "1")
    assert Config(data_dir=tmp_path).allow_tenant_brain_cli is True
    monkeypatch.setenv("KUMA_ALLOW_TENANT_BRAIN_CLI", "0")
    assert Config(data_dir=tmp_path).allow_tenant_brain_cli is False


def test_tenant_brain_providers_range():
    assert tenant_brain_providers(False) == ("muse", "claude", "gemini")
    allowed = tenant_brain_providers(True)
    assert allowed[:3] == ("muse", "claude", "gemini")
    assert set(allowed[3:]) == {"claude-code", "gemini-cli", "codex",
                                "antigravity-cli"}


# ------------------------------------------------------- 解決（brain.py）

def test_cli_setting_gated_by_switch(tmp_path: Path):
    set_brain_setting(tmp_path, "acme", "codex", None)
    # スイッチ無効（既定）では CLI 系の指定は読み取りで無視される
    assert tenant_brain_setting(tmp_path, "acme") == {}
    # スイッチ有効なら指定が効く。未知の名前は有効でも落とす
    assert tenant_brain_setting(tmp_path, "acme", allow_cli=True) == {
        "provider": "codex"}
    set_brain_setting(tmp_path, "beta", "unknown-x", None)
    assert tenant_brain_setting(tmp_path, "beta", allow_cli=True) == {}


def test_effective_and_scoped_follow_switch(tmp_path: Path):
    set_brain_setting(tmp_path, "acme", "claude-code", "cli-model-x")
    off = Config(data_dir=tmp_path)
    # プロバイダの項目だけが落ち、モデル名の指定は全体の既定の
    # プロバイダへの部分指定として効く（第1段の項目ごとの検証）
    assert effective_brain(off, "acme") == ("muse", "cli-model-x", False)
    assert scoped_config(off, "acme").provider == "muse"
    on = Config(data_dir=tmp_path, allow_tenant_brain_cli=True)
    assert effective_brain(on, "acme") == ("claude-code", "cli-model-x", True)
    scoped = scoped_config(on, "acme")
    assert scoped.provider == "claude-code"
    assert scoped.is_cli_provider is True


def test_resolve_brain_creates_cli_client_only_when_allowed(tmp_path: Path):
    set_brain_setting(tmp_path, "acme", "claude-code", None)
    fallback = _EchoModel()
    off = Config(data_dir=tmp_path)
    _cfg, model = resolve_brain(off, "acme", fallback_model=fallback)
    # スイッチ無効では上書き扱いにならず、全体の既定の実体を使い回す
    assert model is fallback
    on = Config(data_dir=tmp_path, allow_tenant_brain_cli=True)
    cfg, model = resolve_brain(on, "acme", fallback_model=fallback)
    assert isinstance(model, CliAgentClient)
    assert cfg.provider == "claude-code"


def test_antigravity_follows_switch_without_brain_change(tmp_path: Path):
    # 0.91.0: antigravity-cli も CLI_PROVIDERS への登録だけで、
    # brain.py 無変更のままスイッチの枠組みに入る
    from kuma_agent.agent_cli import AntigravityCliClient

    set_brain_setting(tmp_path, "acme", "antigravity-cli", None)
    off = Config(data_dir=tmp_path)
    assert tenant_brain_setting(tmp_path, "acme") == {}
    # 全体の既定（muse）に従い、既定モデル名が補完される
    assert effective_brain(off, "acme") == ("muse", "muse-wonder-1.0", False)
    on = Config(data_dir=tmp_path, allow_tenant_brain_cli=True)
    assert effective_brain(on, "acme") == ("antigravity-cli", "", True)
    scoped = scoped_config(on, "acme")
    assert scoped.provider == "antigravity-cli"
    assert scoped.is_cli_provider is True
    _cfg, model = resolve_brain(on, "acme", fallback_model=_EchoModel())
    assert isinstance(model, AntigravityCliClient)
    assert cli_brain_tenants(tmp_path) == ["acme"]


def test_global_cli_default_not_blocked_by_switch(tmp_path: Path):
    # 全体の既定が CLI 系であること自体は、スイッチ無効でも
    # 従来どおり。指定の無いテナントはその既定に従う
    base = Config(data_dir=tmp_path, provider="claude-code")
    assert effective_brain(base, "acme") == ("claude-code", "", True)
    # テナントに別の CLI 系を書いても、スイッチ無効では無視され、
    # 「書いた CLI」ではなく全体の既定の CLI に従う
    set_brain_setting(tmp_path, "acme", "gemini-cli", None)
    assert effective_brain(base, "acme") == ("claude-code", "", True)
    # API 系への切り替えはスイッチと無関係に効く（第1段の挙動）
    set_brain_setting(tmp_path, "beta", "gemini", None)
    assert effective_brain(base, "beta") == (
        "gemini", "gemini-3.8-flash", False)


def test_cli_brain_tenants_lists_regardless_of_switch(tmp_path: Path):
    set_brain_setting(tmp_path, "beta", "gemini", None)
    set_brain_setting(tmp_path, "acme", "codex", None)
    set_brain_setting(tmp_path, "gamma", "claude-code", None)
    assert cli_brain_tenants(tmp_path) == ["acme", "gamma"]


# ------------------------------------------------------- 起動時の注意

def test_tenant_cli_brain_note(tmp_path: Path):
    off = Config(data_dir=tmp_path)
    assert tenant_cli_brain_note(off) is None
    on = Config(data_dir=tmp_path, allow_tenant_brain_cli=True)
    note = tenant_cli_brain_note(on)
    assert note is not None
    assert "KUMA_ALLOW_TENANT_BRAIN_CLI" in note
    # API 系だけの指定では、テナント名は挙がらない
    set_brain_setting(tmp_path, "beta", "gemini", None)
    note = tenant_cli_brain_note(on)
    assert note is not None and "beta" not in note
    # CLI 系を指定しているテナントは名前が挙がる
    set_brain_setting(tmp_path, "acme", "codex", None)
    note = tenant_cli_brain_note(on)
    assert note is not None and "acme" in note


# ------------------------------------------------------- 保存 API

def test_save_cli_brain_rejected_when_switch_off(server_off):
    base, _tmp_path, _httpd = server_off
    status, data = _get(base + "/api/tenant_settings?tenant=acme")
    assert status == 200
    # 候補に CLI 系は出ない
    assert data["brain_providers"] == ["muse", "claude", "gemini"]
    for provider in ("claude-code", "gemini-cli", "codex",
                     "antigravity-cli"):
        status, _data = _post(base + "/api/tenant_settings",
                              {"tenant": "acme",
                               "brain": {"provider": provider}})
        assert status == 400, provider
    # 断られた指定は保存もされていない
    status, data = _get(base + "/api/tenant_settings?tenant=acme")
    assert data["brain_provider"] is None
    assert data["brain_effective_provider"] == "muse"


def test_save_cli_brain_allowed_when_switch_on(server_on):
    base, _tmp_path, _httpd = server_on
    status, data = _get(base + "/api/tenant_settings?tenant=acme")
    assert status == 200
    assert "codex" in data["brain_providers"]
    assert "claude-code" in data["brain_providers"]
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "brain": {"provider": "codex"}})
    assert status == 200
    assert data["brain_provider"] == "codex"
    assert data["brain_effective_provider"] == "codex"
    assert data["brain_cli"] is True
    # 未知の名前はスイッチ有効でも 400
    status, _data = _post(base + "/api/tenant_settings",
                          {"tenant": "acme",
                           "brain": {"provider": "unknown-x"}})
    assert status == 400
    # CLI 系が実効のテナントには API キーを設定できない（第2段）
    status, _data = _post(base + "/api/tenant_settings",
                          {"tenant": "acme", "brain_api_key": "sk-test"})
    assert status == 400


def test_cli_tenant_session_and_meta(server_on):
    base, _tmp_path, httpd = server_on
    status, _data = _post(base + "/api/tenant_settings",
                          {"tenant": "acme",
                           "brain": {"provider": "claude-code"}})
    assert status == 200
    # 注意表示は CLI 系のテナントにだけ出る
    status, data = _get(base + "/api/meta?tenant=acme")
    assert data["brain_provider"] == "claude-code"
    assert data["brain_cli"] is True
    status, data = _get(base + "/api/meta")
    assert data["brain_provider"] == "muse"
    assert data["brain_cli"] is False
    # そのテナントの Session は CLI 系の実体で作られる
    session = httpd.tenants.store("acme", None).get("main")
    assert isinstance(session.model, CliAgentClient)
    assert session.cfg.provider == "claude-code"
    # 他のテナントは全体の既定（起動時の実体）のまま
    other = httpd.tenants.store("beta", None).get("main")
    assert isinstance(other.model, _EchoModel)
    assert other.cfg.provider == "muse"


def test_cli_setting_in_file_ignored_when_switch_off(server_off):
    base, tmp_path, httpd = server_off
    # 設定ファイルへ直接書かれた CLI 系の指定は、スイッチ無効
    # では実効にならない（全体の既定のまま）
    set_brain_setting(tmp_path, "acme", "gemini-cli", None)
    status, data = _get(base + "/api/meta?tenant=acme")
    assert data["brain_provider"] == "muse"
    assert data["brain_cli"] is False
    session = httpd.tenants.store("acme", None).get("main")
    assert isinstance(session.model, _EchoModel)
    assert session.cfg.provider == "muse"


def test_cli_brain_does_not_interfere_with_tenant_pin(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, web_tenant="acme",
                 allow_tenant_brain_cli=True)
    session = Session(cfg, _EchoModel())
    httpd, thread, base = _start(session)
    try:
        # 固定先以外は、CLI 系の頭脳指定も含めて従来どおり 403
        status, _ = _post(base + "/api/tenant_settings",
                          {"tenant": "other",
                           "brain": {"provider": "codex"}})
        assert status == 403
        # 固定先はスイッチ有効なら CLI 系も指定できる
        status, data = _post(base + "/api/tenant_settings",
                             {"tenant": "acme",
                              "brain": {"provider": "codex"}})
        assert status == 200
        assert data["brain_cli"] is True
    finally:
        _stop(httpd, thread)
