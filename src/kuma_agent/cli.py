"""エントリポイント。"""
from __future__ import annotations

import argparse
import sys

from .config import Config
from .github import GitHub
from .llm import ModelClient
from .worker import pick_issue, work_on_issue


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kuma-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Issue を処理する")
    run.add_argument("--repo", required=True, help="owner/name")
    run.add_argument("--once", action="store_true", help="1 件だけ処理して終了")
    run.add_argument("--verify", action="append", default=[], help="検証コマンド（複数可）")
    args = parser.parse_args(argv)

    cfg = Config(repo=args.repo, verify_commands=args.verify)
    if not cfg.github_token:
        print("GITHUB_TOKEN が未設定です", file=sys.stderr)
        return 2

    gh = GitHub(cfg.github_token)
    model = ModelClient(cfg.model_api_key, cfg.model)
    issue = pick_issue(gh.list_open_issues(cfg.repo), cfg)
    if issue is None:
        print("処理できる Issue はありません")
        return 0
    url = work_on_issue(cfg, gh, model, issue)
    print(f"PR: {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
