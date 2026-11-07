"""公開先の固定（0.68.0、公開前チェック指摘3・選択肢C）のテスト。

環境変数 KUMA_WEB_TENANT / KUMA_WEB_USER で公開してよい
テナント／ユーザーの組を 1 組に固定し、食い違う組への API
アクセスが 403 になること、固定が無いときは従来どおりである
ことを確認する。照合は設定された軸だけ（一貫した意味）。
"""
import json
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.web import TenantStore, build_server


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


@contextmanager
def running_server(tmp_path: Path, **cfg_kwargs):
    """指定の設定で Web サーバーを立て、ベース URL を返す。"""
    session = Session(Config(data_dir=tmp_path, **cfg_kwargs),
                      _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, dict | None]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            body, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        body, status = exc.read(), exc.code
    try:
        return status, json.loads(body.decode("utf-8"))
    except ValueError:
        return status, None


def _post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


# ---------------------------------------------------------- 固定なし

def test_no_pin_behaves_as_before(tmp_path: Path):
    with running_server(tmp_path) as base:
        status, data = _get(base + "/api/tenants")
        assert status == 200
        assert "default" in data["tenants"]

        status, data = _post(base + "/api/tenants/create",
                             {"name": "acme"})
        assert status == 200

        status, data = _get(base + "/api/tenants")
        assert status == 200
        assert "acme" in data["tenants"]

        status, _data = _get(base + "/api/status?tenant=acme&user=bob")
        assert status == 200

        status, data = _get(base + "/api/users?tenant=acme")
        assert status == 200
        assert "default" in data["users"]


# ---------------------------------------------------------- 両軸の固定

def test_pin_both_axes(tmp_path: Path):
    with running_server(tmp_path, web_tenant="acme",
                        web_user="bob") as base:
        # 一覧は固定先だけが返る
        status, data = _get(base + "/api/tenants")
        assert status == 200
        assert data == {"tenants": ["acme"], "current": "acme"}

        status, data = _get(base + "/api/users?tenant=acme")
        assert status == 200
        assert data == {"users": ["bob"], "current": "bob"}

        # テナント軸が食い違う一覧は 403
        status, data = _get(base + "/api/users?tenant=other")
        assert status == 403
        assert "固定" in data["error"]

        # 固定先の組は通り、どちらか一方でも食い違えば 403
        status, _ = _get(base + "/api/status?tenant=acme&user=bob")
        assert status == 200
        status, _ = _get(base + "/api/status?tenant=acme&user=alice")
        assert status == 403
        status, _ = _get(base + "/api/status?tenant=other&user=bob")
        assert status == 403
        # 無指定は起動時の組（default/default）になり固定先でない
        status, _ = _get(base + "/api/status")
        assert status == 403

        # 会話・履歴・書き出し・他ユーザー向け API も同じ判定
        status, _ = _get(base + "/api/history?tenant=acme&user=bob")
        assert status == 200
        status, _ = _get(base + "/api/history?tenant=other&user=bob")
        assert status == 403
        status, _ = _get(base + "/api/export?tenant=acme&user=bob")
        assert status == 200
        status, _ = _get(base + "/api/export?tenant=other&user=bob")
        assert status == 403
        status, _ = _get(base + "/api/goals?tenant=other&user=bob")
        assert status == 403

        status, data = _post(base + "/api/chat",
                             {"tenant": "other", "user": "bob",
                              "message": "やあ"})
        assert status == 403
        status, data = _post(base + "/api/chat",
                             {"tenant": "acme", "user": "bob",
                              "message": "やあ"})
        assert status == 200
        assert "返答:" in data["reply"]

        # 管理系: テナント設定も固定先以外は 403、固定先は通る
        status, _ = _get(base + "/api/tenant_settings?tenant=other")
        assert status == 403
        status, _ = _get(base + "/api/tenant_settings?tenant=acme")
        assert status == 200
        status, _ = _post(base + "/api/tenant_settings",
                          {"tenant": "other", "proactive_enabled": False})
        assert status == 403
        status, _ = _post(base + "/api/tenant_settings",
                          {"tenant": "acme", "proactive_enabled": False})
        assert status == 200

        # セッション削除も固定先以外は 403
        status, _ = _post(base + "/api/sessions/delete",
                          {"tenant": "other", "user": "bob",
                           "session": "main"})
        assert status == 403


def test_pin_admin_tenant_create_delete(tmp_path: Path):
    with running_server(tmp_path, web_tenant="acme") as base:
        # 固定先以外のテナントは作れない
        status, _ = _post(base + "/api/tenants/create", {"name": "other"})
        assert status == 403
        # 固定先そのものは従来どおり作れる
        status, data = _post(base + "/api/tenants/create",
                             {"name": "acme"})
        assert status == 200
        assert data["created"] is True

        # 固定先以外のテナントは、置き場があっても消せない
        (tmp_path / "tenants" / "other").mkdir(parents=True)
        status, _ = _post(base + "/api/tenants/delete",
                          {"tenant": "other"})
        assert status == 403
        assert (tmp_path / "tenants" / "other").is_dir()

        # 固定先そのものの削除は従来どおり（起動時テナントではない）
        status, data = _post(base + "/api/tenants/delete",
                             {"tenant": "acme"})
        assert status == 200
        assert data["deleted"] is True


# ------------------------------------------------------ 片方だけの固定

def test_pin_tenant_only(tmp_path: Path):
    with running_server(tmp_path, web_tenant="acme") as base:
        # ユーザー軸は問わない
        status, _ = _get(base + "/api/status?tenant=acme&user=alice")
        assert status == 200
        status, _ = _get(base + "/api/status?tenant=other&user=bob")
        assert status == 403

        # ユーザー一覧は絞られない
        status, data = _get(base + "/api/users?tenant=acme")
        assert status == 200
        assert "default" in data["users"]

        status, data = _get(base + "/api/tenants")
        assert status == 200
        assert data["tenants"] == ["acme"]


def test_pin_user_only(tmp_path: Path):
    with running_server(tmp_path, web_user="bob") as base:
        # テナント軸は問わない
        status, _ = _get(base + "/api/status?tenant=whatever&user=bob")
        assert status == 200
        status, _ = _get(base + "/api/status?tenant=whatever&user=alice")
        assert status == 403

        # テナント一覧は絞られない
        status, data = _get(base + "/api/tenants")
        assert status == 200
        assert "default" in data["tenants"]
        assert data["current"] == "default"

        # ユーザー一覧は固定先だけ
        status, data = _get(base + "/api/users?tenant=default")
        assert status == 200
        assert data == {"users": ["bob"], "current": "bob"}


# ------------------------------------------------------ 判定そのもの

def test_store_pair_allowed_axes(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path, web_tenant="acme"),
                      _EchoModel())
    store = TenantStore(session)
    assert store.fixed_tenant == "acme"
    assert store.fixed_user is None
    assert store.tenant_allowed("acme")
    assert not store.tenant_allowed("other")
    assert store.pair_allowed("acme", "anyone")
    assert not store.pair_allowed("other", "anyone")

    session = Session(Config(data_dir=tmp_path), _EchoModel())
    store = TenantStore(session)
    assert store.fixed_tenant is None
    assert store.fixed_user is None
    assert store.pair_allowed("anything", "anyone")
