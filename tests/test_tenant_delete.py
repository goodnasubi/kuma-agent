"""テナント削除 API（POST /api/tenants/delete）と管理パネルのテスト。

削除で置き場が消えて一覧から外れること、削除前に控えの ZIP が
backups/ に作られテナントのファイルが入っていること、既定テナント
と起動時テナントが拒否されること、存在しないテナントは 404 に
なること、置き場の外の設定（settings.json / retention.json の
項目）が取り除かれること、控えが作れなければ削除しないこと、
ログイン有効時は未認証で拒否されることを確認する。

あわせて、削除前の確認に出す影響範囲の集計
（GET /api/tenants/impact）が、ユーザー・会話・用事の件数と
テナント設定を正しく返し、読み取りだけで何も変えないことも
確認する。
"""
import http.client
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import pytest

import kuma_agent.web as web_module
from kuma_agent.config import Config
from kuma_agent.export import EXPORT_INFO_NAME
from kuma_agent.llm import Message
from kuma_agent.memory import load_retention_overrides
from kuma_agent.proactive import run_sweep
from kuma_agent.session import Session
from kuma_agent.settings import load_tenant_settings
from kuma_agent.web import build_server

PASSWORD = "test-password"


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


@contextmanager
def _running(tmp_path: Path, **cfg_kwargs):
    """偽モデルで Web サーバーを起動し、base URL と data_dir を渡す。"""
    cfg = Config(data_dir=tmp_path, **cfg_kwargs)
    session = Session(cfg, _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def server(tmp_path: Path):
    with _running(tmp_path) as addr:
        yield addr


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


def _create(base: str, name: str) -> dict:
    status, data = _post(base + "/api/tenants/create", {"name": name})
    assert status == 200, data
    return data


def _seed_tenant_files(tmp_path: Path, name: str = "acme") -> Path:
    """テナントの置き場に、既定ユーザーと別ユーザーのファイルを置く。"""
    tdir = tmp_path / "tenants" / name
    (tdir / "conversations").mkdir(parents=True, exist_ok=True)
    (tdir / "MEMORY.md").write_text("acme の長期記憶", encoding="utf-8")
    (tdir / "conversations" / "main.jsonl").write_text(
        '{"role": "user", "content": "やあ"}\n', encoding="utf-8")
    bob = tdir / "users" / "bob"
    bob.mkdir(parents=True, exist_ok=True)
    (bob / "MEMORY.md").write_text("bob の長期記憶", encoding="utf-8")
    return tdir


# ---------------------------------------------------------------- 削除の基本

def test_delete_tenant_removes_dir_and_listing(server):
    base, tmp_path = server
    _create(base, "acme")
    tdir = _seed_tenant_files(tmp_path)

    status, data = _post(base + "/api/tenants/delete", {"tenant": "acme"})
    assert status == 200
    assert data["tenant"] == "acme"
    assert data["deleted"] is True
    assert data["backup"].startswith("tenant-acme-")
    assert data["backup"].endswith(".zip")

    assert not tdir.exists()
    _status, listing = _get(base + "/api/tenants")
    assert "acme" not in listing["tenants"]
    assert "default" in listing["tenants"]


def test_delete_tenant_after_use_disappears_from_listing(server):
    """一度使った（UserStore を読み込んだ）テナントも一覧から消える。"""
    base, tmp_path = server
    _create(base, "acme")
    _status, users = _get(base + "/api/users?tenant=acme")
    assert "default" in users["users"]  # acme の UserStore が読み込まれた
    _status, listing = _get(base + "/api/tenants")
    assert "acme" in listing["tenants"]

    status, _data = _post(base + "/api/tenants/delete", {"tenant": "acme"})
    assert status == 200
    _status, listing = _get(base + "/api/tenants")
    assert "acme" not in listing["tenants"]


def test_delete_normalizes_name(server):
    base, tmp_path = server
    _create(base, "acme corp")  # acme-corp として作られる
    assert (tmp_path / "tenants" / "acme-corp").is_dir()

    status, data = _post(base + "/api/tenants/delete",
                         {"tenant": "acme corp"})
    assert status == 200
    assert data["tenant"] == "acme-corp"
    assert not (tmp_path / "tenants" / "acme-corp").exists()


def test_delete_accepts_name_key(server):
    """create と同じ name キーでも指定できる。"""
    base, _tmp_path = server
    _create(base, "acme")
    status, data = _post(base + "/api/tenants/delete", {"name": "acme"})
    assert status == 200
    assert data["deleted"] is True


# ---------------------------------------------------------------- 控えの ZIP

def test_delete_writes_backup_zip_with_tenant_files(server):
    base, tmp_path = server
    _create(base, "acme")
    _seed_tenant_files(tmp_path)

    status, data = _post(base + "/api/tenants/delete", {"tenant": "acme"})
    assert status == 200

    backups = list((tmp_path / "backups").glob("tenant-acme-*.zip"))
    assert len(backups) == 1
    assert backups[0].name == data["backup"]
    with zipfile.ZipFile(io.BytesIO(backups[0].read_bytes())) as zf:
        names = zf.namelist()
        assert names[0] == EXPORT_INFO_NAME
        info = json.loads(zf.read(EXPORT_INFO_NAME).decode("utf-8"))
        assert info["tenant"] == "acme"
        assert info["format"] == "kuma-agent tenant backup"
        assert set(info["users"]) == {"default", "bob"}
        # 全ユーザーのファイルが相対パスのまま入っている
        assert "MEMORY.md" in names
        assert "conversations/main.jsonl" in names
        assert "users/bob/MEMORY.md" in names
        assert zf.read("MEMORY.md").decode("utf-8") == "acme の長期記憶"
        assert zf.read("users/bob/MEMORY.md").decode("utf-8") \
            == "bob の長期記憶"


def test_delete_aborts_when_backup_fails(server, monkeypatch: pytest.MonkeyPatch):
    """控えが作れなければ、置き場は消さずに 500 を返す。"""
    base, tmp_path = server
    _create(base, "acme")
    tdir = _seed_tenant_files(tmp_path)

    def _boom(data_dir, tenant, now=None):
        raise OSError("disk full")

    monkeypatch.setattr(web_module, "save_tenant_backup", _boom)
    status, data = _post(base + "/api/tenants/delete", {"tenant": "acme"})
    assert status == 500 and data["error"]
    assert tdir.is_dir()
    _status, listing = _get(base + "/api/tenants")
    assert "acme" in listing["tenants"]


# ---------------------------------------------------------------- 拒否されるケース

def test_delete_default_tenant_rejected(server):
    base, tmp_path = server
    status, data = _post(base + "/api/tenants/delete", {"tenant": "default"})
    assert status == 400 and data["error"]
    # データ置き場そのものには手が付いていない（控えも作られない）
    assert tmp_path.is_dir()
    assert not (tmp_path / "backups").exists()


def test_delete_missing_tenant_is_404(server):
    base, _tmp_path = server
    status, data = _post(base + "/api/tenants/delete", {"tenant": "ghost"})
    assert status == 404 and data["error"]


def test_delete_rejects_bad_body(server):
    base, _tmp_path = server
    for payload in ({}, {"tenant": ""}, {"tenant": "   "}, {"tenant": 42}):
        status, data = _post(base + "/api/tenants/delete", payload)
        assert status == 400 and data["error"]


def test_delete_startup_tenant_rejected(tmp_path: Path):
    """起動時テナント（default 以外）も土台のため削除できない。"""
    with _running(tmp_path, tenant="acme") as (base, data_dir):
        (data_dir / "tenants" / "acme").mkdir(parents=True, exist_ok=True)
        status, data = _post(base + "/api/tenants/delete", {"tenant": "acme"})
        assert status == 400 and data["error"]
        assert (data_dir / "tenants" / "acme").is_dir()


# ---------------------------------------------------------------- ルート側の設定

def test_delete_removes_root_settings(server):
    base, tmp_path = server
    _create(base, "acme")
    _create(base, "beta")
    for name in ("acme", "beta"):
        _post(base + "/api/tenant_settings",
              {"tenant": name, "proactive_enabled": False,
               "retention_days": 30})
    assert set(load_tenant_settings(tmp_path)) == {"acme", "beta"}
    assert set(load_retention_overrides(tmp_path)) == {"acme", "beta"}

    status, _data = _post(base + "/api/tenants/delete", {"tenant": "acme"})
    assert status == 200
    # acme の項目だけが取り除かれ、beta の指定は残る
    assert set(load_tenant_settings(tmp_path)) == {"beta"}
    assert set(load_retention_overrides(tmp_path)) == {"beta"}


# ---------------------------------------------------------------- 巡回への影響

def test_sweep_skips_deleted_tenant(server):
    base, tmp_path = server
    _create(base, "acme")
    _seed_tenant_files(tmp_path)
    status, _data = _post(base + "/api/tenants/delete", {"tenant": "acme"})
    assert status == 200

    # 巡回はテナントを置き場から列挙するので、消えたテナントで壊れない
    cfg = Config(data_dir=tmp_path)
    posted = run_sweep(cfg, now=datetime(2026, 11, 3, 9, 0, 0))
    assert all(tenant != "acme" for tenant, _user, _text in posted)


# ---------------------------------------------------------------- 画面

def test_page_has_tenant_delete_ui(server):
    base, _tmp_path = server
    with urllib.request.urlopen(base + "/", timeout=10) as resp:
        page = resp.read().decode("utf-8")
    assert "/api/tenants/delete" in page
    assert "削除" in page


# ---------------------------------------------------------------- ログイン

def _request(addr, method: str, path: str, body=None,
             headers: dict | None = None) -> tuple[int, dict, str]:
    host, port = addr
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, dict(resp.getheaders()), resp.read().decode("utf-8")
    finally:
        conn.close()


def _login_cookie(addr) -> str:
    status, headers, _body = _request(
        addr, "POST", "/login",
        body=urlencode({"password": PASSWORD}),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 302
    return headers["Set-Cookie"].split(";")[0]


def test_delete_requires_login_when_auth_enabled(tmp_path: Path):
    with _running(tmp_path, web_password=PASSWORD) as (base, data_dir):
        addr = ("127.0.0.1", int(base.rsplit(":", 1)[1]))
        # ログインしてテナントを作る
        cookie = _login_cookie(addr)
        tdir = data_dir / "tenants" / "acme"
        tdir.mkdir(parents=True)
        (tdir / "MEMORY.md").write_text("acme の長期記憶", encoding="utf-8")

        # 未認証の削除は 401 で、テナントは残る
        status, _headers, body = _request(
            addr, "POST", "/api/tenants/delete",
            body=json.dumps({"tenant": "acme"}),
            headers={"Content-Type": "application/json"})
        assert status == 401
        assert json.loads(body)["error"]
        assert tdir.is_dir()

        # ログイン済みなら削除できる
        status, _headers, body = _request(
            addr, "POST", "/api/tenants/delete",
            body=json.dumps({"tenant": "acme"}),
            headers={"Content-Type": "application/json", "Cookie": cookie})
        assert status == 200
        assert json.loads(body)["deleted"] is True
        assert not tdir.exists()


# ---------------------------------------------------------------- 削除前の影響範囲

def test_impact_reports_users_conversations_tasks_and_settings(server):
    base, tmp_path = server
    _create(base, "acme")
    tdir = _seed_tenant_files(tmp_path)
    # 用事の一覧: 既定ユーザーに2件（未完了1・完了1）、bob に1件（未完了）
    (tdir / "tasks.json").write_text(json.dumps([
        {"title": "牛乳を買う", "status": "open"},
        {"title": "手紙を出す", "status": "done"},
    ]), encoding="utf-8")
    bob = tdir / "users" / "bob"
    (bob / "tasks.json").write_text(json.dumps([
        {"title": "予約を取る", "status": "open"},
    ]), encoding="utf-8")
    # テナント設定: 能動発信オフ、保持期限 45 日
    status, _data = _post(base + "/api/tenant_settings",
                          {"tenant": "acme", "proactive_enabled": False,
                           "retention_days": 45})
    assert status == 200

    status, data = _get(base + "/api/tenants/impact?tenant=acme")
    assert status == 200
    assert data["tenant"] == "acme"
    assert data["user_count"] == 2
    assert data["conversations"] == 1
    assert data["tasks"] == 3
    assert data["tasks_open"] == 2
    assert data["proactive_enabled"] is False
    assert data["retention_effective_days"] == 45
    assert data["size_bytes"] > 0
    by_user = {u["user"]: u for u in data["users"]}
    assert by_user["default"]["conversations"] == 1
    assert by_user["default"]["tasks"] == 2
    assert by_user["bob"]["tasks"] == 1
    assert by_user["bob"]["tasks_open"] == 1


def test_impact_changes_nothing(server):
    """影響範囲の取得は読み取りだけで、置き場に何も作らない・消さない。"""
    base, tmp_path = server
    _create(base, "acme")
    tdir = _seed_tenant_files(tmp_path)
    before = sorted(str(p.relative_to(tmp_path))
                    for p in tmp_path.rglob("*"))
    status, _data = _get(base + "/api/tenants/impact?tenant=acme")
    assert status == 200
    after = sorted(str(p.relative_to(tmp_path))
                   for p in tmp_path.rglob("*"))
    assert before == after
    assert tdir.is_dir()


def test_impact_unknown_tenant_is_404(server):
    base, _tmp_path = server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(base + "/api/tenants/impact?tenant=ghost")
    assert excinfo.value.code == 404


def test_impact_default_tenant_ok(server):
    base, _tmp_path = server
    status, data = _get(base + "/api/tenants/impact?tenant=default")
    assert status == 200
    assert data["tenant"] == "default"
    assert data["user_count"] >= 1


def test_admin_page_has_impact_confirmation(server):
    """管理画面に、影響範囲を表示する削除の確認欄が含まれている。"""
    base, _tmp_path = server
    with urllib.request.urlopen(base + "/", timeout=10) as resp:
        page = resp.read().decode("utf-8")
    assert "tenantDeleteConfirm" in page
    assert "/api/tenants/impact" in page
    assert "showTenantDeleteConfirm" in page
