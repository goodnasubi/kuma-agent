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

0.77.0 では、この土台の上に道具（list_calendar_events）と
朝のまとめへの組み込みを足した。予定の取得・整形はこの
モジュールの `fetch_events` / `format_events` /
`list_events_text` が担い、道具（session.py）と巡回
（proactive.py）の両方が同じ整形を使う。道具の失敗は例外
ではなく、状態に応じた結果文字列（error: …）で返す。

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
import re
import secrets as secrets_module
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

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


# ------------------------------------------------------------ 予定の取得

# 道具（list_calendar_events）の上限（設計 §3.2・§3.4）。
# 期間は最大 31 日、返す件数は既定 20・上限 50、全体の文字数は
# 4,000 字まで。ページ送りは最大 3 ページまで追う
MAX_RANGE_DAYS = 31
DEFAULT_MAX_RESULTS = 20
MAX_RESULTS_LIMIT = 50
MAX_EVENTS_PAGES = 3
_EVENTS_PAGE_SIZE = 250
_EVENTS_TEXT_LIMIT = 4000
_TITLE_LIMIT = 120
_LOCATION_LIMIT = 80

# 道具の結果文字列（設計 §3.5）。予定が無いことと、連携が
# 未設定・失敗したことは別の結果になる。資格情報・URL・
# Google の応答本文は含めない
NOT_CONFIGURED_RESULT = "error: Google カレンダーはまだ設定されていません"
NOT_AUTHORIZED_RESULT = (
    "error: Google カレンダーの許可がまだ済んでいません。"
    "認可の手続きを行ってください")
REVOKED_RESULT = (
    "error: Google カレンダーの許可が無効になっています。"
    "再度許可してください")
FETCH_FAILED_RESULT = "error: Google カレンダーの予定を取得できませんでした"
NO_EVENTS_RESULT = "予定はありませんでした"

# 予定一覧で取る項目。参加者・説明・添付・会議情報は初段では
# 取得しない（設計 §3.3）
_EVENTS_FIELDS = "items(summary,location,start,end,status),nextPageToken"

# 曜日（月曜始まり、date.weekday() の並び）
_WEEKDAY_NAMES = "月火水木金土日"


@dataclass
class CalendarEvent:
    """道具と朝のまとめで共通に使う予定の形。

    start / end は表示に使うローカルタイムゾーンへ変換済みの
    日時。終日予定は all_day が True で、start は開始日の
    0 時、end は終了日（排他的）の 0 時になる。
    """

    title: str
    location: str
    start: datetime
    end: datetime
    all_day: bool = False


def local_tz() -> tzinfo:
    """サーバーのローカルタイムゾーン（設計の判断 5）。

    「今日」の境界と表示の変換はこれでそろえる。取れなければ
    UTC に倒す。
    """
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_event_datetime(raw: dict, tz: tzinfo) -> datetime | None:
    """予定の開始・終了（dateTime 形式）を読む。

    オフセット付きは絶対時刻として解釈する。オフセットが無く
    予定自身のタイムゾーン名（timeZone）がある場合はそれで
    解釈し、どちらも無ければローカルタイムゾーンで解釈する。
    """
    text = raw.get("dateTime")
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        zone: tzinfo = tz
        name = raw.get("timeZone")
        if isinstance(name, str) and name:
            try:
                zone = ZoneInfo(name)
            except Exception:
                zone = tz  # 読めない名前はローカルで解釈する
        parsed = parsed.replace(tzinfo=zone)
    return parsed


def _parse_event(item: object, tz: tzinfo) -> CalendarEvent | None:
    """API の予定 1 件を CalendarEvent へ整える。

    取り消し済みと、開始が読めないものは None（対象外）。
    題名が無いものは「（無題の予定）」にする。説明や参加者は
    そもそも取得していないため、ここにも入らない。
    """
    if not isinstance(item, dict):
        return None
    if item.get("status") == "cancelled":
        return None
    title = item.get("summary")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        title = "（無題の予定）"
    location = item.get("location")
    location = location.strip() if isinstance(location, str) else ""
    start_raw = item.get("start")
    if not isinstance(start_raw, dict):
        return None
    end_raw = item.get("end")
    if not isinstance(end_raw, dict):
        end_raw = {}
    if "dateTime" in start_raw:
        start = _parse_event_datetime(start_raw, tz)
        if start is None:
            return None
        end = _parse_event_datetime(end_raw, tz)
        if end is None or end < start:
            end = start
        return CalendarEvent(
            title=title, location=location,
            start=start.astimezone(tz), end=end.astimezone(tz))
    if "date" in start_raw:
        try:
            start_day = date.fromisoformat(str(start_raw["date"]))
        except ValueError:
            return None
        end_day = start_day + timedelta(days=1)
        if "date" in end_raw:
            try:
                parsed_end = date.fromisoformat(str(end_raw["date"]))
            except ValueError:
                parsed_end = end_day
            if parsed_end > start_day:
                end_day = parsed_end  # 終日予定の終了日は排他的
        return CalendarEvent(
            title=title, location=location,
            start=datetime.combine(start_day, time.min, tzinfo=tz),
            end=datetime.combine(end_day, time.min, tzinfo=tz),
            all_day=True)
    return None


def fetch_events(client: GoogleCalendarClient, start: date,
                 end: date, *, tz: tzinfo | None = None) \
        -> tuple[list[CalendarEvent], bool]:
    """主カレンダーの、start から end（この日を含む）の予定を取る。

    繰り返し予定は singleEvents で個別の予定へ展開し、開始順に
    並べる。ページ送りは最大 MAX_EVENTS_PAGES ページまで追い、
    まだ続きがある場合は第 2 要素を True にする。失敗は
    GoogleCalendarError のまま呼び出し側へ渡す（道具と巡回で
    失敗時の扱いが違うため、ここでは文面にしない）。
    """
    tz = tz or local_tz()
    params: dict[str, str] = {
        "timeMin": datetime.combine(
            start, time.min, tzinfo=tz).isoformat(),
        "timeMax": datetime.combine(
            end + timedelta(days=1), time.min, tzinfo=tz).isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
        "showDeleted": "false",
        "maxResults": str(_EVENTS_PAGE_SIZE),
        "fields": _EVENTS_FIELDS,
    }
    events: list[CalendarEvent] = []
    has_more = False
    for page in range(MAX_EVENTS_PAGES):
        body = client.api_get("/calendars/primary/events", params=params)
        items = body.get("items")
        if isinstance(items, list):
            for item in items:
                event = _parse_event(item, tz)
                if event is not None:
                    events.append(event)
        token = body.get("nextPageToken")
        if not isinstance(token, str) or not token:
            break
        if page == MAX_EVENTS_PAGES - 1:
            has_more = True
            break
        params["pageToken"] = token
    events.sort(key=lambda event: event.start)
    return events, has_more


# ------------------------------------------------------------ 予定の整形

def _format_day(day: date) -> str:
    """「2026-11-07（土）」の形の日付見出し。"""
    return (f"{day.year}-{day.month:02d}-{day.day:02d}"
            f"（{_WEEKDAY_NAMES[day.weekday()]}）")


def _format_short_day(day: date) -> str:
    """「11月7日」の形の短い日付（またぎ表示用）。"""
    return f"{day.month}月{day.day}日"


def _truncate(text: str, limit: int) -> str:
    """上限を超えたら切り詰める（道具の整形の規則）。"""
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def format_time_range(event: CalendarEvent) -> str:
    """予定の時刻の範囲（「09:00–10:30」、日をまたぐ場合は
    日付つき）。朝のまとめの用事への注記でも使う。"""
    if event.start.date() == event.end.date():
        return f"{event.start:%H:%M}–{event.end:%H:%M}"
    return (f"{_format_short_day(event.start.date())} "
            f"{event.start:%H:%M}–"
            f"{_format_short_day(event.end.date())} "
            f"{event.end:%H:%M}")


def _format_event_line(event: CalendarEvent) -> str:
    """予定 1 件の表示行（設計 §3.4 の整形）。"""
    title = _truncate(event.title, _TITLE_LIMIT)
    suffix = ""
    if event.location:
        suffix = f"（場所: {_truncate(event.location, _LOCATION_LIMIT)}）"
    if event.all_day:
        last_day = (event.end - timedelta(days=1)).date()
        if last_day == event.start.date():
            return f"・終日: {title}{suffix}"
        return (f"・終日（{_format_short_day(event.start.date())}〜"
                f"{_format_short_day(last_day)}）: {title}{suffix}")
    return f"・{format_time_range(event)}: {title}{suffix}"


def format_events(events: list[CalendarEvent], start: date, end: date,
                  *, limit: int = MAX_RESULTS_LIMIT,
                  has_more: bool = False) -> str:
    """予定の一覧を道具の返り値と同じプレーンテキストに整える。

    0 件なら「予定はありませんでした」。同じ日の中では終日
    予定を先にまとめ、複数日の結果には日付見出しを付ける。
    表示上限を超えた分は切り捨て、残りが分かる場合だけ
    その旨を末尾に添える（設計 §3.4）。
    """
    if not events:
        return NO_EVENTS_RESULT
    shown = list(events[:limit])
    overflow = len(events) - len(shown)
    header = (f"{_format_day(start)}から{_format_day(end)}までの"
              f"予定は{len(shown)}件です。")
    groups: dict[date, list[CalendarEvent]] = {}
    for event in shown:
        groups.setdefault(event.start.date(), []).append(event)
    single_day = start == end
    lines = [header, ""]
    used = len(header) + 1
    dropped = False
    for day in sorted(groups):
        block: list[str] = []
        if not single_day:
            block.append(f"{_format_day(day)}:")
        day_events = sorted(
            groups[day], key=lambda e: (not e.all_day, e.start))
        block.extend(_format_event_line(e) for e in day_events)
        for line in block:
            if used + len(line) + 1 > _EVENTS_TEXT_LIMIT - 32:
                dropped = True
                break
            lines.append(line)
            used += len(line) + 1
        if dropped:
            break
    if has_more or dropped:
        lines.append("（ほかにも予定があります）")
    elif overflow > 0:
        lines.append(f"（ほか {overflow} 件あります）")
    return "\n".join(lines)


# ------------------------------------------------------- 道具の結果文字列

def _events_error_result(kind: str) -> str:
    """連携の失敗の種類を、道具の結果文字列へ写す（設計 §3.5）。"""
    if kind == "not_configured":
        return NOT_CONFIGURED_RESULT
    if kind == "not_authorized":
        return NOT_AUTHORIZED_RESULT
    if kind in ("revoked", "invalid"):
        # 資格情報の破損も、直し方は再認可なので同じ案内にする
        return REVOKED_RESULT
    return FETCH_FAILED_RESULT


def _parse_date_arg(value: object, default: date) -> date:
    """道具の日付引数（YYYY-MM-DD）を読む。省略時は default。

    形式が違う・日付として成り立たない場合は ValueError。
    """
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise ValueError("日付は文字列で指定してください")
    text = value.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("日付の形式が正しくありません") from None
    if parsed.isoformat() != text:
        raise ValueError("日付の形式が正しくありません")
    return parsed


def list_events_text(client: GoogleCalendarClient, *,
                     start_date: object = None,
                     end_date: object = None,
                     calendar_id: object = None,
                     max_results: object = DEFAULT_MAX_RESULTS,
                     now: datetime | None = None,
                     tz: tzinfo | None = None) -> str:
    """道具 list_calendar_events の実処理。結果文字列を返す。

    失敗は例外ではなく、状態に応じた error: の文字列にする
    （設計 §3.5）。未設定・未認可は通信せずに返す。対象は
    主カレンダー（primary）だけ（複数カレンダーは 0.78.0）。
    """
    state = client.status().state
    if state in ("disabled", "not_configured"):
        return NOT_CONFIGURED_RESULT
    if state == "not_authorized":
        return NOT_AUTHORIZED_RESULT
    if state == "invalid":
        return REVOKED_RESULT
    if calendar_id not in (None, "", "primary"):
        return ("error: 初段では主カレンダー（primary）だけが"
                "対象です")
    now = now or datetime.now()
    try:
        start = _parse_date_arg(start_date, now.date())
        end = _parse_date_arg(end_date, start)
    except ValueError:
        return ("error: 日付の指定が正しくありません"
                "（YYYY-MM-DD で指定してください）")
    if end < start:
        return "error: 終了日が開始日より前になっています"
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        return (f"error: 期間は最大{MAX_RANGE_DAYS}日までです。"
                "範囲を分けて呼んでください")
    try:
        limit = int(max_results)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "error: 件数の指定が正しくありません"
    if limit <= 0:
        return "error: 件数は 1 以上で指定してください"
    limit = min(limit, MAX_RESULTS_LIMIT)
    try:
        events, has_more = fetch_events(client, start, end, tz=tz)
    except GoogleCalendarError as exc:
        return _events_error_result(exc.kind)
    return format_events(events, start, end, limit=limit,
                         has_more=has_more)


def summarize_calendar_result(result: str) -> str:
    """活動の記録用に、道具の結果を件数だけの要約へ置き換える。

    予定の題名・場所は活動の記録へ残さない（設計 §3.6）。
    失敗の文面には個人情報が含まれないためそのまま返し、
    error_kind の分類（activity.classify_tool_error）に使う。
    """
    if result.startswith("error"):
        return result
    if NO_EVENTS_RESULT in result:
        return "0件の予定を取得"
    matched = re.search(r"予定は(\d+)件です", result)
    if matched:
        return f"{matched.group(1)}件の予定を取得"
    return "予定を取得"
