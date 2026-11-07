"""テナントごとの頭脳（第2段: テナント別 API キー）のテスト。

資格情報ファイル（brain/credentials.json）の保存と権限・破損時の
扱い、解決順序（テナントのキーが全体より優先、provider が食い違う
キーは使わない）、書き出し・控えからの除外と復元時の保全、
管理タブ API（設定・削除・検証・即時反映）、キーの値が画面
payload や settings.json に出ないことを確認する。実クライアントは
生成だけ行い、呼び出しはしない。
"""
import io
import json
import os
import stat
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

from kuma_agent.brain import (
    BRAIN_CREDENTIALS_PATH_PARTS,
    BrainCredentials,
    BrainCredentialsError,
    brain_credentials_path,
    brain_key_status,
    delete_brain_credentials,
    load_brain_credentials,
    resolve_brain,
    save_brain_credentials,
    scoped_config,
    tenant_api_key,
)
from kuma_agent.config import Config
from kuma_agent.export import (
    EXPORT_INFO_NAME,
    build_export_zip,
    build_tenant_backup_zip,
    iter_export_files,
)
from kuma_agent.llm import GeminiClient, Message, MetaModelClient
from kuma_agent.memory import tenant_dir, user_dir
from kuma_agent.restore import restore_user_data, save_user_backup
from kuma_agent.session import Session
from kuma_agent.settings import set_brain_setting
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


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _tenant_dir(data_dir: Path, tenant: str) -> Path:
    return tenant_dir(data_dir, tenant)


# ------------------------------------------------------- 資格情報ファイル

def test_save_and_load_brain_credentials_roundtrip(tmp_path: Path):
    tdir = tmp_path / "tenant"
    path = save_brain_credentials(tdir, "claude", "tenant-key-1")
    assert path == brain_credentials_path(tdir)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    # 保存するのは provider と api_key だけ
    assert data == {"provider": "claude", "api_key": "tenant-key-1"}
    assert load_brain_credentials(tdir) == BrainCredentials(
        "claude", "tenant-key-1")
    # 権限: ディレクトリ 0700、ファイル 0600
    assert _mode(tdir / "brain") == 0o700
    assert _mode(path) == 0o600


def test_load_brain_credentials_missing_returns_none(tmp_path: Path):
    assert load_brain_credentials(tmp_path / "tenant") is None
    assert brain_key_status(tmp_path, "acme") == {
        "state": "unset", "provider": None}


def test_load_brain_credentials_broken_is_invalid(tmp_path: Path):
    tdir = tmp_path / "tenant"
    path = save_brain_credentials(tdir, "claude", "tenant-key-1")
    path.write_text("{壊れた JSON", encoding="utf-8")
    with pytest.raises(BrainCredentialsError) as excinfo:
        load_brain_credentials(tdir)
    assert excinfo.value.kind == "invalid"
    # api_key が無い形も同様
    path.write_text(json.dumps({"provider": "claude"}), encoding="utf-8")
    with pytest.raises(BrainCredentialsError) as excinfo:
        load_brain_credentials(tdir)
    assert excinfo.value.kind == "invalid"
    # 状態としては「壊れている」と分かる（キーの値は出ない）
    data_dir = tmp_path / "data"
    save_brain_credentials(_tenant_dir(data_dir, "acme"),
                           "claude", "tenant-key-1")
    brain_credentials_path(_tenant_dir(data_dir, "acme")).write_text(
        "壊れた", encoding="utf-8")
    assert brain_key_status(data_dir, "acme") == {
        "state": "invalid", "provider": None}
    # 壊れていても解決は「キー無し」に倒れて落ちない
    assert tenant_api_key(data_dir, "acme", "claude") is None


def test_load_brain_credentials_tightens_permissions(tmp_path: Path):
    tdir = tmp_path / "tenant"
    path = save_brain_credentials(tdir, "claude", "tenant-key-1")
    path.chmod(0o644)
    assert _mode(path) == 0o644
    assert load_brain_credentials(tdir) == BrainCredentials(
        "claude", "tenant-key-1")
    assert _mode(path) == 0o600  # 読み込み時に直る


def test_delete_brain_credentials(tmp_path: Path):
    tdir = tmp_path / "tenant"
    save_brain_credentials(tdir, "claude", "tenant-key-1")
    assert delete_brain_credentials(tdir) is True
    assert load_brain_credentials(tdir) is None
    assert delete_brain_credentials(tdir) is False


# ------------------------------------------------------- 解決順序

def test_tenant_api_key_requires_provider_match(tmp_path: Path):
    save_brain_credentials(_tenant_dir(tmp_path, "acme"),
                           "gemini", "gem-tenant-key")
    assert tenant_api_key(tmp_path, "acme", "gemini") == "gem-tenant-key"
    # provider が食い違うキーは使わない
    assert tenant_api_key(tmp_path, "acme", "claude") is None
    assert tenant_api_key(tmp_path, "beta", "gemini") is None


def test_scoped_config_prefers_tenant_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-env-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    set_brain_setting(tmp_path, "acme", "gemini", None)
    save_brain_credentials(_tenant_dir(tmp_path, "acme"),
                           "gemini", "gem-tenant-key")
    base = Config(data_dir=tmp_path, provider="claude",
                  model_api_key="claude-key")
    scoped = scoped_config(base, "acme")
    assert scoped.provider == "gemini"
    # テナントのキーが全体（環境変数）より優先する
    assert scoped.model_api_key == "gem-tenant-key"
    # テナントのキーが無ければ全体のキーに落ちる
    scoped_beta = scoped_config(base, "beta")
    assert scoped_beta.provider == "claude"
    assert scoped_beta.model_api_key == "claude-key"


def test_scoped_config_ignores_mismatched_tenant_key(
        tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-env-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    set_brain_setting(tmp_path, "acme", "gemini", None)
    # 実効プロバイダ（gemini）とは違うプロバイダ用のキーは使わない
    save_brain_credentials(_tenant_dir(tmp_path, "acme"),
                           "claude", "claude-tenant-key")
    base = Config(data_dir=tmp_path, provider="claude",
                  model_api_key="claude-key")
    scoped = scoped_config(base, "acme")
    assert scoped.provider == "gemini"
    assert scoped.model_api_key == "gem-env-key"


def test_scoped_config_tenant_key_without_brain_setting(
        tmp_path: Path, monkeypatch):
    # プロバイダ／モデルの指定が無く、キーだけあるテナントでも、
    # 実効プロバイダ（全体の既定）と一致すればテナントのキーが効く
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    save_brain_credentials(_tenant_dir(tmp_path, "acme"),
                           "muse", "muse-tenant-key")
    base = Config(data_dir=tmp_path, model_api_key="global-key")
    scoped = scoped_config(base, "acme")
    assert scoped.provider == "muse"
    assert scoped.model_api_key == "muse-tenant-key"


def test_resolve_brain_key_only_creates_own_client(tmp_path: Path):
    # キーだけのテナントは、全体の既定の実体を使い回さず、
    # テナントのキーで作り直す
    save_brain_credentials(_tenant_dir(tmp_path, "acme"),
                           "muse", "muse-tenant-key")
    base = Config(data_dir=tmp_path)
    fallback = _EchoModel()
    cfg, model = resolve_brain(base, "acme", fallback_model=fallback)
    assert model is not fallback
    assert isinstance(model, MetaModelClient)
    assert cfg.model_api_key == "muse-tenant-key"
    # キーの無いテナントは従来どおり fallback を使う
    cfg, model = resolve_brain(base, "beta", fallback_model=fallback)
    assert model is fallback


# ------------------------------------------------------- 書き出しと復元

def _seed_tenant_with_key(data_dir: Path, tenant: str = "default") -> Path:
    tdir = tenant_dir(data_dir, tenant)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "MEMORY.md").write_text("記憶", encoding="utf-8")
    save_brain_credentials(tdir, "claude", "tenant-secret-key")
    return tdir


def _zip_names(body: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return zf.namelist()


def test_export_excludes_brain_credentials(tmp_path: Path):
    _seed_tenant_with_key(tmp_path)  # default × default は置き場そのもの
    names = [p.relative_to(tmp_path).as_posix()
             for p in iter_export_files(tmp_path, "default", "default")]
    assert "/".join(BRAIN_CREDENTIALS_PATH_PARTS) not in names
    assert "MEMORY.md" in names
    zipped = _zip_names(build_export_zip(tmp_path, "default", "default"))
    assert "/".join(BRAIN_CREDENTIALS_PATH_PARTS) not in zipped
    assert "MEMORY.md" in zipped


def test_tenant_backup_excludes_brain_credentials(tmp_path: Path):
    data_dir = tmp_path / "data"
    _seed_tenant_with_key(data_dir)
    alice = user_dir(tenant_dir(data_dir, "default"), "alice")
    alice.mkdir(parents=True, exist_ok=True)
    (alice / "MEMORY.md").write_text("alice の記憶", encoding="utf-8")
    names = _zip_names(build_tenant_backup_zip(data_dir, "default"))
    assert not any(name.endswith("/".join(BRAIN_CREDENTIALS_PATH_PARTS))
                   for name in names)
    assert "MEMORY.md" in names
    assert "users/alice/MEMORY.md" in names


def test_user_backup_excludes_brain_credentials(tmp_path: Path):
    _seed_tenant_with_key(tmp_path)
    path = save_user_backup(tmp_path, "default", "default")
    names = _zip_names(path.read_bytes())
    assert "/".join(BRAIN_CREDENTIALS_PATH_PARTS) not in names
    assert "MEMORY.md" in names


def _hostile_zip(extra: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(EXPORT_INFO_NAME, json.dumps(
            {"format": "kuma-agent export",
             "tenant": "default", "user": "default"}))
        for name, text in extra.items():
            zf.writestr(name, text)
    return buffer.getvalue()


def test_restore_preserves_existing_brain_credentials(tmp_path: Path):
    tdir = _seed_tenant_with_key(tmp_path)  # default × default
    body = _hostile_zip({
        "MEMORY.md": "復元された記憶",
        "/".join(BRAIN_CREDENTIALS_PATH_PARTS):
            json.dumps({"provider": "claude",
                        "api_key": "injected-key"}),
    })
    result = restore_user_data(tmp_path, "default", "default", body)
    # ZIP 側のキーは書き戻されず、既存のものが保全される
    assert load_brain_credentials(tdir) == BrainCredentials(
        "claude", "tenant-secret-key")
    # キー以外は通常どおり置き換わる
    assert (tdir / "MEMORY.md").read_text(encoding="utf-8") \
        == "復元された記憶"
    assert result.skipped_files >= 1


def test_restore_does_not_create_brain_credentials(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("記憶", encoding="utf-8")
    body = _hostile_zip({
        "/".join(BRAIN_CREDENTIALS_PATH_PARTS):
            json.dumps({"provider": "claude",
                        "api_key": "injected-key"}),
    })
    restore_user_data(tmp_path, "default", "default", body)
    assert not brain_credentials_path(tmp_path).exists()
    assert load_brain_credentials(tmp_path) is None


# ------------------------------------------------------- 画面 API

def test_brain_key_save_and_payload(server):
    base, tmp_path, _httpd = server
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme",
                          "brain_api_key": "acme-secret-key"})
    assert status == 200
    assert data["brain_key_set"] is True
    assert data["brain_key_provider"] == "muse"  # 実効プロバイダ用
    assert data["brain_key_invalid"] is False
    # キーの値は応答のどこにも出ない
    assert "acme-secret-key" not in json.dumps(data)
    # settings.json にもキーは書かれない（資格情報ファイルだけ）
    settings_text = (tmp_path / "settings.json").read_text(encoding="utf-8") \
        if (tmp_path / "settings.json").is_file() else ""
    assert "acme-secret-key" not in settings_text
    tdir = _tenant_dir(tmp_path, "acme")
    assert load_brain_credentials(tdir) == BrainCredentials(
        "muse", "acme-secret-key")
    assert _mode(brain_credentials_path(tdir)) == 0o600
    # GET でも同じ状態が返る
    status, data = _get(base + "/api/tenant_settings?tenant=acme")
    assert status == 200
    assert data["brain_key_set"] is True
    assert data["brain_key_provider"] == "muse"


def test_brain_key_empty_string_keeps_existing(server):
    base, tmp_path, _httpd = server
    _post(base + "/api/tenant_settings",
          {"tenant": "acme", "brain_api_key": "acme-secret-key"})
    # 空文字は「変更しない」
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "brain_api_key": ""})
    assert status == 200
    assert data["brain_key_set"] is True
    assert load_brain_credentials(_tenant_dir(tmp_path, "acme")) \
        == BrainCredentials("muse", "acme-secret-key")


def test_brain_key_delete(server):
    base, tmp_path, _httpd = server
    _post(base + "/api/tenant_settings",
          {"tenant": "acme", "brain_api_key": "acme-secret-key"})
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme", "brain_key_delete": True})
    assert status == 200
    assert data["brain_key_set"] is False
    assert data["brain_key_provider"] is None
    assert load_brain_credentials(_tenant_dir(tmp_path, "acme")) is None


def test_brain_key_validation(server):
    base, _tmp_path, _httpd = server
    status, _data = _post(base + "/api/tenant_settings",
                          {"tenant": "acme", "brain_api_key": 123})
    assert status == 400
    status, _data = _post(base + "/api/tenant_settings",
                          {"tenant": "acme",
                           "brain_api_key": "x" * 1001})
    assert status == 400
    status, _data = _post(base + "/api/tenant_settings",
                          {"tenant": "acme", "brain_key_delete": "yes"})
    assert status == 400


def test_brain_key_saved_for_new_provider_in_same_request(
        server, monkeypatch):
    base, tmp_path, _httpd = server
    monkeypatch.setenv("GEMINI_API_KEY", "gem-env-key")
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "acme",
                          "brain": {"provider": "gemini"},
                          "brain_api_key": "gem-tenant-key"})
    assert status == 200
    # キーは、同じリクエストで切り替えた新しいプロバイダ用として
    # 記録され、そのテナントの解決で全体のキーより優先する
    assert data["brain_key_provider"] == "gemini"
    assert load_brain_credentials(_tenant_dir(tmp_path, "acme")) \
        == BrainCredentials("gemini", "gem-tenant-key")
    scoped = scoped_config(Config(data_dir=tmp_path), "acme")
    assert scoped.model_api_key == "gem-tenant-key"


def test_brain_key_change_reflects_immediately(server):
    base, tmp_path, httpd = server
    store = httpd.tenants.store("acme", None)
    before = store.get("main")
    assert isinstance(before.model, _EchoModel)
    status, _data = _post(base + "/api/tenant_settings",
                          {"tenant": "acme",
                           "brain_api_key": "acme-secret-key"})
    assert status == 200
    # キーだけの変更でも Session は破棄され、テナントのキーで
    # 作り直される
    after = store.get("main")
    assert after is not before
    assert after.cfg.model_api_key == "acme-secret-key"
    assert isinstance(after.model, MetaModelClient)


def test_brain_key_rejected_for_cli_provider(tmp_path: Path):
    # 全体の既定が CLI 系のときは、API キーは設定できない
    cfg = Config(data_dir=tmp_path, provider="claude-code")
    session = Session(cfg, _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        status, _data = _post(base + "/api/tenant_settings",
                              {"tenant": "acme",
                               "brain_api_key": "acme-secret-key"})
        assert status == 400
        assert load_brain_credentials(_tenant_dir(tmp_path, "acme")) is None
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_brain_key_broken_file_shows_invalid_in_payload(server):
    base, tmp_path, _httpd = server
    tdir = _tenant_dir(tmp_path, "acme")
    save_brain_credentials(tdir, "muse", "acme-secret-key")
    brain_credentials_path(tdir).write_text("壊れた", encoding="utf-8")
    status, data = _get(base + "/api/tenant_settings?tenant=acme")
    assert status == 200
    assert data["brain_key_set"] is False
    assert data["brain_key_invalid"] is True
    # 壊れたファイルの中身（キーの値だった文字列）は応答に出ない
    assert "acme-secret-key" not in json.dumps(data)
