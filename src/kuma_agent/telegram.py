"""Telegram 接続（Bot API のロングポーリングで会話する）。

Telegram の Bot から届いたテキストメッセージを、指定したテナント×
ユーザーの会話（Session）へ渡し、応答を Bot から返信する。Web UI や
CLI と同じ Session／記録の仕組みを使うため、会話の行き先はモードで
決まる:

- 相棒モード（ledger）: そのユーザーの main の会話（Web UI と履歴を
  共有する）
- スレッドモード（wall）: telegram-<chat id> という名前の会話

安全側の既定:
- Bot トークン（KUMA_TELEGRAM_BOT_TOKEN）が未設定なら機能自体が無効
- 許可リスト（KUMA_TELEGRAM_ALLOWED_CHATS）が未設定・空のときは
  会話を処理しない。届いたメッセージには chat id を知らせる案内だけ
  を返し、運用者が id を知って設定できるようにする
- Telegram 経路の Session には承認者を付けない。そのため承認が
  必要な道具（run_shell と MCP 道具）は既存の仕組みで安全側に倒れ、
  常に却下される

処理済み位置（update_id のオフセット）はデータ置き場ルートの
telegram_state.json に保存し、再起動で同じ更新を再処理しない。
ポーリングの失敗（通信エラー等）ではスレッドを落とさず、少し待って
再試行する。

Bot API の呼び出しは TelegramApi（httpx 実装）に閉じ込め、テストでは
差し替えられるようにしてある。トークンは URL に含まれるため、例外の
文面などにも出さないよう、失敗は一般的な文面で扱う。

受信だけでなく、能動発信の配信も担う: 巡回（proactive）が
KUMA_TELEGRAM_TENANT / KUMA_TELEGRAM_USER の指すユーザーの main の
会話へ投稿した内容は、build_proactive_notifier が作る通知コール
バック経由で許可リストのチャットへも送られる（KUMA_TELEGRAM_
PROACTIVE=0 で無効化）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
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

API_BASE_URL = "https://api.telegram.org"
STATE_FILENAME = "telegram_state.json"
# Telegram の sendMessage が受け付ける本文の上限（文字数）
MESSAGE_LIMIT = 4096
# getUpdates のロングポーリングの待ち時間（秒）
POLL_TIMEOUT_SECONDS = 30
# ポーリング失敗時の再試行までの待ち時間（秒）
RETRY_WAIT_SECONDS = 5.0
# 許可されていないチャットへの案内と、テキスト以外への返答
NOT_REGISTERED_NOTICE = (
    "このチャットはまだ登録されていません。chat id は {chat_id} です。"
    "運用者にこの chat id を KUMA_TELEGRAM_ALLOWED_CHATS へ"
    "登録してもらってください。")
TEXT_ONLY_NOTICE = "今はテキストだけ対応しています"
EMPTY_REPLY_NOTICE = "（応答がありませんでした）"
ERROR_NOTICE = "エラーが発生しました。時間をおいてもう一度お試しください。"


class TelegramApi(Protocol):
    """Bot API のうち、このモジュールが使う 2 操作だけの型。

    テストではこの形に合わせた偽物を差し込める。
    """

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        ...

    def send_message(self, chat_id: int, text: str) -> None:
        ...


class HttpxTelegramApi:
    """httpx で Bot API を叩く実装。

    トークンはリクエスト URL（/bot<トークン>/...）に含まれるため、
    ログや例外の文面に URL を出さない。失敗は一般的な文面の
    RuntimeError に包む（httpx の例外文面には URL が含まれうる）。
    """

    def __init__(self, token: str, transport=None,
                 base_url: str = API_BASE_URL) -> None:
        self._client = httpx.Client(
            base_url=f"{base_url}/bot{token}",
            timeout=POLL_TIMEOUT_SECONDS + 35.0,
            transport=transport)

    def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        """未処理の更新をロングポーリングで受け取る。"""
        params: dict[str, object] = {
            "timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        data = self._call("GET", "/getUpdates", params=params)
        result = data.get("result")
        return result if isinstance(result, list) else []

    def send_message(self, chat_id: int, text: str) -> None:
        self._call("POST", "/sendMessage",
                   json={"chat_id": chat_id, "text": text})

    def _call(self, method: str, path: str, **kwargs: object) -> dict:
        try:
            resp = self._client.request(method, path, **kwargs)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise RuntimeError("Telegram API の呼び出しに失敗しました") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError("Telegram API がエラーを返しました")
        return data


# ---------------------------------------------------------------- 設定まわり

def parse_allowed_chats(raw: str | None) -> set[int]:
    """許可リスト（カンマ区切りの chat id）を整数の集合として読む。

    読めない項目は無視する。未設定・空なら空集合（＝誰も許可しない）。
    """
    allowed: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            allowed.add(int(part))
        except ValueError:
            continue
    return allowed


def session_id_for_chat(cfg: Config, chat_id: int) -> str:
    """チャットの会話の行き先（モードで決まる）。

    相棒モードでは main（Session 側でも main に固定されるが、ここでも
    明示する）、スレッドモードでは telegram-<chat id> の専用の会話。
    """
    if normalize_mode(cfg.mode) == "ledger":
        return DEFAULT_SESSION_ID
    return normalize_session_id(f"telegram-{chat_id}")


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """返信文を Telegram の上限に収まる断片に分ける。

    なるべく改行の位置で切り、改行が無い長い文は上限でそのまま切る。
    各断片は limit 以下で、空の断片は作らない（本文が空の場合は
    空文字列 1 件を返すので、呼び出し側で扱う）。
    """
    if limit <= 0:
        limit = MESSAGE_LIMIT
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest[:limit].rfind("\n")
        if cut <= 0:
            chunks.append(rest[:limit])
            rest = rest[limit:]
        else:
            chunks.append(rest[:cut])
            rest = rest[cut + 1:]  # 区切りの改行自体は送らない
    if rest:
        chunks.append(rest)
    return chunks


# ---------------------------------------------------------------- 処理済み位置

def load_offset(data_dir: Path) -> int | None:
    """保存済みのオフセット（次に処理する update_id）を読む。

    ファイルが無い・壊れている場合は None（未保存として扱う）。
    """
    try:
        raw = json.loads(
            (data_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(raw, dict):
        offset = raw.get("offset")
        if isinstance(offset, int) and not isinstance(offset, bool):
            return offset
    return None


def save_offset(data_dir: Path, offset: int) -> None:
    """オフセットをデータ置き場ルートの telegram_state.json に保存する。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"offset": offset}, ensure_ascii=False)
    (data_dir / STATE_FILENAME).write_text(text + "\n", encoding="utf-8")


# ---------------------------------------------------------------- 本体

SessionFactory = Callable[[str], Session]


def _default_session_factory(cfg: Config, model) -> SessionFactory:
    """Telegram 用の Session を作る既定の factory。

    対象は Config の telegram_tenant / telegram_user が指す組み合わせで、
    会話（セッション ID）ごとに Session を 1 つ作って使い回す。承認者は
    付けない（Session の既定のまま）ため、承認が必要な道具は安全側で
    却下される。記録ファイルは行き先の会話（main 等）のものをそのまま
    使うので、履歴は他の経路と共有される。
    """
    scoped = replace(cfg,
                     tenant=normalize_tenant_name(cfg.telegram_tenant),
                     user=normalize_user_name(cfg.telegram_user))
    cache: dict[str, Session] = {}

    def factory(session_id: str) -> Session:
        session = cache.get(session_id)
        if session is None:
            session = Session(scoped, model, session_id)
            cache[session_id] = session
        return session

    return factory


class TelegramPoller:
    """getUpdates のポーリングと、届いたメッセージの処理。

    api は TelegramApi の形（テストでは差し替え可能）。Session は
    session_factory（セッション ID → Session）で引く。無ければ model
    から既定の factory を組み立てる。
    """

    def __init__(self, cfg: Config, api: TelegramApi,
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
        self.allowed_chats = parse_allowed_chats(cfg.telegram_allowed_chats)
        self.offset = load_offset(cfg.data_dir)
        # Session はスレッド安全ではないため、say の呼び出しは直列化する
        self._lock = threading.Lock()

    # ---------------------------------------------------------- 1 件の処理

    def handle_update(self, update: dict) -> None:
        """更新 1 件を処理する（メッセージでない更新は何もしない）。

        許可されていないチャットには登録案内だけを返し、会話は処理
        しない。許可されたチャットでもテキスト以外（写真・ファイル等）
        にはテキストのみ対応の返答をする。モデルの失敗など処理中の
        例外はここで握り、利用者へは一般的なエラー文を返す（ポーリング
        全体を落とさないため）。
        """
        if not isinstance(update, dict):
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        try:
            chat_id = int(chat.get("id"))
        except (TypeError, ValueError):
            return
        if chat_id not in self.allowed_chats:
            self._send(chat_id,
                       NOT_REGISTERED_NOTICE.format(chat_id=chat_id))
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send(chat_id, TEXT_ONLY_NOTICE)
            return
        try:
            session = self._session_factory(session_id_for_chat(
                self.cfg, chat_id))
            with self._lock:
                reply = session.say(text)
        except Exception:
            self._send(chat_id, ERROR_NOTICE)
            return
        self._send(chat_id, reply if reply and reply.strip() else
                   EMPTY_REPLY_NOTICE)

    def _send(self, chat_id: int, text: str) -> None:
        """返信を上限に合わせて分割して送る。"""
        for chunk in split_message(text):
            if chunk:
                self.api.send_message(chat_id, chunk)

    # ---------------------------------------------------------- ポーリング

    def poll_once(self, timeout: int = POLL_TIMEOUT_SECONDS) -> int:
        """保留中の更新を 1 回分取りに行き、順に処理する。

        処理した更新の件数を返す。各更新の処理が済むたびにオフセット
        （その update_id の次）を保存するので、途中で落ちても済んだ分は
        再処理しない。通信の失敗は例外として呼び出し側へ上げる
        （run_forever が受けて再試行する）。
        """
        updates = self.api.get_updates(self.offset, timeout)
        count = 0
        for update in updates:
            if not isinstance(update, dict):
                continue
            self.handle_update(update)
            count += 1
            update_id = update.get("update_id")
            if isinstance(update_id, int) and not isinstance(update_id, bool):
                next_offset = update_id + 1
                if self.offset is None or next_offset > self.offset:
                    self.offset = next_offset
                    save_offset(self.cfg.data_dir, self.offset)
        return count

    def run_forever(self, stop_event: threading.Event | None = None,
                    retry_delay: float = RETRY_WAIT_SECONDS,
                    poll_timeout: int = POLL_TIMEOUT_SECONDS,
                    log: Callable[[str], None] | None = None) -> None:
        """ポーリングを続ける（フォアグラウンド実行と裏側スレッド共用）。

        通信エラー等で 1 回のポーリングが失敗しても終わらず、
        retry_delay 秒待って再試行する。失敗の詳細（URL にトークンが
        含まれうる） はログに出さず、一般的な文面だけを log へ渡す。
        stop_event がセットされたら戻る。
        """
        stopped = stop_event or threading.Event()
        while not stopped.is_set():
            try:
                self.poll_once(timeout=poll_timeout)
            except Exception:
                if log is not None:
                    log("Telegram との通信に失敗しました。再試行します")
                stopped.wait(retry_delay)


def start_polling_thread(cfg: Config, model) -> threading.Thread | None:
    """Web 起動時用: トークンがあれば裏側スレッドでポーリングを始める。

    トークン未設定なら何もせず None を返す（Telegram 機能は無効）。
    スレッドはデーモンで、プロセス終了とともに止まる。ポーリングの
    失敗は run_forever の中で再試行に回るため、Web サーバー自体には
    影響しない。能動発信の巡回（web._start_proactive_loop）と同じ流儀。
    """
    if not cfg.telegram_bot_token:
        return None
    poller = TelegramPoller(
        cfg, HttpxTelegramApi(cfg.telegram_bot_token), model=model)
    thread = threading.Thread(
        target=poller.run_forever, daemon=True, name="kuma-telegram")
    thread.start()
    return thread


def run_polling(cfg: Config, model) -> None:
    """CLI 用: フォアグラウンドでポーリングし続ける（Ctrl-C で終了）。"""
    poller = TelegramPoller(
        cfg, HttpxTelegramApi(cfg.telegram_bot_token), model=model)
    print("Telegram の受信を開始しました（終了は Ctrl-C）")
    try:
        poller.run_forever(log=print)
    except KeyboardInterrupt:
        print()


# ---------------------------------------------------------------- 能動発信の配信

def build_proactive_notifier(
        cfg: Config, api: TelegramApi | None = None,
) -> Callable[[str, str, str], None] | None:
    """能動発信の投稿を Telegram へも届ける通知コールバックを作る。

    巡回（proactive）は会話へ投稿するたびに (テナント名, ユーザー名,
    文面) でこのコールバックを呼ぶ。届けるのは Config の
    telegram_tenant / telegram_user が指すユーザーの投稿だけで、
    許可リスト（telegram_allowed_chats）の全チャットへ、返信と同じ
    分割（split_message）で送る。他のテナント／ユーザーの投稿は
    送らない。

    次のどれかに当てはまれば、通知自体が無いものとして None を返す
    （巡回は従来どおり会話への投稿だけを行う）:
    - Bot トークンが未設定（Telegram 機能が無効）
    - 配信が無効化されている（KUMA_TELEGRAM_PROACTIVE=0）
    - 許可リストが未設定・空（届け先が無い）

    api はテスト用の差し替えで、省略時は httpx の実装を作る。送信に
    失敗しても例外は上げない: 会話への投稿は巡回側で先に済んでおり、
    失敗したチャットへは標準エラーへ短く出すだけで、再送もしない
    （二重送信を避ける）。
    """
    if not cfg.telegram_bot_token or not cfg.telegram_proactive:
        return None
    allowed = sorted(parse_allowed_chats(cfg.telegram_allowed_chats))
    if not allowed:
        return None
    if api is None:
        api = HttpxTelegramApi(cfg.telegram_bot_token)
    target = (normalize_tenant_name(cfg.telegram_tenant),
              normalize_user_name(cfg.telegram_user))

    def notify(tenant: str, user: str, text: str) -> None:
        if (normalize_tenant_name(tenant),
                normalize_user_name(user)) != target:
            return  # Telegram の行き先以外のユーザーの投稿は送らない
        for chat_id in allowed:
            try:
                for chunk in split_message(text):
                    if chunk:
                        api.send_message(chat_id, chunk)
            except Exception:
                # 1 チャットの失敗で他のチャットや巡回は止めない。
                # 文面にはトークンや本文を含めない
                print("Telegram への能動発信の配信に失敗しました"
                      f"（chat id: {chat_id}）", file=sys.stderr)

    return notify
