"""Google カレンダー読み取り連携の土台（google_calendar.py）のテスト。

資格情報の保存・読み戻し（権限とテナント×ユーザーの分離を
含む）、書き出し・控え・復元からの資格情報の除外、トークン
更新と接続確認の振る舞い（Google 側は httpx.MockTransport で
差し替え、実ネットワークには出ない）、資格情報が未設定なら
連携が無効として静かに扱われることを確認する。認可の流れは、
ループバックの戻り先（127.0.0.1 の一時ポート）へ実際に
HTTP でアクセスして検証する。
"""
import base64
import hashlib
import io
import json
import stat
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.export import (
    EXPORT_INFO_NAME,
    build_export_zip,
    build_tenant_backup_zip,
    iter_export_files,
)
from kuma_agent.google_calendar import (
    EVENTS_READONLY_SCOPE,
    REQUESTED_SCOPES,
    ClientSecrets,
    Credentials,
    GoogleCalendarClient,
    GoogleCalendarError,
    authorize,
    build_client,
    credentials_path,
    delete_credentials,
    load_client_secrets,
    load_credentials,
    revoke_refresh_token,
    save_credentials,
)
from kuma_agent.memory import tenant_dir, user_dir
from kuma_agent.restore import restore_user_data, save_user_backup

SECRETS = ClientSecrets(client_id="client-id-123",
                        client_secret="client-secret-xyz")


def _creds(refresh_token: str = "refresh-token-1") -> Credentials:
    return Credentials(
        refresh_token=refresh_token,
        scope=EVENTS_READONLY_SCOPE,
        authorized_at="2026-11-07T10:00:00+09:00",
        client_id="client-id-123")


def _write_secrets_file(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ------------------------------------------------------- 資格情報の保存

def test_save_and_load_credentials_roundtrip(tmp_path: Path) -> None:
    udir = tmp_path / "user"
    path = save_credentials(udir, _creds())
    assert path == credentials_path(udir)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    # 保存するのは必要最小限だけ（アクセストークンやシークレットは無い）
    assert set(data) == {"refresh_token", "scope", "authorized_at",
                         "client_id"}
    assert data["refresh_token"] == "refresh-token-1"
    loaded = load_credentials(udir)
    assert loaded == _creds()
    # 権限: ディレクトリ 0700、ファイル 0600
    assert _mode(udir / "google-calendar") == 0o700
    assert _mode(path) == 0o600


def test_credentials_are_separated_per_user(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    alice = user_dir(tenant_dir(data_dir, "default"), "alice")
    bob = user_dir(tenant_dir(data_dir, "default"), "bob")
    assert alice != bob
    save_credentials(alice, _creds("alice-token"))
    save_credentials(bob, _creds("bob-token"))
    assert load_credentials(alice).refresh_token == "alice-token"
    assert load_credentials(bob).refresh_token == "bob-token"
    # 片方を消しても、もう片方には影響しない
    assert delete_credentials(alice) is True
    assert load_credentials(alice) is None
    assert load_credentials(bob).refresh_token == "bob-token"
    assert delete_credentials(alice) is False


def test_load_credentials_missing_returns_none(tmp_path: Path) -> None:
    assert load_credentials(tmp_path / "user") is None


def test_load_credentials_broken_raises_invalid(tmp_path: Path) -> None:
    udir = tmp_path / "user"
    path = save_credentials(udir, _creds())
    path.write_text("{壊れた JSON", encoding="utf-8")
    with pytest.raises(GoogleCalendarError) as excinfo:
        load_credentials(udir)
    assert excinfo.value.kind == "invalid"
    # リフレッシュトークンが無い形も同様
    path.write_text(json.dumps({"scope": "x"}), encoding="utf-8")
    with pytest.raises(GoogleCalendarError) as excinfo:
        load_credentials(udir)
    assert excinfo.value.kind == "invalid"


def test_load_credentials_tightens_permissions(tmp_path: Path) -> None:
    udir = tmp_path / "user"
    path = save_credentials(udir, _creds())
    path.chmod(0o644)
    assert _mode(path) == 0o644
    assert load_credentials(udir) == _creds()
    assert _mode(path) == 0o600  # 読み込み時に直る


# ------------------------------------------------- クライアント設定の読込

def test_load_client_secrets_from_installed_json(tmp_path: Path) -> None:
    path = _write_secrets_file(tmp_path, {"installed": {
        "client_id": "abc", "client_secret": "def",
        "auth_uri": "https://example.invalid/auth",
        "token_uri": "https://example.invalid/token"}})
    secrets = load_client_secrets(path)
    assert secrets.client_id == "abc"
    assert secrets.client_secret == "def"
    assert secrets.auth_uri == "https://example.invalid/auth"
    assert secrets.token_uri == "https://example.invalid/token"


def test_load_client_secrets_errors_are_config_error(
        tmp_path: Path) -> None:
    with pytest.raises(GoogleCalendarError) as excinfo:
        load_client_secrets(tmp_path / "ありません.json")
    assert excinfo.value.kind == "config_error"
    broken = _write_secrets_file(tmp_path, {"installed": {}})
    with pytest.raises(GoogleCalendarError) as excinfo:
        load_client_secrets(broken)
    assert excinfo.value.kind == "config_error"
    # 秘密の値が文面に漏れていない
    assert "def" not in excinfo.value.message


def test_build_client_with_broken_secrets_file(tmp_path: Path) -> None:
    broken = _write_secrets_file(tmp_path, {"installed": {}})
    client = build_client(tmp_path / "user", enabled=True,
                          client_secrets_file=str(broken))
    status = client.status()
    assert status.state == "not_configured"
    assert client.secrets_error  # 理由は区別できる


# ------------------------------------------------------- 書き出し・控え

def _seed_user_dir(udir: Path) -> None:
    udir.mkdir(parents=True, exist_ok=True)
    (udir / "MEMORY.md").write_text("長期記憶", encoding="utf-8")
    save_credentials(udir, _creds())
    settings = credentials_path(udir).parent / "settings.json"
    settings.write_text("{}", encoding="utf-8")


def _zip_names(body: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return zf.namelist()


def test_export_excludes_credentials(tmp_path: Path) -> None:
    _seed_user_dir(tmp_path)  # default × default は置き場そのもの
    names = [p.relative_to(tmp_path).as_posix()
             for p in iter_export_files(tmp_path, "default", "default")]
    assert "google-calendar/credentials.json" not in names
    assert "google-calendar/settings.json" in names
    zipped = _zip_names(build_export_zip(tmp_path, "default", "default"))
    assert "google-calendar/credentials.json" not in zipped
    assert "google-calendar/settings.json" in zipped
    assert "MEMORY.md" in zipped


def test_tenant_backup_excludes_credentials(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_user_dir(data_dir)  # 既定ユーザーはテナント置き場そのもの
    alice = user_dir(tenant_dir(data_dir, "default"), "alice")
    _seed_user_dir(alice)
    names = _zip_names(build_tenant_backup_zip(data_dir, "default"))
    assert not any(name.endswith("google-calendar/credentials.json")
                   for name in names)
    assert "google-calendar/settings.json" in names
    assert "users/alice/google-calendar/settings.json" in names
    assert "users/alice/MEMORY.md" in names


def test_user_backup_excludes_credentials(tmp_path: Path) -> None:
    _seed_user_dir(tmp_path)
    path = save_user_backup(tmp_path, "default", "default")
    names = _zip_names(path.read_bytes())
    assert "google-calendar/credentials.json" not in names
    assert "google-calendar/settings.json" in names


# ---------------------------------------------------------------- 復元

def _hostile_zip(extra: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(EXPORT_INFO_NAME, json.dumps(
            {"format": "kuma-agent export",
             "tenant": "default", "user": "default"}))
        for name, text in extra.items():
            zf.writestr(name, text)
    return buffer.getvalue()


def test_restore_preserves_existing_credentials(tmp_path: Path) -> None:
    udir = tmp_path  # default × default
    save_credentials(udir, _creds("original-token"))
    body = _hostile_zip({
        "MEMORY.md": "復元された記憶",
        "google-calendar/credentials.json":
            json.dumps({"refresh_token": "injected-token"}),
        "google-calendar/settings.json": "{\"from\": \"zip\"}",
    })
    result = restore_user_data(tmp_path, "default", "default", body)
    # ZIP 側の資格情報は書き戻されず、既存のものが保全される
    loaded = load_credentials(udir)
    assert loaded is not None
    assert loaded.refresh_token == "original-token"
    # 資格情報以外は通常どおり置き換わる
    assert (udir / "MEMORY.md").read_text(encoding="utf-8") \
        == "復元された記憶"
    settings = json.loads(
        (udir / "google-calendar" / "settings.json")
        .read_text(encoding="utf-8"))
    assert settings == {"from": "zip"}
    assert result.skipped_files >= 1


def test_restore_does_not_create_credentials(tmp_path: Path) -> None:
    body = _hostile_zip({
        "MEMORY.md": "記憶",
        "google-calendar/credentials.json":
            json.dumps({"refresh_token": "injected-token"}),
    })
    restore_user_data(tmp_path, "default", "default", body)
    assert not credentials_path(tmp_path).exists()
    assert load_credentials(tmp_path) is None


# ------------------------------------------- トークン更新と接続確認

class _GoogleStub:
    """Google 側のモック。トークンとカレンダーの応答を差し替える。"""

    def __init__(self) -> None:
        self.token_calls: list[dict[str, str]] = []
        self.calendar_calls: list[httpx.Request] = []
        self.token_status = 200
        self.token_body: dict = {"access_token": "access-1",
                                 "expires_in": 3600}
        self.calendar_status = 200
        self.raise_network = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.raise_network:
            raise httpx.ConnectError("接続できません",
                                     request=request)
        if request.url.path == "/token":
            form = {key: values[0] for key, values
                    in parse_qs(request.content.decode()).items()}
            self.token_calls.append(form)
            return httpx.Response(self.token_status,
                                  json=self.token_body)
        if request.url.path == "/calendar/v3/calendars/primary":
            self.calendar_calls.append(request)
            body = {"id": "primary"} \
                if self.calendar_status == 200 else {}
            return httpx.Response(self.calendar_status, json=body)
        raise AssertionError(f"想定外の宛先: {request.url.path}")


def _client(tmp_path: Path, stub: _GoogleStub, **kwargs) \
        -> GoogleCalendarClient:
    return GoogleCalendarClient(
        tmp_path, SECRETS, transport=stub.transport(), **kwargs)


def test_access_token_refresh_and_cache(tmp_path: Path) -> None:
    save_credentials(tmp_path, _creds())
    stub = _GoogleStub()
    now = [datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)]
    client = _client(tmp_path, stub, now_fn=lambda: now[0])
    assert client.access_token() == "access-1"
    assert len(stub.token_calls) == 1
    form = stub.token_calls[0]
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "refresh-token-1"
    assert form["client_id"] == "client-id-123"
    # 期限に余裕がある間はキャッシュを使う
    assert client.access_token() == "access-1"
    assert len(stub.token_calls) == 1
    # 期限を過ぎたら取り直す
    stub.token_body = {"access_token": "access-2", "expires_in": 3600}
    now[0] += timedelta(seconds=3600)
    assert client.access_token() == "access-2"
    assert len(stub.token_calls) == 2


def test_refresh_rotates_refresh_token(tmp_path: Path) -> None:
    save_credentials(tmp_path, _creds())
    stub = _GoogleStub()
    stub.token_body = {"access_token": "access-1", "expires_in": 3600,
                       "refresh_token": "refresh-token-2"}
    client = _client(tmp_path, stub)
    assert client.access_token() == "access-1"
    loaded = load_credentials(tmp_path)
    assert loaded is not None
    assert loaded.refresh_token == "refresh-token-2"
    assert loaded.authorized_at == "2026-11-07T10:00:00+09:00"  # 据え置き


def test_refresh_invalid_grant_is_revoked(tmp_path: Path) -> None:
    save_credentials(tmp_path, _creds())
    stub = _GoogleStub()
    stub.token_status = 400
    stub.token_body = {"error": "invalid_grant",
                       "error_description": "SECRET-MARKER"}
    client = _client(tmp_path, stub)
    with pytest.raises(GoogleCalendarError) as excinfo:
        client.access_token()
    assert excinfo.value.kind == "revoked"
    # Google の応答本文は文面へ漏らさない
    assert "SECRET-MARKER" not in excinfo.value.message
    status = client.check_connection()
    assert status.state == "revoked"
    assert "SECRET-MARKER" not in status.message


def test_check_connection_ok(tmp_path: Path) -> None:
    save_credentials(tmp_path, _creds())
    stub = _GoogleStub()
    client = _client(tmp_path, stub)
    status = client.check_connection()
    assert status.state == "connected"
    assert len(stub.calendar_calls) == 1
    assert stub.calendar_calls[0].headers["Authorization"] \
        == "Bearer access-1"


def test_check_connection_network_failure(tmp_path: Path) -> None:
    save_credentials(tmp_path, _creds())
    stub = _GoogleStub()
    stub.raise_network = True
    client = _client(tmp_path, stub)
    with pytest.raises(GoogleCalendarError) as excinfo:
        client.access_token()
    assert excinfo.value.kind == "network"
    assert client.check_connection().state == "failed"


def test_api_get_retries_once_on_401(tmp_path: Path) -> None:
    save_credentials(tmp_path, _creds())
    stub = _GoogleStub()
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(200, json={
                "access_token": "access-x", "expires_in": 3600})
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"id": "primary"})

    client = GoogleCalendarClient(
        tmp_path, SECRETS, transport=httpx.MockTransport(handler))
    body = client.api_get("/calendars/primary", params={"fields": "id"})
    assert body == {"id": "primary"}
    assert attempts["count"] == 2


# ------------------------------------------------- 未設定時の静かな扱い

def _exploding_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("ネットワークへ出てはいけない")
    return httpx.MockTransport(handler)


def test_status_when_not_configured_is_quiet(tmp_path: Path) -> None:
    client = GoogleCalendarClient(
        tmp_path, None, transport=_exploding_transport())
    assert client.status().state == "not_configured"
    assert client.check_connection().state == "not_configured"
    with pytest.raises(GoogleCalendarError) as excinfo:
        client.access_token()
    assert excinfo.value.kind == "not_configured"


def test_status_when_disabled_is_quiet(tmp_path: Path) -> None:
    save_credentials(tmp_path, _creds())
    client = GoogleCalendarClient(
        tmp_path, SECRETS, enabled=False,
        transport=_exploding_transport())
    assert client.status().state == "disabled"
    assert client.check_connection().state == "disabled"


def test_status_when_not_authorized_is_quiet(tmp_path: Path) -> None:
    stub = _GoogleStub()
    client = _client(tmp_path, stub)
    assert client.status().state == "not_authorized"
    assert client.check_connection().state == "not_authorized"
    assert stub.token_calls == []  # 通信していない


def test_status_when_credentials_broken(tmp_path: Path) -> None:
    path = save_credentials(tmp_path, _creds())
    path.write_text("壊れています", encoding="utf-8")
    client = _client(tmp_path, _GoogleStub())
    assert client.status().state == "invalid"
    assert client.check_connection().state == "invalid"


def test_config_fields_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUMA_GOOGLE_CALENDAR", "0")
    monkeypatch.setenv("KUMA_GOOGLE_CALENDAR_CLIENT_SECRETS_FILE",
                       "/somewhere/client_secret.json")
    cfg = Config()
    assert cfg.google_calendar_enabled is False
    assert cfg.google_calendar_client_secrets_file \
        == "/somewhere/client_secret.json"
    monkeypatch.delenv("KUMA_GOOGLE_CALENDAR")
    monkeypatch.delenv("KUMA_GOOGLE_CALENDAR_CLIENT_SECRETS_FILE")
    cfg = Config()
    assert cfg.google_calendar_enabled is True
    assert cfg.google_calendar_client_secrets_file == ""


# ---------------------------------------------------------------- 認可

class _AuthRun:
    """authorize を別スレッドで回し、認可 URL を捕まえる。"""

    def __init__(self, tmp_path: Path, transport) -> None:
        self.url: str | None = None
        self.error: GoogleCalendarError | None = None
        self.credentials: Credentials | None = None
        self._opened = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(tmp_path, transport), daemon=True)

    def _run(self, tmp_path: Path, transport) -> None:
        def open_url(url: str) -> None:
            self.url = url
            self._opened.set()

        try:
            self.credentials = authorize(
                tmp_path, SECRETS, open_url=open_url,
                transport=transport)
        except GoogleCalendarError as exc:
            self.error = exc

    def start_and_wait_url(self) -> str:
        self._thread.start()
        assert self._opened.wait(timeout=10)
        assert self.url is not None
        return self.url

    def finish(self) -> None:
        self._thread.join(timeout=10)
        assert not self._thread.is_alive()


def _auth_query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values
            in parse_qs(urlparse(url).query).items()}


def _callback(url: str, **params: str) -> None:
    # ループバックへの素の HTTP アクセス。プロキシ環境変数の影響を
    # 受けないよう http.client で直接つなぐ
    import http.client

    query = _auth_query(url)
    redirect = urlparse(query["redirect_uri"])
    qs = urlencode(params)
    conn = http.client.HTTPConnection(redirect.hostname, redirect.port,
                                      timeout=10)
    try:
        conn.request("GET", f"/?{qs}")
        conn.getresponse().read()
    finally:
        conn.close()


def _exchange_transport(captured: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        form = {key: values[0] for key, values
                in parse_qs(request.content.decode()).items()}
        captured.update(form)
        return httpx.Response(200, json={
            "access_token": "access-1", "expires_in": 3600,
            "refresh_token": "refresh-from-auth",
            "scope": EVENTS_READONLY_SCOPE})

    return httpx.MockTransport(handler)


def test_authorize_flow_saves_credentials(tmp_path: Path) -> None:
    captured: dict = {}
    run = _AuthRun(tmp_path, _exchange_transport(captured))
    url = run.start_and_wait_url()
    query = _auth_query(url)
    # 0.78.0 から、予定とカレンダー一覧の読み取り専用スコープを
    # 両方要求する（増分認可）
    assert query["scope"] == " ".join(REQUESTED_SCOPES)
    assert query["include_granted_scopes"] == "true"
    assert query["access_type"] == "offline"
    assert query["code_challenge_method"] == "S256"
    _callback(url, code="auth-code-1", state=query["state"])
    run.finish()
    assert run.error is None
    loaded = load_credentials(tmp_path)
    assert loaded is not None
    assert loaded.refresh_token == "refresh-from-auth"
    # PKCE: 交換時の verifier から作った challenge が認可 URL と一致
    verifier = captured["code_verifier"]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest) \
        .rstrip(b"=").decode("ascii")
    assert challenge == query["code_challenge"]
    assert captured["grant_type"] == "authorization_code"
    assert captured["redirect_uri"] == query["redirect_uri"]


def test_authorize_rejects_state_mismatch(tmp_path: Path) -> None:
    run = _AuthRun(tmp_path, _exchange_transport({}))
    url = run.start_and_wait_url()
    _callback(url, code="auth-code-1", state="違うstate")
    run.finish()
    assert run.error is not None
    assert run.error.kind == "state_mismatch"
    assert not credentials_path(tmp_path).exists()


def test_authorize_denied(tmp_path: Path) -> None:
    run = _AuthRun(tmp_path, _exchange_transport({}))
    url = run.start_and_wait_url()
    query = _auth_query(url)
    _callback(url, error="access_denied", state=query["state"])
    run.finish()
    assert run.error is not None
    assert run.error.kind == "denied"
    assert not credentials_path(tmp_path).exists()


def test_revoke_refresh_token(tmp_path: Path) -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert form["token"] == ["refresh-token-1"]
        return httpx.Response(200)

    assert revoke_refresh_token(
        SECRETS, "refresh-token-1", httpx.MockTransport(ok)) is True

    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("接続できません", request=request)

    assert revoke_refresh_token(
        SECRETS, "refresh-token-1",
        httpx.MockTransport(failing)) is False


# ---------------------------------------------------------------- CLI

def _cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KUMA_GOOGLE_CALENDAR_CLIENT_SECRETS_FILE",
                       raising=False)
    monkeypatch.delenv("KUMA_GOOGLE_CALENDAR", raising=False)


def test_cli_status_unconfigured(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    _cli_env(monkeypatch, tmp_path)
    assert cli.main(["google-calendar", "status"]) == 0
    out = capsys.readouterr().out
    assert "まだ設定されていません" in out


def test_cli_auth_requires_client_secrets(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    _cli_env(monkeypatch, tmp_path)
    assert cli.main(["google-calendar", "auth"]) == 2
    out = capsys.readouterr().out
    assert "KUMA_GOOGLE_CALENDAR_CLIENT_SECRETS_FILE" in out


def test_cli_revoke_removes_credentials(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture) -> None:
    _cli_env(monkeypatch, tmp_path)
    save_credentials(tmp_path, _creds())  # default × default の置き場
    assert cli.main(["google-calendar", "revoke"]) == 0
    out = capsys.readouterr().out
    assert "解除しました" in out
    assert not credentials_path(tmp_path).exists()
    assert "refresh-token-1" not in out  # トークンの値は出さない
    # 資格情報が無い状態でもう一度実行しても穏やかに終わる
    assert cli.main(["google-calendar", "revoke"]) == 0
    out = capsys.readouterr().out
    assert "ありませんでした" in out
