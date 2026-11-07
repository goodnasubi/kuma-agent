"""Google カレンダーの読み取り連携の土台（認可・資格情報・接続確認）。

利用者本人の Google カレンダーを **読み取りだけ** に使うための
基盤。認証は OAuth 2.0 のデスクトップアプリ型（PKCE・ループ
バック戻り先）で、認可は CLI コマンド（cli.py の
`google-calendar`）から回す。スコープは予定の読み取り専用だけ
を要求し、このモジュールに Google API への書き込み呼び出しは
置かない（設計の正は目標文書
google-calendar-tool-design-2026-11-07.md、要約は DESIGN.md
の「Google カレンダー連携」）。

資格情報の扱い:

- リフレッシュトークンは利用者ごとに、その利用者の置き場の
  `google-calendar/credentials.json` へ置く（置き場は
  ディレクトリ 0700・ファイル 0600、一時ファイル経由の原子的
  置換）。アクセストークンはメモリ上だけに保持する
- 資格情報ファイルは、書き出し・控え・復元から除外される
  （export.py / restore.py がこのモジュールのパス定義を使う）
- トークン・認可コード・クライアントシークレットは、例外
  文面・標準出力・活動の記録・会話記録のどこにも出さない。
  HTTP の失敗は Telegram / LINE と同じく一般的な文面へ包む
  （httpx の例外文面には URL が含まれ得るため、そのまま
  連結しない）

この段（0.76.0）では、まだ道具（list_calendar_events）と
朝のまとめへの組み込みは無い（0.77.0 で行う）。次段はこの
モジュールの `GoogleCalendarClient`（status /
check_connection / access_token / api_get）を土台に使う。

クライアント設定（OAuth クライアント ID / シークレット）は
運用者単位で、Google Cloud で作成したデスクトップ型 OAuth
クライアントの設定ファイルへのパスを環境変数
`KUMA_GOOGLE_CALENDAR_CLIENT_SECRETS_FILE` で渡す。ファイル
自体はリポジトリやデータ書き出しへ入れない。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets as secrets_module
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

# 機能の無効化スイッチ（"0" で無効。それ以外・未設定は有効）
ENABLED_ENV = "KUMA_GOOGLE_CALENDAR"
# OAuth クライアント設定ファイルへのパス（運用者単位の設定）
CLIENT_SECRETS_FILE_ENV = "KUMA_GOOGLE_CALENDAR_CLIENT_SECRETS_FILE"

# 予定の読み取り専用スコープ（書き込み可能なスコープは要求しない）
EVENTS_READONLY_SCOPE = (
    "https://www.googleapis.com/auth/calendar.events.readonly")

DEFAULT_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
CALENDAR_API_BASE_URL = "https://www.googleapis.com/calendar/v3"

# 資格情報ファイルの置き場（利用者の置き場からの相対パス）。
# export.py / restore.py の除外もこの定義を正とする
CREDENTIALS_DIRNAME = "google-calendar"
CREDENTIALS_FILENAME = "credentials.json"
CREDENTIALS_PATH_PARTS = (CREDENTIALS_DIRNAME, CREDENTIALS_FILENAME)
# 資格情報とは別の、利用者ごとの非秘密設定のファイル名（後段で使用）
SETTINGS_FILENAME = "settings.json"

# 認可（ブラウザでの許可）を待つ上限（秒）
AUTH_TIMEOUT_SECONDS = 300.0
# Google API への通信のタイムアウト（秒）
HTTP_TIMEOUT_SECONDS = 15.0
# アクセストークンの期限がこの秒数を切ったら更新する
REFRESH_MARGIN_SECONDS = 60

# リフレッシュトークンの更新をプロセス内で直列化する鍵。同時に
# 更新が走って資格情報ファイルを取り合わないようにするため
_REFRESH_LOCK = threading.Lock()

_CALLBACK_OK_HTML = (
    "<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
    "<title>kuma-agent</title></head><body>"
    "<p>認可を受け付けました。この画面を閉じてください。</p>"
    "</body></html>")
_CALLBACK_NG_HTML = (
    "<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
    "<title>kuma-agent</title></head><body>"
    "<p>認可を受け付けられませんでした。コマンドの表示を確認して"
    "ください。</p></body></html>")


class GoogleCalendarError(Exception):
    """連携の失敗。kind で種類を区別し、message は利用者へ出せる
    安全な文面（資格情報・URL・応答本文を含まない）にする。

    kind: not_configured / config_error / not_authorized /
    invalid / revoked / denied / state_mismatch / timeout /
    network / http / invalid_response
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class ClientSecrets:
    """OAuth クライアント設定（運用者単位）。

    Google Cloud で作成したデスクトップ型 OAuth クライアントの
    設定ファイルから読み込む。client_secret はこの中だけに持ち、
    資格情報ファイルや記録には写さない。
    """

    client_id: str
    client_secret: str
    auth_uri: str = DEFAULT_AUTH_URI
    token_uri: str = DEFAULT_TOKEN_URI


@dataclass
class Credentials:
    """利用者ごとの資格情報（credentials.json の中身）。

    入れるのはリフレッシュトークン・許可されたスコープ・認可した
    日時・クライアント識別子だけ。アクセストークンや認可コード、
    クライアントシークレットは保存しない。
    """

    refresh_token: str
    scope: str = ""
    authorized_at: str = ""
    client_id: str = ""


@dataclass
class CalendarStatus:
    """連携の状態。state は機械可読な区別、message は利用者へ
    出せる安全な文面。

    state: disabled / not_configured / not_authorized / invalid /
    authorized / connected / revoked / failed
    """

    state: str
    message: str


# ------------------------------------------------------- クライアント設定

def load_client_secrets(path: Path) -> ClientSecrets:
    """クライアント設定ファイルを読み込む。

    Google Cloud からダウンロードした JSON（"installed" または
    "web" の下に client_id 等が入る形）を想定する。読めない・
    形式が違う・必須項目が無い場合は GoogleCalendarError
    （kind=config_error）。文面に秘密の値は含めない。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        raise GoogleCalendarError(
            "config_error",
            "クライアント設定ファイルが読み込めませんでした"
            f"（{path}）")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise GoogleCalendarError(
            "config_error",
            "クライアント設定ファイルの形式が正しくありません"
            f"（{path}）")
    section: object = data
    if isinstance(data, dict):
        for key in ("installed", "web"):
            if isinstance(data.get(key), dict):
                section = data[key]
                break
    if not isinstance(section, dict):
        raise GoogleCalendarError(
            "config_error",
            "クライアント設定ファイルの形式が正しくありません"
            f"（{path}）")
    client_id = section.get("client_id")
    client_secret = section.get("client_secret")
    if not isinstance(client_id, str) or not client_id \
            or not isinstance(client_secret, str) or not client_secret:
        raise GoogleCalendarError(
            "config_error",
            "クライアント設定ファイルにクライアント ID / "
            f"シークレットがありません（{path}）")
    auth_uri = section.get("auth_uri")
    token_uri = section.get("token_uri")
    return ClientSecrets(
        client_id=client_id,
        client_secret=client_secret,
        auth_uri=auth_uri if isinstance(auth_uri, str) and auth_uri
        else DEFAULT_AUTH_URI,
        token_uri=token_uri if isinstance(token_uri, str) and token_uri
        else DEFAULT_TOKEN_URI)


# ------------------------------------------------------------ 資格情報

def credentials_dir(user_data_dir: Path) -> Path:
    """利用者の置き場の中の、連携用ディレクトリのパス。"""
    return user_data_dir / CREDENTIALS_DIRNAME


def credentials_path(user_data_dir: Path) -> Path:
    """資格情報ファイルのパス。"""
    return credentials_dir(user_data_dir) / CREDENTIALS_FILENAME


def _chmod_quiet(path: Path, mode: int) -> None:
    """権限を設定する。失敗しても処理は止めない（置き場の
    ファイルシステムによっては権限の概念が無いため）。"""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def save_credentials(user_data_dir: Path,
                     credentials: Credentials) -> Path:
    """資格情報を保存し、そのパスを返す。

    ディレクトリは 0700、ファイルは 0600 を目安にし、一時
    ファイルへ書いてから os.replace で置き換える（途中で止まって
    も壊れた資格情報を残さない）。
    """
    directory = credentials_dir(user_data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _chmod_quiet(directory, 0o700)
    path = credentials_path(user_data_dir)
    payload = {
        "refresh_token": credentials.refresh_token,
        "scope": credentials.scope,
        "authorized_at": credentials.authorized_at,
        "client_id": credentials.client_id,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = directory / (CREDENTIALS_FILENAME + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    _chmod_quiet(path, 0o600)
    return path


def load_credentials(user_data_dir: Path) -> Credentials | None:
    """資格情報を読み込む。ファイルが無ければ None。

    ファイルはあるが読めない・形式が壊れている場合は
    GoogleCalendarError（kind=invalid）。読み込めた場合は、
    緩くなっているファイル権限を 0600 へ直す。
    """
    path = credentials_path(user_data_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise GoogleCalendarError(
            "invalid",
            "資格情報が読み込めませんでした。再度許可してください")
    if not isinstance(data, dict):
        raise GoogleCalendarError(
            "invalid",
            "資格情報が読み込めませんでした。再度許可してください")
    refresh_token = data.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise GoogleCalendarError(
            "invalid",
            "資格情報が読み込めませんでした。再度許可してください")
    _chmod_quiet(path, 0o600)

    def _text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    return Credentials(
        refresh_token=refresh_token,
        scope=_text("scope"),
        authorized_at=_text("authorized_at"),
        client_id=_text("client_id"))


def delete_credentials(user_data_dir: Path) -> bool:
    """資格情報ファイルを削除する。元々無ければ False。"""
    try:
        credentials_path(user_data_dir).unlink()
        return True
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------- HTTP

def _post_form(uri: str, data: dict[str, str],
               transport: httpx.BaseTransport | None) \
        -> tuple[int, dict]:
    """フォーム POST し、(状態コード, JSON 本文) を返す。

    通信失敗は GoogleCalendarError（kind=network）。本文が JSON
    でなければ空辞書にする。例外文面にも戻り値にも、URI や応答
    本文そのものは含めない（呼び出し側は状態コードと error の
    種別だけを見る）。
    """
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS,
                          transport=transport) as client:
            resp = client.post(uri, data=data)
    except Exception as exc:
        # httpx の例外文面には URL が含まれ得るため、そのまま
        # 連結せず一般的な文面へ包む（telegram.py / line_api.py
        # と同じ流儀）。通信失敗として扱う
        raise GoogleCalendarError(
            "network", "Google へ接続できませんでした") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def revoke_refresh_token(client_secrets: ClientSecrets,
                         refresh_token: str,
                         transport: httpx.BaseTransport | None = None) \
        -> bool:
    """Google 側でリフレッシュトークンの取り消しを試みる。

    解除の補助であり、成否は呼び出し側の必須条件にしない
    （失敗しても資格情報ファイルの削除は進める）。
    """
    try:
        status, _ = _post_form(REVOKE_URI, {
            "token": refresh_token,
            "client_id": client_secrets.client_id,
            "client_secret": client_secrets.client_secret,
        }, transport)
    except GoogleCalendarError:
        return False
    return status == 200


# ---------------------------------------------------------------- 認可

def _make_pkce_pair() -> tuple[str, str]:
    """PKCE の (code_verifier, code_challenge) を作る（S256）。"""
    verifier = secrets_module.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest) \
        .rstrip(b"=").decode("ascii")
    return verifier, challenge


def _authorization_url(client_secrets: ClientSecrets,
                       redirect_uri: str, state: str,
                       code_challenge: str) -> str:
    """Google の認可画面の URL を組み立てる。

    スコープは予定の読み取り専用だけ。access_type=offline と
    prompt=consent で、再認可のときもリフレッシュトークンが
    確実に返るようにする。
    """
    params = {
        "client_id": client_secrets.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": EVENTS_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{client_secrets.auth_uri}?{urlencode(params)}"


class _LoopbackReceiver:
    """認可の戻り先（127.0.0.1 の一時ポート）を受け取る受け口。

    受け取ったクエリ（認可コードや state を含む）はメモリ上だけ
    に保つ。リクエスト行には認可コードが含まれるため、
    log_message を無効化してログへ一切出さない。
    """

    def __init__(self) -> None:
        self.query: dict[str, str] = {}
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                params = {key: values[0] for key, values
                          in parse_qs(parsed.query).items()}
                receiver.query = params
                ok = "code" in params
                body = _CALLBACK_OK_HTML if ok else _CALLBACK_NG_HTML
                payload = body.encode("utf-8")
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type",
                                 "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                pass  # 認可コードを含むリクエスト行をログに出さない

        self._server = HTTPServer(("127.0.0.1", 0), Handler)

    @property
    def redirect_uri(self) -> str:
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}/"

    def wait(self, timeout: float) -> dict[str, str]:
        """戻り先へのアクセスを 1 回だけ待ち、クエリを返す。

        時間内にアクセスが無ければ空辞書を返す。
        """
        self._server.timeout = timeout
        try:
            self._server.handle_request()
        finally:
            self._server.server_close()
        return self.query


def authorize(user_data_dir: Path, client_secrets: ClientSecrets, *,
              open_url: Callable[[str], None],
              timeout: float = AUTH_TIMEOUT_SECONDS,
              transport: httpx.BaseTransport | None = None,
              now: datetime | None = None) -> Credentials:
    """ブラウザ認可を回し、資格情報を保存して返す。

    open_url には認可画面の URL が渡る（表示とブラウザ起動は
    呼び出し側の役目）。state の照合と PKCE を満たさない戻りは
    資格情報として保存しない。失敗は GoogleCalendarError で、
    文面に認可コードやトークンは含めない。
    """
    verifier, challenge = _make_pkce_pair()
    state = secrets_module.token_urlsafe(32)
    try:
        receiver = _LoopbackReceiver()
    except OSError:
        raise GoogleCalendarError(
            "network",
            "認可の受け口（このマシンの一時ポート）を"
            "用意できませんでした")
    redirect_uri = receiver.redirect_uri
    url = _authorization_url(client_secrets, redirect_uri,
                             state, challenge)
    open_url(url)
    query = receiver.wait(timeout)
    if not query:
        raise GoogleCalendarError(
            "timeout",
            "認可が時間切れになりました。もう一度実行してください")
    if query.get("state") != state:
        raise GoogleCalendarError(
            "state_mismatch",
            "認可の確認情報が一致しませんでした。"
            "もう一度実行してください")
    code = query.get("code")
    if "error" in query or not code:
        raise GoogleCalendarError(
            "denied", "認可が許可されませんでした")
    status, body = _post_form(client_secrets.token_uri, {
        "code": code,
        "client_id": client_secrets.client_id,
        "client_secret": client_secrets.client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }, transport)
    if status != 200:
        raise GoogleCalendarError(
            "http", "認可の受け付けに失敗しました。"
            "時間をおいてもう一度実行してください")
    refresh_token = body.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise GoogleCalendarError(
            "invalid_response",
            "リフレッシュトークンが返りませんでした。"
            "認可のやり直しが必要です")
    scope = body.get("scope")
    credentials = Credentials(
        refresh_token=refresh_token,
        scope=scope if isinstance(scope, str) else EVENTS_READONLY_SCOPE,
        authorized_at=(now or datetime.now())
        .astimezone().isoformat(timespec="seconds"),
        client_id=client_secrets.client_id)
    save_credentials(user_data_dir, credentials)
    return credentials


# ------------------------------------------------------------ クライアント

class GoogleCalendarClient:
    """1 人の利用者分の Google カレンダー読み取りクライアント。

    アクセストークンはメモリ上だけに保持し、期限が近づいたら
    リフレッシュトークンで更新する。更新で新しいリフレッシュ
    トークンが返った場合だけ、資格情報ファイルを原子的に
    書き換える。HTTP transport は差し替え可能で、テストでは
    実ネットワークへ出ない。
    """

    def __init__(self, user_data_dir: Path,
                 client_secrets: ClientSecrets | None, *,
                 enabled: bool = True,
                 secrets_error: str | None = None,
                 transport: httpx.BaseTransport | None = None,
                 now_fn: Callable[[], datetime] | None = None) -> None:
        self._user_data_dir = user_data_dir
        self._secrets = client_secrets
        self._enabled = enabled
        self._secrets_error = secrets_error
        self._transport = transport
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._access_token: str | None = None
        self._access_expires_at: datetime | None = None

    @property
    def client_secrets(self) -> ClientSecrets | None:
        return self._secrets

    @property
    def secrets_error(self) -> str | None:
        return self._secrets_error

    def status(self) -> CalendarStatus:
        """ネットワークへ出ずに分かる範囲の状態を返す。"""
        if not self._enabled:
            return CalendarStatus(
                "disabled",
                "連携は無効になっています"
                "（環境変数 KUMA_GOOGLE_CALENDAR=0）")
        if self._secrets is None:
            return CalendarStatus(
                "not_configured",
                self._secrets_error
                or "Google カレンダーはまだ設定されていません")
        try:
            credentials = load_credentials(self._user_data_dir)
        except GoogleCalendarError as exc:
            return CalendarStatus("invalid", exc.message)
        if credentials is None:
            return CalendarStatus(
                "not_authorized",
                "Google カレンダーの許可がまだ済んでいません")
        return CalendarStatus(
            "authorized", "Google カレンダーの許可は保存されています")

    def check_connection(self) -> CalendarStatus:
        """資格情報でアクセストークンを取り直し、主カレンダーの
        読み取りができるかまでを確認した状態を返す。

        未設定・未認可など、通信する前に分かる状態はそのまま
        返す。失敗は例外ではなく状態として返す。
        """
        state = self.status()
        if state.state != "authorized":
            return state
        try:
            self.api_get("/calendars/primary",
                         params={"fields": "id"})
        except GoogleCalendarError as exc:
            if exc.kind == "revoked":
                return CalendarStatus(
                    "revoked",
                    "Google カレンダーの許可が無効になっています。"
                    "再度許可してください")
            return CalendarStatus(
                "failed",
                "Google カレンダーへの接続を確認できませんでした")
        return CalendarStatus(
            "connected",
            "接続済みです（主カレンダーを読み取れました）")

    def access_token(self, *, force_refresh: bool = False) -> str:
        """有効なアクセストークンを返す（メモリ上にだけ保持）。

        期限に余裕が無ければ（または force_refresh なら）
        リフレッシュトークンで更新する。更新はプロセス内で
        直列化する。
        """
        if self._secrets is None:
            raise GoogleCalendarError(
                "not_configured",
                "Google カレンダーはまだ設定されていません")
        if not force_refresh and self._cached_token_fresh():
            return self._access_token or ""
        with _REFRESH_LOCK:
            if not force_refresh and self._cached_token_fresh():
                return self._access_token or ""
            self._refresh()
        return self._access_token or ""

    def _cached_token_fresh(self) -> bool:
        if self._access_token is None or self._access_expires_at is None:
            return False
        remaining = (self._access_expires_at
                     - self._now_fn()).total_seconds()
        return remaining > REFRESH_MARGIN_SECONDS

    def _refresh(self) -> None:
        """リフレッシュトークンでアクセストークンを取り直す。

        呼び出し側で _REFRESH_LOCK を保持している前提。
        """
        assert self._secrets is not None
        credentials = load_credentials(self._user_data_dir)
        if credentials is None:
            raise GoogleCalendarError(
                "not_authorized",
                "Google カレンダーの許可がまだ済んでいません")
        status, body = _post_form(self._secrets.token_uri, {
            "client_id": self._secrets.client_id,
            "client_secret": self._secrets.client_secret,
            "refresh_token": credentials.refresh_token,
            "grant_type": "refresh_token",
        }, self._transport)
        if status == 400 and body.get("error") == "invalid_grant":
            raise GoogleCalendarError(
                "revoked",
                "Google カレンダーの許可が無効になっています。"
                "再度許可してください")
        if status != 200:
            raise GoogleCalendarError(
                "http",
                "Google カレンダーへ接続できませんでした")
        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise GoogleCalendarError(
                "invalid_response",
                "Google カレンダーへ接続できませんでした")
        expires_in = body.get("expires_in")
        seconds = expires_in if isinstance(expires_in, int) \
            and expires_in > 0 else 3600
        self._access_token = access_token
        self._access_expires_at = self._now_fn() \
            + timedelta(seconds=seconds)
        new_refresh = body.get("refresh_token")
        if isinstance(new_refresh, str) and new_refresh \
                and new_refresh != credentials.refresh_token:
            scope = body.get("scope")
            save_credentials(self._user_data_dir, Credentials(
                refresh_token=new_refresh,
                scope=scope if isinstance(scope, str)
                else credentials.scope,
                authorized_at=credentials.authorized_at,
                client_id=credentials.client_id))

    def api_get(self, path: str,
                params: dict[str, str] | None = None) -> dict:
        """Calendar API へ GET し、JSON 本文を返す。

        401 のときだけトークン更新を 1 回試して再送する。失敗は
        GoogleCalendarError で、文面に URL や応答本文は含めない。
        次段（0.77.0）の予定一覧の取得はこの土台を使う。
        """
        status, body = self._api_get_once(path, params,
                                          self.access_token())
        if status == 401:
            status, body = self._api_get_once(
                path, params, self.access_token(force_refresh=True))
        if status == 401:
            raise GoogleCalendarError(
                "revoked",
                "Google カレンダーの許可が無効になっています。"
                "再度許可してください")
        if status != 200:
            raise GoogleCalendarError(
                "http",
                "Google カレンダーの予定を取得できませんでした")
        return body

    def _api_get_once(self, path: str,
                      params: dict[str, str] | None,
                      access_token: str) -> tuple[int, dict]:
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS,
                              transport=self._transport) as client:
                resp = client.get(
                    f"{CALENDAR_API_BASE_URL}{path}",
                    params=params,
                    headers={"Authorization":
                             f"Bearer {access_token}"})
        except Exception as exc:
            # 例外文面に URL を含めないため一般的な文面へ包む
            raise GoogleCalendarError(
                "network",
                "Google カレンダーへ接続できませんでした") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code, body if isinstance(body, dict) else {}


def build_client(user_data_dir: Path, *, enabled: bool,
                 client_secrets_file: str,
                 transport: httpx.BaseTransport | None = None) \
        -> GoogleCalendarClient:
    """Config の値からクライアントを組み立てる。

    クライアント設定ファイルが指定されていて読めない場合は、
    その理由を secrets_error として保持し、status() で「未設定
    （設定ファイルの問題）」と区別できるようにする（例外には
    しない）。
    """
    client_secrets: ClientSecrets | None = None
    secrets_error: str | None = None
    if client_secrets_file:
        try:
            client_secrets = load_client_secrets(
                Path(client_secrets_file))
        except GoogleCalendarError as exc:
            secrets_error = exc.message
    return GoogleCalendarClient(
        user_data_dir, client_secrets, enabled=enabled,
        secrets_error=secrets_error, transport=transport)
