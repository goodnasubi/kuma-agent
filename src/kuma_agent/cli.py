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
    sub.add_parser("chat", help="対話を開始する")
    args = parser.parse_args(argv)

    cfg = Config()
    api_key = cfg.resolve_api_key()
    if not cfg.is_cli_provider and not api_key:
        print(f"プロバイダ {cfg.provider} の API キーが未設定です", file=sys.stderr)
        return 2
    session = Session(cfg, create_client(cfg.provider, api_key, cfg.model))
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
