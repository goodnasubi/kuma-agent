"""LINE 接続（Messaging API の Webhook で会話する）。

Telegram のロングポーリングと違い、LINE は Webhook 方式: LINE の
サーバーが運用者の公開 HTTPS URL（Web UI の POST /line/webhook）
へイベントを POST してくる。このモジュールは、届いたイベントの
処理と送信 API の呼び出しを担い、web.py が受け口を持つ。

- 署名検証: ヘッダー X-Line-Signature は、リクエストボディに
  対する HMAC-SHA256（チャネルシークレット）の Base64。検証は
  hmac.compare_digest で行い、合わないものは処理しない
- 対象は 1 対 1（source.type == "user"）の message イベントだけ。
  グループ・ルームのイベントと follow 等の他種イベントは無視する
- 会話の行き先は Telegram と同じ考え方で、モードで決まる:
  相棒モード（ledger）はそのユーザーの main の会話（Web UI と
  履歴を共有する）、スレッドモード（wall）は line-<user id> の会話

安全側の既定:
- チャネルシークレットとチャネルアクセストークンの両方が未設定
  なら機能自体が無効（受け口は web.py 側で 404 になる）
- 許可リスト（KUMA_LINE_ALLOWED_USERS）が未設定・空のときは会話を
  処理しない。届いたメッセージには user id を知らせる案内だけを
  返し、運用者が id を知って設定できるようにする
- LINE 経路の Session には承認者を付けない。そのため承認が必要な
  道具（run_shell と MCP 道具）は既存の仕組みで安全側に倒れ、
  常に却下される

返信は Reply API（webhook イベントの replyToken を使う。無料で
通数に数えない）を先に試み、失敗したら Push API に切り替える。
テキストは 1 通 5000 字までで、Reply は最大 5 通まで同時に送れる
ため、それを超える分は Push API で送る。Push API はいつでも送れる
が月間の通数に数える。

受信だけでなく、能動発信の配信も担う: 巡回（proactive）が
KUMA_LINE_TENANT / KUMA_LINE_USER の指すユーザーの main の会話へ
投稿した内容は、build_proactive_notifier が作る通知コールバック
経由で許可リストのユーザーへ Push でも送られる
（KUMA_LINE_PROACTIVE=0 で無効化）。

送信 API の呼び出しは LineApi（httpx 実装）に閉じ込め、テストでは
差し替えられるようにしてある。アクセストークンは Authorization
ヘッダーで送るため URL には含まれないが、例外の文面にも出さない
よう、失敗は一般的な文面で扱う。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import threading
from dataclasses import replace
from typing import Callable, Protocol

import httpx

from .config import Config, normalize_mode
from .memory import (
    DEFAULT_SESSION_ID,
    normalize_session_id,
    normalize_tenant_name,
    normalize_user_name,
)
from .session import Session
from .telegram import split_message

API_BASE_URL = "https://api.line.me"
# LINE のテキストメッセージ 1 通の上限（文字数）
MESSAGE_LIMIT = 5000
# Reply API で同時に送れるメッセージの上限（Push も同じ上限で分ける）
REPLY_MAX_MESSAGES = 5
# 許可されていないユーザーへの案内と、テキスト以外への返答
NOT_REGISTERED_NOTICE = (
    "このユーザーはまだ登録されていません。あなたの user id は {user_id} です。"
    "運用者にこの user id を KUMA_LINE_ALLOWED_USERS へ"
    "登録してもらってください。")
TEXT_ONLY_NOTICE = "今はテキストだけ対応しています"
EMPTY_REPLY_NOTICE = "（応答がありませんでした）"
ERROR_NOTICE = "エラーが発生しました。時間をおいてもう一度お試しください。"


def verify_signature(channel_secret: str, body: bytes,
                     signature: str | None) -> bool:
    """Webhook の署名（X-Line-Signature）を検証する。

    署名はチャネルシークレットを鍵にしたボディの HMAC-SHA256 を
    Base64 化したもの。シークレット未設定・署名なし・形式違いは
    すべて不一致として扱う。比較は hmac.compare_digest で行う。
    """
    if not channel_secret or not signature:
        return False
    digest = hmac.new(channel_secret.encode("utf-8"), body,
                      hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    try:
        return hmac.compare_digest(expected, signature)
    except TypeError:
        return False


def parse_webhook_body(body: bytes) -> dict:
    """Webhook の JSON 本文を読む。読めなければ空の辞書。"""
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class LineApi(Protocol):
    """送信 API のうち、このモジュールが使う 2 操作だけの型。

    テストではこの形に合わせた偽物を差し込める。texts は 1 回の
    呼び出しで送るテキストの一覧（最大 REPLY_MAX_MESSAGES 件まで、
    それぞれ MESSAGE_LIMIT 字以内であること）。
    """

    def reply_message(self, reply_token: str, texts: list[str]) -> None:
        ...

    def push_message(self, user_id: str, texts: list[str]) -> None:
        ...


def _text_messages(texts: list[str]) -> list[dict]:
    """テキストの一覧を LINE のメッセージオブジェクトに変える。"""
    return [{"type": "text", "text": text} for text in texts]


class HttpxLineApi:
    """httpx で送信 API を叩く実装。

    認証は Authorization: Bearer <チャネルアクセストークン>。
    トークンをログや例外の文面に出さないよう、失敗は一般的な
    文面の RuntimeError に包む。
    """

    def __init__(self, access_token: str, transport=None,
                 base_url: str = API_BASE_URL) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=30.0,
            transport=transport,
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"})

    def reply_message(self, reply_token: str, texts: list[str]) -> None:
        self._call("/v2/bot/message/reply",
                   {"replyToken": reply_token,
                    "messages": _text_messages(texts)})

    def push_message(self, user_id: str, texts: list[str]) -> None:
        self._call("/v2/bot/message/push",
                   {"to": user_id, "messages": _text_messages(texts)})

    def _call(self, path: str, payload: dict) -> None:
        try:
            resp = self._client.post(path, json=payload)
            resp.raise_for_status()
        except Exception as exc:
            raise RuntimeError("LINE API の呼び出しに失敗しました") from exc


# ---------------------------------------------------------------- 設定まわり

def line_enabled(cfg: Config) -> bool:
    """LINE 接続が有効か（シークレットとトークンの両方が設定済みか）。"""
    return bool(cfg.line_channel_secret and cfg.line_channel_access_token)


def parse_allowed_users(raw: str | None) -> set[str]:
    """許可リスト（カンマ区切りの user id）を文字列の集合として読む。

    空の項目は無視する。未設定・空なら空集合（＝誰も許可しない）。
    """
    return {part.strip() for part in (raw or "").split(",") if part.strip()}


def session_id_for_user(cfg: Config, user_id: str) -> str:
    """ユーザーの会話の行き先（モードで決まる）。

    相棒モードでは main（Session 側でも main に固定されるが、ここでも
    明示する）、スレッドモードでは line-<user id> の専用の会話。
    """
    if normalize_mode(cfg.mode) == "ledger":
        return DEFAULT_SESSION_ID
    return normalize_session_id(f"line-{user_id}")


# ---------------------------------------------------------------- 本体

SessionFactory = Callable[[str], Session]


def _default_session_factory(cfg: Config, model) -> SessionFactory:
    """LINE 用の Session を作る既定の factory。

    対象は Config の line_tenant / line_user が指す組み合わせで、
    会話（セッション ID）ごとに Session を 1 つ作って使い回す。
    承認者は付けない（Session の既定のまま）ため、承認が必要な
    道具は安全側で却下される。記録ファイルは行き先の会話（main
    等）のものをそのまま使うので、履歴は他の経路と共有される。
    """
    scoped = replace(cfg,
                     tenant=normalize_tenant_name(cfg.line_tenant),
                     user=normalize_user_name(cfg.line_user))
    cache: dict[str, Session] = {}

    def factory(session_id: str) -> Session:
        session = cache.get(session_id)
        if session is None:
            session = Session(scoped, model, session_id)
            cache[session_id] = session
        return session

    return factory


class LineBot:
    """Webhook イベントの検証と、届いたメッセージの処理。

    api は LineApi の形（テストでは差し替え可能）。Session は
    session_factory（セッション ID → Session）で引く。無ければ
    model から既定の factory を組み立てる。受け口（web.py）は
    verify() で署名を見て即座に 200 を返し、process_body() を裏側
    のスレッドで呼ぶ（モデルの応答が遅くても Webhook を待たせ
    ないため）。
    """

    def __init__(self, cfg: Config, api: LineApi,
                 model=None,
                 session_factory: SessionFactory | None = None) -> None:
        if session_factory is None:
            if model is None:
                raise ValueError(
                    "session_factory か model のどちらかが必要です")
            session_factory = _default_session_factory(cfg, model)
        self.cfg = cfg
        self.api = api
        self._session_factory = session_factory
        self.allowed_users = parse_allowed_users(cfg.line_allowed_users)
        # Session はスレッド安全ではないため、say の呼び出しは直列化する
        self._lock = threading.Lock()

    def verify(self, body: bytes, signature: str | None) -> bool:
        """この Bot のチャネルシークレットで署名を検証する。"""
        return verify_signature(self.cfg.line_channel_secret, body, signature)

    def process_body(self, body: bytes) -> int:
        """Webhook 本文の events を順に処理し、処理した件数を返す。

        1 件の処理の失敗（送信の失敗など）で残りのイベントや呼び
        出し側のスレッドは落とさない。対象外のイベント（グループ
        宛て・follow 等）は handle_event の中で何もせずに終わる。
        """
        events = parse_webhook_body(body).get("events")
        if not isinstance(events, list):
            return 0
        count = 0
        for event in events:
            try:
                self.handle_event(event)
            except Exception:
                pass  # 1 件の失敗で残りを止めない
            count += 1
        return count

    # ---------------------------------------------------------- 1 件の処理

    def handle_event(self, event: dict) -> None:
        """イベント 1 件を処理する（対象外のイベントは何もしない）。

        対象は 1 対 1（source.type == "user"）の message イベント
        だけ。許可されていないユーザーには登録案内だけを返し、
        会話は処理しない。許可されたユーザーでもテキスト以外
        （画像・音声等）にはテキストのみ対応の返答をする。モデル
        の失敗など処理中の例外はここで握り、利用者へは一般的な
        エラー文を返す。
        """
        if not isinstance(event, dict) or event.get("type") != "message":
            return
        source = event.get("source")
        if not isinstance(source, dict) or source.get("type") != "user":
            return  # グループ・ルーム宛ては未対応
        user_id = source.get("userId")
        if not isinstance(user_id, str) or not user_id:
            return
        message = event.get("message")
        if not isinstance(message, dict):
            return
        reply_token = event.get("replyToken")
        if not isinstance(reply_token, str) or not reply_token:
            reply_token = None
        if user_id not in self.allowed_users:
            self._send(user_id, reply_token,
                       NOT_REGISTERED_NOTICE.format(user_id=user_id))
            return
        text = message.get("text")
        if message.get("type") != "text" \
                or not isinstance(text, str) or not text.strip():
            self._send(user_id, reply_token, TEXT_ONLY_NOTICE)
            return
        try:
            session = self._session_factory(session_id_for_user(
                self.cfg, user_id))
            with self._lock:
                reply = session.say(text)
        except Exception:
            self._send(user_id, reply_token, ERROR_NOTICE)
            return
        self._send(user_id, reply_token,
                   reply if reply and reply.strip() else EMPTY_REPLY_NOTICE)

    def _send(self, user_id: str, reply_token: str | None,
              text: str) -> None:
        """返信を上限に合わせて分割して送る。

        先頭の REPLY_MAX_MESSAGES 通までは Reply API（replyToken、
        無料）で送り、残りは Push API で送る。Reply が失敗したら
        （replyToken の期限切れ等）、全通を Push に切り替える。
        """
        chunks = [chunk for chunk in split_message(text, MESSAGE_LIMIT)
                  if chunk]
        if not chunks:
            return
        rest = chunks
        if reply_token:
            first, rest = chunks[:REPLY_MAX_MESSAGES], \
                chunks[REPLY_MAX_MESSAGES:]
            try:
                self.api.reply_message(reply_token, first)
            except Exception:
                rest = chunks  # Reply がだめなら全通を Push で送る
        self._push_chunks(user_id, rest)

    def _push_chunks(self, user_id: str, chunks: list[str]) -> None:
        """テキストの断片を Push API で送る（1 回最大 5 通）。"""
        for start in range(0, len(chunks), REPLY_MAX_MESSAGES):
            self.api.push_message(
                user_id, chunks[start:start + REPLY_MAX_MESSAGES])


# ---------------------------------------------------------------- 能動発信の配信

def build_proactive_notifier(
        cfg: Config, api: LineApi | None = None,
) -> Callable[[str, str, str], None] | None:
    """能動発信の投稿を LINE へも届ける通知コールバックを作る。

    巡回（proactive）は会話へ投稿するたびに (テナント名, ユーザー名,
    文面) でこのコールバックを呼ぶ。届けるのは Config の line_tenant
    / line_user が指すユーザーの投稿だけで、許可リスト
    （line_allowed_users）の全ユーザーへ Push API で送る。他の
    テナント／ユーザーの投稿は送らない。Push は月間の通数に数え
    られる点に注意（telegram の配信との違い）。

    次のどれかに当てはまれば、通知自体が無いものとして None を返す
    （巡回は従来どおり会話への投稿だけを行う）:
    - チャネルアクセストークンが未設定（LINE 送信ができない）
    - 配信が無効化されている（KUMA_LINE_PROACTIVE=0）
    - 許可リストが未設定・空（届け先が無い）

    api はテスト用の差し替えで、省略時は httpx の実装を作る。送信に
    失敗しても例外は上げない: 会話への投稿は巡回側で先に済んでおり、
    失敗した相手へは標準エラーへ短く出すだけで、再送もしない
    （二重送信を避ける）。
    """
    if not cfg.line_channel_access_token or not cfg.line_proactive:
        return None
    allowed = sorted(parse_allowed_users(cfg.line_allowed_users))
    if not allowed:
        return None
    if api is None:
        api = HttpxLineApi(cfg.line_channel_access_token)
    target = (normalize_tenant_name(cfg.line_tenant),
              normalize_user_name(cfg.line_user))

    def notify(tenant: str, user: str, text: str) -> None:
        if (normalize_tenant_name(tenant),
                normalize_user_name(user)) != target:
            return  # LINE の行き先以外のユーザーの投稿は送らない
        chunks = [chunk for chunk in split_message(text, MESSAGE_LIMIT)
                  if chunk]
        if not chunks:
            return
        for user_id in allowed:
            try:
                for start in range(0, len(chunks), REPLY_MAX_MESSAGES):
                    api.push_message(
                        user_id, chunks[start:start + REPLY_MAX_MESSAGES])
            except Exception:
                # 1 人の失敗で他の相手や巡回は止めない。文面には
                # トークンや本文を含めない
                print("LINE への能動発信の配信に失敗しました"
                      f"（user id: {user_id}）", file=sys.stderr)

    return notify
