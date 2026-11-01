"""エントリポイント。"""
from __future__ import annotations

import argparse
import sys

from .config import Config
from .llm import create_client
from .session import Session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kuma-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    chat_parser = sub.add_parser("chat", help="対話を開始する")
    chat_parser.add_argument("--tui", action="store_true",
                             help="curses のターミナル UI で対話する")
    chat_parser.add_argument("--session", default="main",
                             help="会話の部屋（セッション）の名前（既定: main）")
    web_parser = sub.add_parser("web", help="Web UI を起動する")
    web_parser.add_argument("--host", default="127.0.0.1",
                            help="バインドするホスト（既定: 127.0.0.1）")
    web_parser.add_argument("--port", type=int, default=8765,
                            help="待ち受けるポート（既定: 8765）")
    args = parser.parse_args(argv)

    cfg = Config()
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
