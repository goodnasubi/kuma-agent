"""エントリポイント。"""
from __future__ import annotations

import argparse
import sys

from .config import Config
from .llm import ModelClient
from .session import Session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kuma-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("chat", help="対話を開始する")
    args = parser.parse_args(argv)

    cfg = Config()
    if not cfg.model_api_key:
        print("META_MODEL_API_KEY が未設定です", file=sys.stderr)
        return 2
    session = Session(cfg, ModelClient(cfg.model_api_key, cfg.model))
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
