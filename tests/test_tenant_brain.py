"""テナントごとの頭脳（第1段: プロバイダ／モデル名）のテスト。

settings.json の brain 項目の読み書き、brain.py の解決（scoped
Config と実体の組・キーの書き戻し・検証）、TenantStore のテナント
別モデル、保存時の即時反映、/api/meta と設定 API の頭脳項目、
CLI 注意の出し分け、テナント固定（0.68.0）と干渉しないことを
確認する。実クライアントは生成だけ行い、呼び出しはしない。
"""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kuma_agent import brain as brain_mod
from kuma_agent import llm
from kuma_agent.brain import (effective_brain, resolve_brain,
                              scoped_config, tenant_brain_setting)
from kuma_agent.config import Config
from kuma_agent.delegate import _child_model
from kuma_agent.llm import AnthropicClient, GeminiClient, Message
from kuma_agent.session import Session
from kuma_agent.settings import (brain_setting_for, remove_tenant_settings,
                                 set_brain_setting, set_proactive_enabled)
from kuma_agent.web import build_server


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


@pytest.fixture()
def server(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# 環境の proxy 設定に引きずられないよう、proxy を使わない
# 専用の opener でローカルのテストサーバーへつなぐ（urllib の
# グローバル opener は proxy 設定を保持するため、環境変数を
# 消す fixture と組み合わさると 127.0.0.1 まで proxy 経由に
# なってしまう）
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


def _write_settings(data_dir: Path, content: object) -> None:
    (data_dir / "settings.json").write_text(
        json.dumps(content, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------- settings 層

def test_brain_setting_roundtrip(tmp_path: Path):
    assert brain_setting_for(tmp_path, "acme") == {}
    set_brain_setting(tmp_path, "acme", "claude", "claude-x")
    assert brain_setting_for(tmp_path, "acme") == {
        "provider": "claude", "model": "claude-x"}
    # 片方だけ取り除くと、もう片方は残る
    set_brain_setting(tmp_path, "acme", None, "claude-y")
    assert brain_setting_for(tmp_path, "acme") == {"model": "claude-y"}
    # 両方取り除くと brain の指定ごと消える
    set_brain_setting(tmp_path, "acme", None, None)
    assert brain_setting_for(tmp_path, "acme") == {}
    raw = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert "brain" not in raw["tenants"]["acme"]


def test_brain_setting_preserves_others(tmp_path: Path):
    set_proactive_enabled(tmp_path, "acme", False)
    set_brain_setting(tmp_path, "beta", "gemini", None)
    set_brain_setting(tmp_path, "acme", "claude", None)
    raw = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    # 同じテナントの他の項目・他のテナントの指定は壊れない
    assert raw["tenants"]["acme"]["proactive_enabled"] is False
    assert raw["tenants"]["acme"]["brain"] == {"provider": "claude"}
    assert raw["tenants"]["beta"]["brain"] == {"provider": "gemini"}
    # テナント削除で頭脳の指定も一緒に消える
    remove_tenant_settings(tmp_path, "acme")
    raw = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert "acme" not in raw["tenants"]
    assert raw["tenants"]["beta"]["brain"] == {"provider": "gemini"}


def test_brain_setting_for_ignores_non_strings(tmp_path: Path):
    _write_settings(tmp_path, {"tenants": {"acme": {
        "brain": {"provider": 123, "model": "m1"}}}})
    assert brain_setting_for(tmp_path, "acme") == {"model": "m1"}
    _write_settings(tmp_path, "壊れた json")
    assert brain_setting_for(tmp_path, "acme") == {}


# ------------------------------------------------------- 解決（brain.py）

def test_tenant_brain_setting_validation(tmp_path: Path):
    # 第1段では CLI 系と未知のプロバイダはその項目だけ落とす
    set_brain_setting(tmp_path, "acme", "codex", "m1")
    assert tenant_brain_setting(tmp_path, "acme") == {"model": "m1"}
    set_brain_setting(tmp_path, "acme", "unknown-x", None)
    assert tenant_brain_setting(tmp_path, "acme") == {}
    set_brain_setting(tmp_path, "acme", "claude", "")
    assert tenant_brain_setting(tmp_path, "acme") == {"provider": "claude"}


def test_scoped_config_key_writeback(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    set_brain_setting(tmp_path, "acme", "gemini", None)
    base = Config(data_dir=tmp_path, provider="claude",
                  model_api_key="claude-key")
    scoped = scoped_config(base, "acme")
    assert scoped.provider == "gemini"
    assert scoped.tenant == "acme"
    # プロバイダが変わる場合は、全体用に解決済みのキーを流用せず
    # そのプロバイダのキーへ引き直す
    assert scoped.model_api_key == "gem-key"
    # プロバイダが同じ（モデルだけの指定）なら、解決済みのキーを保つ
    set_brain_setting(tmp_path, "beta", None, "claude-y")
    scoped_beta = scoped_config(base, "beta")
    assert scoped_beta.provider == "claude"
    assert scoped_beta.model == "claude-y"
    assert scoped_beta.model_api_key == "claude-key"


def test_effective_brain(tmp_path: Path):
    base = Config(data_dir=tmp_path)
    assert effective_brain(base, "acme") == ("muse", "muse-wonder-1.0", False)
    set_brain_setting(tmp_path, "acme", None, "custom-model")
    assert effective_brain(base, "acme") == ("muse", "custom-model", False)
    # 全体の既定が CLI 系なら、指定の無いテナントは CLI 系として返る
    cli_base = Config(data_dir=tmp_path, provider="claude-code")
    assert effective_brain(cli_base, "beta") == ("claude-code", "", True)
    # API 系に切り替えたテナントは CLI 系ではなくなる
    set_brain_setting(tmp_path, "beta", "gemini", None)
    assert effective_brain(cli_base, "beta") == (
        "gemini", "gemini-3.8-flash", False)


def test_resolve_brain_fallback_and_create(tmp_path: Path):
    base = Config(data_dir=tmp_path)
    fallback = _EchoModel()
    cfg, model = resolve_brain(base, "acme", fallback_model=fallback)
    # 指定が無ければ fallback をそのまま使い、Config はテナントだけ差し替わる
    assert model is fallback
    assert cfg.tenant == "acme"
    set_brain_setting(tmp_path, "acme", "gemini", "gemini-x")
    cfg, model = resolve_brain(base, "acme", fallback_model=fallback)
    assert isinstance(model, GeminiClient)
    assert model.model == "gemini-x"
    assert cfg.provider == "gemini"


def test_providers_include_codex():
    assert "codex" in llm.PROVIDERS
    assert set(llm.PROVIDERS) == set(brain_mod.ALL_PROVIDERS)


# ------------------------------------------------------- TenantStore と画面 API

def test_tenant_session_uses_tenant_brain(server):
    _base, tmp_path, httpd = server
    set_brain_setting(tmp_path, "acme", "gemini", None)
    tenants = httpd.tenants
    acme = tenants.store("acme", None).get("main")
    assert acme.cfg.provider == "gemini"
    assert isinstance(acme.model, GeminiClient)
    # 他のテナントは全体の既定（渡された実体）のまま
    other = tenants.store("beta", None).get("main")
    assert other.cfg.provider == "muse"
    assert isinstance(other.model, _EchoModel)


def test_startup_tenant_brain_applied(tmp_path: Path):
    set_brain_setting(tmp_path, "default", "claude", None)
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    try:
        initial = httpd.tenants.store("default", None).get("main")
        assert initial.cfg.provider == "claude"
        assert isinstance(initial.model, AnthropicClient)
    finally:
        httpd.server_close()


def test_brain_save_reflects_immediately(server):
    base, tmp_path, httpd = server
    store = httpd.tenants.store("acme", None)
    before = store.get("main")
    assert before.cfg.provider == "muse"
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme",
                          "brain": {"provider": "gemini", "model": "g-x"}})
    assert status == 200
    assert data["brain_provider"] == "gemini"
    assert data["brain_model"] == "g-x"
    assert data["brain_effective_provider"] == "gemini"
    assert data["brain_effective_model"] == "g-x"
    assert data["brain_cli"] is False
    # 保存前の Session は破棄され、新しい頭脳で作り直される
    after = store.get("main")
    assert after is not before
    assert after.cfg.provider == "gemini"
    assert isinstance(after.model, GeminiClient)
    # 全体の既定に戻すと、再び既定の頭脳で作り直される
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "brain": None})
    assert status == 200
    assert data["brain_provider"] is None
    restored = store.get("main")
    assert restored.cfg.provider == "muse"
    assert isinstance(restored.model, _EchoModel)


def test_brain_save_validation(server):
    base, _tmp_path, _httpd = server
    for payload in ({"provider": "codex"}, {"provider": "unknown-x"},
                    {"model": "x" * 201}, {"provider": 123}):
        status, _data = _post(base + "/api/tenant_settings",
                              {"tenant": "acme", "brain": payload})
        assert status == 400, payload
    status, _data = _post(base + "/api/tenant_settings",
                          {"tenant": "acme", "brain": "gemini"})
    assert status == 400


def test_meta_brain_fields(server):
    base, tmp_path, _httpd = server
    status, data = _get(base + "/api/meta")
    assert status == 200
    assert data["brain_provider"] == "muse"
    assert data["brain_model"] == "muse-wonder-1.0"
    assert data["brain_cli"] is False
    set_brain_setting(tmp_path, "acme", "gemini", None)
    status, data = _get(base + "/api/meta?tenant=acme")
    assert data["tenant"] == "acme"
    assert data["brain_provider"] == "gemini"
    assert data["brain_cli"] is False


def test_meta_cli_note_follows_effective_brain(tmp_path: Path):
    # 全体の既定が CLI 系のサーバーでは、指定の無いテナントは
    # CLI 系（注意あり）、API 系に切り替えたテナントは CLI 系
    # ではない（注意なし）と返る
    cfg = Config(data_dir=tmp_path, provider="claude-code")
    session = Session(cfg, _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, data = _get(base + "/api/meta")
        assert data["brain_provider"] == "claude-code"
        assert data["brain_cli"] is True
        set_brain_setting(tmp_path, "acme", "gemini", None)
        status, data = _get(base + "/api/meta?tenant=acme")
        assert data["brain_provider"] == "gemini"
        assert data["brain_cli"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_brain_does_not_interfere_with_tenant_pin(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, web_tenant="acme")
    session = Session(cfg, _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        # 固定先以外は、頭脳まわりも含めて従来どおり 403
        status, _ = _get(base + "/api/meta?tenant=other")
        assert status == 403
        status, _ = _post(base + "/api/tenant_settings",
                          {"tenant": "other",
                           "brain": {"provider": "gemini"}})
        assert status == 403
        # 固定先の頭脳は通常どおり読み書きできる
        status, data = _post(base + "/api/tenant_settings",
                             {"tenant": "acme",
                              "brain": {"provider": "gemini"}})
        assert status == 200
        assert data["brain_provider"] == "gemini"
        status, data = _get(base + "/api/meta?tenant=acme")
        assert data["brain_provider"] == "gemini"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ------------------------------------------------------- 裏側処理の追随

def test_delegate_child_follows_parent_brain(tmp_path: Path):
    # 委任の子は親の scoped Config から作られるため、テナントの
    # 頭脳（プロバイダ）に追随する
    set_brain_setting(tmp_path, "acme", "gemini", None)
    base = Config(data_dir=tmp_path)
    cfg = scoped_config(base, "acme")
    parent = Session(cfg, _EchoModel())
    # モデル指定が無ければ親の実体をそのまま使う
    assert _child_model(parent) is parent.model
    # モデル指定がある場合は、親（テナント）のプロバイダで作り直す
    cfg2 = scoped_config(base, "acme")
    cfg2.delegate_model = "gemini-x"
    parent2 = Session(cfg2, _EchoModel())
    child_model = _child_model(parent2)
    assert isinstance(child_model, GeminiClient)
    assert child_model.model == "gemini-x"


def test_page_header_shows_effective_provider(tmp_path: Path):
    # 起動時テナントに指定があれば、ページの初期表示もその頭脳
    set_brain_setting(tmp_path, "default", "claude", None)
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with _OPENER.open(
                f"http://127.0.0.1:{httpd.server_address[1]}/",
                timeout=10) as resp:
            page = resp.read().decode("utf-8")
        assert 'id="providerName">provider: claude<' in page
        # テナント行に頭脳の欄がある
        assert "頭脳を既定に戻す" in page
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
