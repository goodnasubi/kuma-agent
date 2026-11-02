"""テナント設定・管理 API と管理パネルのテスト。

GET/POST /api/tenant_settings（能動発信の ON/OFF と保持期間）、
POST /api/tenants/create（正常・不正・重複）、ページにテナント管理
パネルが含まれることを確認する。
"""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import load_retention_overrides
from kuma_agent.session import Session
from kuma_agent.settings import proactive_enabled_for
from kuma_agent.web import build_server


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


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


def _post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ---------------------------------------------------------------- 設定の取得

def test_tenant_settings_defaults(server):
    base, _tmp_path = server
    status, data = _get(base + "/api/tenant_settings")
    assert status == 200
    assert data == {"tenant": "default", "proactive_enabled": True,
                    "approvals_enabled": True,
                    "retention_days": None, "retention_effective_days": 90}

    status, data = _get(base + "/api/tenant_settings?tenant=acme")
    assert status == 200
    assert data["tenant"] == "acme"
    assert data["proactive_enabled"] is True
    assert data["retention_days"] is None


# ---------------------------------------------------------------- 設定の保存

def test_tenant_settings_save_proactive(server):
    base, tmp_path = server
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "proactive_enabled": False})
    assert status == 200
    assert data["proactive_enabled"] is False
    # settings.json に書かれ、読み直しても OFF のまま
    assert proactive_enabled_for(tmp_path, "acme") is False
    _status, data = _get(base + "/api/tenant_settings?tenant=acme")
    assert data["proactive_enabled"] is False
    # 他のテナントには影響しない
    _status, data = _get(base + "/api/tenant_settings?tenant=beta")
    assert data["proactive_enabled"] is True

    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "proactive_enabled": True})
    assert status == 200
    assert data["proactive_enabled"] is True


def test_tenant_settings_save_retention(server):
    base, tmp_path = server
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "retention_days": 30})
    assert status == 200
    assert data["retention_days"] == 30
    assert data["retention_effective_days"] == 30
    # retention.json の既存の仕組みに書かれる
    assert load_retention_overrides(tmp_path) == {"acme": 30}

    # null で未設定に戻す（実効値は全体の既定 90）
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "retention_days": None})
    assert status == 200
    assert data["retention_days"] is None
    assert data["retention_effective_days"] == 90
    assert load_retention_overrides(tmp_path) == {}


def test_tenant_settings_partial_update_keeps_other_field(server):
    base, _tmp_path = server
    _post(base + "/api/tenant_settings",
          {"tenant": "acme", "proactive_enabled": False, "retention_days": 30})
    # 保持期間だけ変えても能動発信の設定は残る
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "retention_days": 10})
    assert status == 200
    assert data["proactive_enabled"] is False
    assert data["retention_days"] == 10


def test_tenant_settings_rejects_bad_values(server):
    base, _tmp_path = server
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "proactive_enabled": "yes"})
    assert status == 400 and data["error"]
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "retention_days": -1})
    assert status == 400 and data["error"]
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "retention_days": "30"})
    assert status == 400 and data["error"]
    # 不正な保存では何も書かれていない
    _status, data = _get(base + "/api/tenant_settings?tenant=acme")
    assert data["proactive_enabled"] is True
    assert data["retention_days"] is None


# ---------------------------------------------------------------- テナント作成

def test_create_tenant(server):
    base, tmp_path = server
    status, data = _post(base + "/api/tenants/create", {"name": "acme"})
    assert status == 200
    assert data == {"tenant": "acme", "created": True}
    assert (tmp_path / "tenants" / "acme").is_dir()
    _status, data = _get(base + "/api/tenants")
    assert "acme" in data["tenants"]


def test_create_tenant_normalizes_name(server):
    base, tmp_path = server
    status, data = _post(base + "/api/tenants/create", {"name": "acme corp"})
    assert status == 200
    assert data["tenant"] == "acme-corp"
    assert (tmp_path / "tenants" / "acme-corp").is_dir()


def test_create_tenant_rejects_duplicates_and_default(server):
    base, _tmp_path = server
    assert _post(base + "/api/tenants/create", {"name": "acme"})[0] == 200
    status, data = _post(base + "/api/tenants/create", {"name": "acme"})
    assert status == 400 and "すでに存在" in data["error"]
    # 既定テナントは作成済みとして扱う
    status, data = _post(base + "/api/tenants/create", {"name": "default"})
    assert status == 400 and data["error"]


def test_create_tenant_rejects_bad_names(server):
    base, _tmp_path = server
    for payload in ({"name": ""}, {"name": "   "}, {}, {"name": 42}):
        status, data = _post(base + "/api/tenants/create", payload)
        assert status == 400 and data["error"]


# ---------------------------------------------------------------- 画面

def test_page_has_tenant_admin_panel(server):
    base, _tmp_path = server
    with urllib.request.urlopen(base + "/", timeout=10) as resp:
        page = resp.read().decode("utf-8")
    assert "tenantAdminBtn" in page and "テナント管理" in page
    assert "tenantPanel" in page
    assert "/api/tenant_settings" in page and "/api/tenants/create" in page
    assert "能動発信" in page and "保持期限" in page
