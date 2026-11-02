"""エントリポイント。"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from .config import Config, mode_label, normalize_mode
from .llm import create_client
from .memory import (Memory, cleanup_expired, delete_session_record,
                     format_conversation_hit, list_session_ids,
                     normalize_session_id, normalize_tenant_name,
                     retention_days_for)
from .session import Session
from .tools import summarize_tool_args


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
    削除する（用事の一覧や長期記憶には触れない）。指定しなければ一覧
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


def _run_proactive(cfg: Config, tenant: str | None = None,
                   user: str | None = None) -> int:
    """能動発信のチェックを 1 回だけ実行する（運用者向けコマンドの本体）。

    tenant を指定するとそのテナント（配下の全ユーザー）だけ、user も
    指定するとその 1 ユーザーだけを対象にする。投稿した場合は対象と
    文面を表示し、投稿が無ければその旨を表示する。モデルも API キー
    も要らない（発言文は用事の一覧から組み立てる）。Telegram が設定
    されていれば、Web の巡回と同じく対象ユーザーの投稿は Telegram
    へも届ける。
    """
    from .proactive import run_sweep
    from .telegram import build_proactive_notifier

    # Telegram が設定されていれば、対象ユーザーの投稿は Telegram
    # へも届ける（未設定・無効化・許可リスト空なら通知は付かない）
    posted = run_sweep(cfg, tenant=tenant, user=user,
                       notify=build_proactive_notifier(cfg))
    if not posted:
        print("投稿する内容はありませんでした")
        return 0
    for tenant_name, user_name, text in posted:
        print(f"投稿しました（テナント: {tenant_name}、ユーザー: {user_name}）")
        print(text)
    return 0


def _run_telegram(cfg: Config, client) -> int:
    """Telegram のポーリングを単独実行する（コマンドの本体）。

    Web UI なしで Telegram から会話できるようにする経路。トークンが
    未設定ならその旨を伝えて終わる（終了コード 2）。対象のテナント×
    ユーザーは Config の telegram_tenant / telegram_user
    （環境変数 KUMA_TELEGRAM_TENANT / KUMA_TELEGRAM_USER）で決まる。
    """
    if not cfg.telegram_bot_token:
        print("Telegram の Bot トークンが未設定です"
              "（環境変数 KUMA_TELEGRAM_BOT_TOKEN）", file=sys.stderr)
        return 2
    from .telegram import run_polling

    run_polling(cfg, client)
    return 0


def _run_jobs(cfg: Config) -> int:
    """定期ジョブの一覧表示（運用者向けコマンドの本体）。

    対象は cfg が指すテナント×ユーザーの置き場。各ジョブの名前・
    予定・ON/OFF・前回実行と指示文を表示する。モデルも API キーも
    要らない（読み取り専用）。
    """
    from .jobs import JobBoard, format_last_run, schedule_description

    board = JobBoard(cfg.user_data_dir)
    jobs = board.list()
    if not jobs:
        print("定期ジョブはありません")
        return 0
    for job in jobs:
        state = "ON" if job.enabled else "OFF"
        print(f"#{job.id} {job.title}"
              f"（{schedule_description(job.schedule)}、{state}、"
              f"前回実行: {format_last_run(job.last_run_at)}）")
        print(f"  指示: {job.prompt}")
    return 0


def _run_outputs(cfg: Config) -> int:
    """成果物の一覧表示（運用者向けコマンドの本体）。

    対象は cfg が指すテナント×ユーザーの置き場。各成果物の
    タイトル・ファイル名・サイズ・登録日時を表示する。モデルも
    API キーも要らない（読み取り専用）。
    """
    from .outputs import OutputBoard, format_created_at, format_size

    board = OutputBoard(cfg.user_data_dir)
    outputs = board.list()
    if not outputs:
        print("成果物はありません")
        return 0
    for output in outputs:
        print(f"{output.title}（{output.filename}、"
              f"{format_size(output.size)}、"
              f"{format_created_at(output.created_at)}）")
    return 0


def _run_memory_tidy(cfg: Config, client, dry_run: bool = False) -> int:
    """長期記憶（MEMORY.md）の整理（運用者向けコマンドの本体）。

    対象は cfg が指すテナント×ユーザーの置き場。機械的な重複
    除去→モデル統合の順で整理する（モデルが要るため API キー
    必須のコマンドとして配置し、--dry-run でも同様）。
    --dry-run では本体を変更せず、提案を MEMORY.md.proposed に
    書き出して差分行数だけを表示する。通常実行ではバックアップ
    先と前後の行数を表示する。記憶が無い場合は何もしない
    （エラーにはしない）。
    """
    import difflib

    from .memory_tidy import propose_memory_tidy, tidy_memory_with_model

    memory = Memory(cfg.user_data_dir)
    original = memory.load_long_term()
    if not original.strip():
        print("整理する長期記憶がありません")
        return 0
    if dry_run:
        result = propose_memory_tidy(memory, client)
        if result.reason == "model_failed":
            print("モデルによる統合に失敗したため、提案は作りませんでした"
                  "（MEMORY.md は変更していません）")
            return 0
        diff_lines = sum(
            1 for line in difflib.unified_diff(
                original.splitlines(), result.final_text.splitlines(),
                lineterm="")
            if line[:1] in "+-" and not line.startswith(("+++", "---")))
        print(f"現在の長期記憶: {result.before_lines} 行")
        print(f"提案: {result.after_lines} 行（差分 {diff_lines} 行）")
        print(f"提案を書き出しました: {result.proposed_path}"
              "（MEMORY.md 本体は変更していません）")
        return 0
    result = tidy_memory_with_model(memory, client)
    if result.reason == "model_failed":
        print("モデルによる統合に失敗したため、長期記憶は変更していません")
        return 0
    if not result.applied:
        print(f"変更はありませんでした（{result.before_lines} 行）")
        return 0
    print(f"バックアップ: {result.backup_path}")
    print(f"{result.before_lines} 行 → {result.after_lines} 行に整理しました")
    return 0


def _run_memory_search(cfg: Config, query: str, limit: int = 5) -> int:
    """長期記憶（MEMORY.md）のキーワード検索（コマンドの本体）。

    対象は cfg が指すテナント×ユーザーの置き場。ヒットした断片を
    出どころ（MEMORY.md の行番号）つきで表示する。モデルも
    API キーも要らない。
    """
    hits = Memory(cfg.user_data_dir).search_ranked(query, limit=limit)
    if not hits:
        print("見つかりませんでした")
        return 0
    for hit in hits:
        print(f"（MEMORY.md の {hit.line} 行目）{hit.text}")
    return 0


def _run_search_history(cfg: Config, query: str, limit: int = 5) -> int:
    """過去の会話記録の横断検索（コマンドの本体）。

    対象は cfg が指すテナント×ユーザーの置き場にある全セッション
    の会話記録。ヒットした発話をセッション名・日時・話者つきで
    表示する。モデルも API キーも要らない。
    """
    hits = Memory(cfg.user_data_dir).search_conversations(query,
                                                          limit=limit)
    if not hits:
        print("見つかりませんでした")
        return 0
    for hit in hits:
        print(format_conversation_hit(hit))
    return 0


def _show_status(status: str) -> None:
    """作業中表示を同じ行に上書きする（プレーンな対話ループ用）。

    作業中は「⋯ 文言」を出し、空文字列が来たら行を消して戻す。
    """
    if status:
        print(f"\r⋯ {status}", end="", flush=True)
    else:
        print("\r" + " " * 40 + "\r", end="", flush=True)


def _cli_approver(name: str, args: dict) -> bool:
    """プレーンな対話ループ用の承認者（標準入力で y/N を尋ねる）。

    対象の道具と引数の要約を示し、「y」の入力があったときだけ
    許可する。Enter のみ・それ以外の入力・入力の終わり（EOF）は
    すべて却下として扱う。端末の前に人がいてプロンプト自体が
    待ちになるため、この経路に時間切れは設けない（Web / TUI の
    承認待ちには Config.approval_timeout_seconds が効く）。
    """
    summary = summarize_tool_args(name, args)
    print()  # 作業中表示の同じ行上書きを区切ってから尋ねる
    print(f"承認が必要です: {name}" + (f"（{summary}）" if summary else ""))
    try:
        answer = input("許可しますか？ [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


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
    proactive_parser = sub.add_parser(
        "proactive", help="能動発信のチェックを 1 回実行する")
    proactive_parser.add_argument("--tenant", default=None,
                                  help="対象のテナント名（既定: 全テナント）")
    proactive_parser.add_argument("--user", default=None,
                                  help="対象のユーザー名（既定: 対象テナントの全ユーザー）")
    memory_search_parser = sub.add_parser(
        "memory-search", help="長期記憶（MEMORY.md）をキーワードで検索する")
    memory_search_parser.add_argument(
        "query", help="検索クエリ（空白や句読点で区切って複数キーワード）")
    memory_search_parser.add_argument("--tenant", default=None,
                                      help="対象のテナント名（既定: 環境変数 KUMA_TENANT、無ければ default）")
    memory_search_parser.add_argument("--user", default=None,
                                      help="対象のユーザー名（既定: 環境変数 KUMA_USER、無ければ default）")
    memory_search_parser.add_argument("--limit", type=int, default=5,
                                      help="表示する最大件数（既定: 5）")
    search_history_parser = sub.add_parser(
        "search-history", help="過去の会話記録をキーワードで横断検索する")
    search_history_parser.add_argument(
        "query", help="検索クエリ（空白や句読点で区切って複数キーワード）")
    search_history_parser.add_argument("--tenant", default=None,
                                       help="対象のテナント名（既定: 環境変数 KUMA_TENANT、無ければ default）")
    search_history_parser.add_argument("--user", default=None,
                                       help="対象のユーザー名（既定: 環境変数 KUMA_USER、無ければ default）")
    search_history_parser.add_argument("--limit", type=int, default=5,
                                       help="表示する最大件数（既定: 5）")
    memory_tidy_parser = sub.add_parser(
        "memory-tidy", help="長期記憶（MEMORY.md）を整理する")
    memory_tidy_parser.add_argument("--tenant", default=None,
                                    help="対象のテナント名（既定: 環境変数 KUMA_TENANT、無ければ default）")
    memory_tidy_parser.add_argument("--user", default=None,
                                    help="対象のユーザー名（既定: 環境変数 KUMA_USER、無ければ default）")
    memory_tidy_parser.add_argument("--dry-run", action="store_true",
                                    help="本体を変更せず、提案を MEMORY.md.proposed に書き出す")
    jobs_parser = sub.add_parser("jobs", help="定期ジョブの一覧を表示する")
    jobs_parser.add_argument("--tenant", default=None,
                             help="対象のテナント名（既定: 環境変数 KUMA_TENANT、無ければ default）")
    jobs_parser.add_argument("--user", default=None,
                             help="対象のユーザー名（既定: 環境変数 KUMA_USER、無ければ default）")
    outputs_parser = sub.add_parser("outputs", help="成果物の一覧を表示する")
    outputs_parser.add_argument("--tenant", default=None,
                                help="対象のテナント名（既定: 環境変数 KUMA_TENANT、無ければ default）")
    outputs_parser.add_argument("--user", default=None,
                                help="対象のユーザー名（既定: 環境変数 KUMA_USER、無ければ default）")
    sub.add_parser(
        "telegram",
        help="Telegram からのメッセージを受けて会話する（単独実行）")
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
    if args.command == "proactive":  # 能動発信もモデルも API キーも要らない
        return _run_proactive(cfg, tenant=args.tenant, user=args.user)
    if args.command == "memory-search":  # 記憶の検索もモデルも API キーも要らない
        return _run_memory_search(cfg, args.query, limit=args.limit)
    if args.command == "search-history":  # 会話の検索もモデルも API キーも要らない
        return _run_search_history(cfg, args.query, limit=args.limit)
    if args.command == "jobs":  # 一覧表示もモデルも API キーも要らない
        return _run_jobs(cfg)
    if args.command == "outputs":  # 一覧表示もモデルも API キーも要らない
        return _run_outputs(cfg)
    api_key = cfg.resolve_api_key()
    if not cfg.is_cli_provider and not api_key:
        print(f"プロバイダ {cfg.provider} の API キーが未設定です", file=sys.stderr)
        return 2
    client = create_client(cfg.provider, api_key, cfg.model)

    if args.command == "memory-tidy":  # モデル統合が要るため API キー必須
        return _run_memory_tidy(cfg, client, dry_run=args.dry_run)
    if args.command == "web":
        from .web import serve

        serve(Session(cfg, client), host=args.host, port=args.port)
        return 0
    if args.command == "telegram":
        return _run_telegram(cfg, client)
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
    session.approver = _cli_approver
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
