"""エントリポイント。"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from .config import Config, mode_label, normalize_mode
from .llm import create_client
from .memory import (cleanup_expired, delete_session_record,
                     list_session_ids, normalize_session_id,
                     normalize_tenant_name, retention_days_for)
from .session import Session


def _run_cleanup(cfg: Config, dry_run: bool = False,
                 tenant: str | None = None,
                 days: int | None = None) -> int:
    """保持期限を過ぎた記録の掃除（運用者向けコマンドの本体）。

    tenant を指定した場合はそのテナントの置き場だけを掃除する。
    days を指定した場合はその場限りの上書きとして、対象範囲すべて
    にその日数を適用する（retention.json と全体既定より優先）。
    """
    if tenant is not None:
        print(f"対象テナント: {normalize_tenant_name(tenant)}")
    if days is not None:
        print(f"保持期限: {days} 日（--days の指定）")
        if days <= 0:
            print("保持期限は無期限のため、掃除しません")
            return 0
    elif tenant is not None:
        resolved = retention_days_for(cfg.data_dir, tenant,
                                      cfg.retention_days)
        print(f"保持期限: {resolved} 日")
        if resolved <= 0:
            print("保持期限は無期限のため、掃除しません")
            return 0
    else:
        print(f"保持期限: 全体の既定 {cfg.retention_days} 日"
              "（テナントごとの指定は retention.json）")
    result = cleanup_expired(cfg.data_dir, cfg.retention_days,
                             dry_run=dry_run, tenant=tenant,
                             override_days=days)
    if dry_run:
        print(f"削除対象: 部屋の記録 {result.sessions_removed} 件、"
              f"完了した用事 {result.tasks_removed} 件"
              "（--dry-run のため削除していません）")
    else:
        print(f"削除しました: 部屋の記録 {result.sessions_removed} 件、"
              f"完了した用事 {result.tasks_removed} 件")
    return 0


def _run_threads(cfg: Config, delete: str | None = None) -> int:
    """スレッド（セッション）の一覧表示と削除（運用者向けコマンドの本体）。

    対象は cfg が指すテナント×ユーザーの置き場。--delete を指定した
    場合は対象名を表示してから、そのスレッドの記録ファイルだけを
    削除する（台帳や長期記憶には触れない）。指定しなければ一覧
    （名前と最終更新）を表示する。
    """
    user_dir = cfg.user_data_dir
    if delete is not None:
        name = normalize_session_id(delete)
        print(f"削除対象のスレッド: {name}")
        if delete_session_record(user_dir, name):
            print(f"削除しました: {name}")
        else:
            print(f"記録が見つかりませんでした: {name}")
        return 0
    names = list_session_ids(user_dir)
    if not names:
        print("スレッドはありません")
        return 0
    for name in names:
        path = user_dir / "conversations" / f"{name}.jsonl"
        try:
            updated = datetime.fromtimestamp(path.stat().st_mtime)
            stamp = updated.strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            stamp = "不明"
        print(f"{name}\t最終更新: {stamp}")
    return 0


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
    chat_parser.add_argument("--mode", default=None,
                             help="モード: ledger（相棒モード）/ wall（スレッドモード）"
                                  "（既定: 環境変数 KUMA_MODE、無ければ ledger）")
    web_parser = sub.add_parser("web", help="Web UI を起動する")
    web_parser.add_argument("--host", default="127.0.0.1",
                            help="バインドするホスト（既定: 127.0.0.1）")
    web_parser.add_argument("--port", type=int, default=8765,
                            help="待ち受けるポート（既定: 8765）")
    web_parser.add_argument("--tenant", default=None,
                            help="起動時のテナント名（既定: 環境変数 KUMA_TENANT、無ければ default）")
    web_parser.add_argument("--user", default=None,
                            help="起動時のユーザー名（既定: 環境変数 KUMA_USER、無ければ default）")
    web_parser.add_argument("--mode", default=None,
                            help="モード: ledger（相棒モード）/ wall（スレッドモード）"
                                 "（既定: 環境変数 KUMA_MODE、無ければ ledger）")
    cleanup_parser = sub.add_parser("cleanup", help="保持期限を過ぎた記録を掃除する")
    cleanup_parser.add_argument("--dry-run", action="store_true",
                                help="削除せず、対象の件数だけを表示する")
    cleanup_parser.add_argument("--tenant", default=None,
                                help="掃除するテナント名（既定: 全テナント）")
    cleanup_parser.add_argument("--days", type=int, default=None,
                                help="保持日数をその場限りで上書きする"
                                     "（retention.json と全体既定より優先、0 で無期限）")
    threads_parser = sub.add_parser("threads", help="スレッド（セッション）の一覧と削除")
    threads_parser.add_argument("--tenant", default=None,
                                help="対象のテナント名（既定: 環境変数 KUMA_TENANT、無ければ default）")
    threads_parser.add_argument("--user", default=None,
                                help="対象のユーザー名（既定: 環境変数 KUMA_USER、無ければ default）")
    threads_parser.add_argument("--delete", default=None, metavar="名前",
                                help="指定した名前のスレッドの記録を削除する")
    args = parser.parse_args(argv)

    cfg = Config()
    if getattr(args, "tenant", None) is not None:  # CLI 引数は環境変数より優先する
        cfg.tenant = args.tenant
    if getattr(args, "user", None) is not None:
        cfg.user = args.user
    if getattr(args, "mode", None) is not None:
        cfg.mode = normalize_mode(args.mode)  # 代入では __post_init__ が効かないため
    if args.command == "cleanup":  # 掃除はモデルも API キーも要らない
        return _run_cleanup(cfg, dry_run=args.dry_run, tenant=args.tenant,
                            days=args.days)
    if args.command == "threads":  # 一覧・削除もモデルも API キーも要らない
        return _run_threads(cfg, delete=args.delete)
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
    if (cfg.mode == "ledger"
            and normalize_session_id(args.session) != session.session_id):
        # 相棒モードでは部屋が固定されることを起動時に知らせる
        print(f"「{mode_label(cfg.mode)}」モードのため"
              f"部屋は {session.session_id} に固定されます")
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
