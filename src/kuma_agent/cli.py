"""エントリポイント。"""
from __future__ import annotations

import argparse
import sys

from .config import Config
from .llm import create_client
from .session import Session


def _show_status(status: str) -> None:
    """作業中表示を同じ行に上書きする（プレーンな対話ループ用）。

    作業中は「⋯ 文言」を出し、空文字列が来たら行を消して戻す。
    """
    if status:
        print(f"\r⋯ {status}", end="", flush=True)
    else:
        print("\r" + " " * 40 + "\r", end="", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kuma-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    chat_parser = sub.add_parser("chat", help="対話を開始する")
    chat_parser.add_argument("--tui", action="store_true",
                             help="curses のターミナル UI で対話する")
    chat_parser.add_argument("--session", default="main",
                             help="会話の部屋（セッション）の名前（既定: main）")
    chat_parser.add_argument("--tenant", default=None,
                             help="テナント名（既定: 環境変数 KUMA_TENANT、無ければ default）")
    chat_parser.add_argument("--user", default=None,
                             help="ユーザー名（既定: 環境変数 KUMA_USER、無ければ default）")
    web_parser = sub.add_parser("web", help="Web UI を起動する")
    web_parser.add_argument("--host", default="127.0.0.1",
                            help="バインドするホスト（既定: 127.0.0.1）")
    web_parser.add_argument("--port", type=int, default=8765,
                            help="待ち受けるポート（既定: 8765）")
    web_parser.add_argument("--tenant", default=None,
                            help="起動時のテナント名（既定: 環境変数 KUMA_TENANT、無ければ default）")
    web_parser.add_argument("--user", default=None,
                            help="起動時のユーザー名（既定: 環境変数 KUMA_USER、無ければ default）")
    args = parser.parse_args(argv)

    cfg = Config()
    if args.tenant is not None:  # CLI 引数は環境変数より優先する
        cfg.tenant = args.tenant
    if args.user is not None:
        cfg.user = args.user
    api_key = cfg.resolve_api_key()
    if not cfg.is_cli_provider and not api_key:
        print(f"プロバイダ {cfg.provider} の API キーが未設定です", file=sys.stderr)
        return 2
    client = create_client(cfg.provider, api_key, cfg.model)

    if args.command == "web":
        from .web import serve

        serve(Session(cfg, client), host=args.host, port=args.port)
        return 0
    session = Session(cfg, client, session_id=args.session)
    if args.tui:
        from .tui import run_tui

        return run_tui(session, cfg.provider)
    session.on_status = _show_status
    print("クマです。話しかけてください（終了は Ctrl-D）")
    try:
        while True:
            text = input("> ").strip()
            if text:
                print(session.say(text))
    except EOFError:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
