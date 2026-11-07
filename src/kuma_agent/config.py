"""設定。環境変数と CLI 引数から組み立てる。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .memory import tenant_dir, user_dir


def _env_int(name: str, default: int) -> int:
    """環境変数を整数として読む。不正な値なら既定値にする。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# 文脈の分け方のモード（詳細は docs/DESIGN.md の「モード」節）:
# - ledger（相棒モード、既定）: 部屋は実質 1 つ（main）に固定し、
#   抱えている用事は用事の一覧で分けて覚える
# - wall（スレッドモード）: 会話を部屋（スレッド）ごとに分ける。用事の一覧は無効
MODES = ("wall", "ledger")
DEFAULT_MODE = "ledger"


def normalize_mode(raw: str | None) -> str:
    """モード名を正規化する。

    旧値 both は後方互換のため ledger に読み替え、それ以外の不正な
    値も既定（ledger）に倒す。
    """
    mode = (raw or "").strip().lower()
    if mode == "both":
        return "ledger"
    return mode if mode in MODES else DEFAULT_MODE


# モードのユーザー向け表示名。内部の識別子（wall / ledger）は
# 設定値としてだけ使い、画面や文面ではこちらの名前で出す。
# 対応付けはこの辞書に集約する
MODE_LABELS: dict[str, str] = {
    "wall": "スレッドモード",
    "ledger": "相棒モード",
}


def mode_label(mode: str | None) -> str:
    """モードの表示名を返す。不正な値は既定モードの表示名に倒す。"""
    return MODE_LABELS[normalize_mode(mode)]


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("KUMA_DATA_DIR", "data")))
    tenant: str = field(default_factory=lambda: os.environ.get("KUMA_TENANT", "default"))
    user: str = field(default_factory=lambda: os.environ.get("KUMA_USER", "default"))
    # 文脈の分け方のモード（wall / ledger）。不正な値と旧値 both は
    # __post_init__ で既定の ledger に倒す
    mode: str = field(default_factory=lambda: os.environ.get("KUMA_MODE", DEFAULT_MODE))
    provider: str = field(default_factory=lambda: os.environ.get("KUMA_LLM_PROVIDER", "muse"))
    model: str | None = field(default_factory=lambda: os.environ.get("KUMA_LLM_MODEL") or None)
    model_api_key: str = ""

    def __post_init__(self) -> None:
        # どの経路で作られてもモードは正規化済みの値にそろえる
        self.mode = normalize_mode(self.mode)
        # 承認待ちの上限が 0 以下なら既定値として扱う（MCP の応答待ちと
        # 同じ流儀）。フィールドの既定値が組み立てられた後に直す
        if self.approval_timeout_seconds <= 0:
            self.approval_timeout_seconds = 300
        # 委任の時間切れも同じ流儀で、0 以下は既定値として扱う
        if self.delegate_timeout_seconds <= 0:
            self.delegate_timeout_seconds = 600

    @property
    def is_cli_provider(self) -> bool:
        return self.provider in ("claude-code", "gemini-cli", "codex",
                                 "antigravity-cli")

    def resolve_api_key(self) -> str:
        """プロバイダに応じた環境変数から API キーを引く。"""
        if self.model_api_key:
            return self.model_api_key
        env_names = {
            "muse": ("META_MODEL_API_KEY", "MODEL_API_KEY"),
            "claude": ("ANTHROPIC_API_KEY",),
            "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        }.get(self.provider, ())
        for name in env_names:
            if os.environ.get(name):
                return os.environ[name]
        return ""
    max_tool_rounds: int = 16
    # 会話の圧縮（コンパクション）: user/assistant の発話数が compact_after を
    # 超えたら古い発話を要約に置き換える。0 で無効。直近 compact_keep 件は残す
    compact_after: int = field(
        default_factory=lambda: _env_int("KUMA_COMPACT_AFTER_MESSAGES", 40))
    compact_keep: int = field(
        default_factory=lambda: _env_int("KUMA_COMPACT_KEEP_MESSAGES", 10))
    # 記録の保持期限（日数）。部屋の記録と完了した用事は、最終更新から
    # この日数を過ぎたら自動で掃除される（cleanup_expired）。0 で無期限
    retention_days: int = field(
        default_factory=lambda: _env_int("KUMA_RETENTION_DAYS", 90))
    # 活動の記録（activity.jsonl）の保持期限（日数）。道具の呼び出し・
    # 承認・外部送信の記録は、行ごとの時刻がこの日数を過ぎたら能動
    # 発信の巡回で古い行から落とされる（activity.auto_cleanup_activity）。
    # 0 で無期限
    activity_retention_days: int = field(
        default_factory=lambda: _env_int("KUMA_ACTIVITY_RETENTION_DAYS", 90))
    # 能動発信の巡回間隔（秒）。Web サーバーの裏側で定期的に全テナント×
    # 全ユーザーを巡回し、言うべきことがあるユーザーにだけ発言する
    # （proactive）。0 以下で巡回しない
    proactive_interval_seconds: int = field(
        default_factory=lambda: _env_int("KUMA_PROACTIVE_INTERVAL_SECONDS", 300))
    # 朝のまとめ（能動発信の日報の拡張）の時刻。"HH:MM" 形式の
    # 文字列で、この時刻を過ぎた最初の巡回で、子エージェントが
    # 予定と用事をまとめた知らせを投稿する（日報の代わり）。
    # "off" で無効（従来の日報になる）。値の解釈（不正な値は
    # 既定の 07:30 に倒す）は proactive.py 側で行う
    briefing_time: str = field(
        default_factory=lambda: os.environ.get("KUMA_BRIEFING_TIME", "07:30"))
    # 通知の取捨（見張りの届け方の振り分け、0.84.0）の有効化:
    # "0" の間は、見張りの delivery が digest でも即時に倒す
    # （新しい振り分けの切り戻し）。ただし既に置き場
    # （notification_queue.jsonl）にある未配信分は、無効化中も
    # 朝の知らせの材料として読み続けて届ける（止める操作で
    # 未配信分を消さない）。合成は notification_triage.py の
    # 判定と proactive.py が担う
    notification_triage_enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "KUMA_NOTIFICATION_TRIAGE", "") != "0")
    # MCP サーバーへの要求の応答待ち上限（秒）。サーバーがこの時間に
    # 応答しなければ、その道具の呼び出しは失敗として扱う（接続は
    # 捨てて次回に再起動を試みる）。0 以下は既定値として扱う
    mcp_timeout_seconds: int = field(
        default_factory=lambda: _env_int("KUMA_MCP_TIMEOUT_SECONDS", 60))
    # 危険な道具（run_shell と MCP 道具）の承認待ちの上限（秒）。
    # 承認側がこの時間に決めなければ却下として扱う。0 以下は
    # __post_init__ で既定値（300）として扱う
    approval_timeout_seconds: int = field(
        default_factory=lambda: _env_int("KUMA_APPROVAL_TIMEOUT_SECONDS", 300))
    # 長期記憶（MEMORY.md）の全文注入の上限（文字数）。記憶が
    # これ以下なら従来どおり全文をシステムプロンプトへ注入し、
    # 超えたら新しい方の項目だけを注入して残りは search_memory
    # 道具で検索させる（memory.memory_inject_text）
    memory_full_inject_chars: int = field(
        default_factory=lambda: _env_int("KUMA_MEMORY_FULL_INJECT_CHARS", 6000))
    # 委任（delegate 道具）で作る子エージェントのモデル名。未指定
    # （None）なら親と同じモデルを使う。プロバイダは常に親と同じ
    delegate_model: str | None = field(
        default_factory=lambda: os.environ.get("KUMA_DELEGATE_MODEL") or None)
    # 委任した仕事の時間切れ（秒）。子がこの時間に最終応答を返さ
    # なければ、親には時間切れを道具の失敗として返す。0 以下は
    # __post_init__ で既定値（600）として扱う
    delegate_timeout_seconds: int = field(
        default_factory=lambda: _env_int("KUMA_DELEGATE_TIMEOUT_SECONDS", 600))
    # Telegram 接続（telegram.py）。Bot トークンが未設定なら機能は
    # 無効。telegram_tenant / telegram_user は、届いたメッセージを
    # 処理するテナントとユーザー（CLI / Web の起動時指定とは別の、
    # Telegram 経路専用の行き先）。telegram_allowed_chats は会話を
    # 許可する chat id のカンマ区切りで、未設定・空のときは会話を
    # 処理しない（chat id を知らせる案内だけを返す）
    telegram_bot_token: str = field(
        default_factory=lambda: os.environ.get("KUMA_TELEGRAM_BOT_TOKEN", ""))
    telegram_tenant: str = field(
        default_factory=lambda: os.environ.get("KUMA_TELEGRAM_TENANT", "default"))
    telegram_user: str = field(
        default_factory=lambda: os.environ.get("KUMA_TELEGRAM_USER", "default"))
    telegram_allowed_chats: str = field(
        default_factory=lambda: os.environ.get("KUMA_TELEGRAM_ALLOWED_CHATS", ""))
    # 能動発信の Telegram 配信（telegram.build_proactive_notifier）。
    # 巡回が telegram_tenant / telegram_user の main の会話へ投稿した
    # 内容（日報・リマインダー・定期ジョブの結果）を、許可リストの
    # チャットへも送るかどうか。"0" で無効化し、それ以外（未設定を
    # 含む）は有効。トークン未設定・許可リスト空なら有効でも送らない
    telegram_proactive: bool = field(
        default_factory=lambda: os.environ.get("KUMA_TELEGRAM_PROACTIVE", "") != "0")
    # LINE 接続（line_api.py）。Telegram と違い Webhook 方式で、受け口は
    # Web UI の POST /line/webhook。チャネルシークレットとチャネル
    # アクセストークンの両方が設定されているときだけ受け口が有効に
    # なる。line_tenant / line_user は、届いたメッセージを処理する
    # テナントとユーザー（LINE 経路専用の行き先）。line_allowed_users
    # は会話を許可する user id のカンマ区切りで、未設定・空のときは
    # 会話を処理しない（user id を知らせる案内だけを返す）
    line_channel_secret: str = field(
        default_factory=lambda: os.environ.get("KUMA_LINE_CHANNEL_SECRET", ""))
    line_channel_access_token: str = field(
        default_factory=lambda: os.environ.get(
            "KUMA_LINE_CHANNEL_ACCESS_TOKEN", ""))
    line_tenant: str = field(
        default_factory=lambda: os.environ.get("KUMA_LINE_TENANT", "default"))
    line_user: str = field(
        default_factory=lambda: os.environ.get("KUMA_LINE_USER", "default"))
    line_allowed_users: str = field(
        default_factory=lambda: os.environ.get("KUMA_LINE_ALLOWED_USERS", ""))
    # 能動発信の LINE 配信（line_api.build_proactive_notifier）。巡回が
    # line_tenant / line_user の main の会話へ投稿した内容を、許可
    # リストのユーザーへ Push API でも送るかどうか。Push は月間の
    # 通数に数えるため、止めたい場合は "0" で無効化する（それ以外、
    # 未設定を含む、は有効）
    line_proactive: bool = field(
        default_factory=lambda: os.environ.get("KUMA_LINE_PROACTIVE", "") != "0")
    # Web UI のログイン（アクセス制御）。パスワードが未設定（空）なら
    # ログインは無効で、従来どおり認証なしで開く。設定したときだけ
    # Web UI 全体がパスワードで守られる（CLI / TUI は対象外）。
    # web_secure_cookie は Cookie に Secure 属性を付けるかどうかで、
    # HTTPS で公開する場合に運用者が 1 にする（既定は付けない）
    web_password: str = field(
        default_factory=lambda: os.environ.get("KUMA_WEB_PASSWORD", ""))
    web_secure_cookie: bool = field(
        default_factory=lambda: os.environ.get("KUMA_WEB_SECURE_COOKIE", "") == "1")
    # 公開先の固定（公開前チェックの指摘3・選択肢C）: テナントと
    # ユーザーを環境変数で 1 組に固定する。設定した軸だけを Web の
    # 解決で照合し、食い違うテナント／ユーザーへの API アクセスを
    # 403 で返す（TenantStore の fixed_tenant / fixed_user と
    # Handler の解決ヘルパーが担う）。どちらも未設定（空）なら
    # 固定なしで、従来どおり全テナント・全ユーザーへ切り替えられる
    web_tenant: str = field(
        default_factory=lambda: os.environ.get("KUMA_WEB_TENANT", ""))
    web_user: str = field(
        default_factory=lambda: os.environ.get("KUMA_WEB_USER", ""))
    # 承認の運用者ロック: "1" の間は、テナント設定（settings.json の
    # approvals_enabled）で承認を OFF にできず、常に承認を挟む。
    # 公開時に画面から承認を切られる事故を防ぐための歯止めで、
    # 合成は Session の承認の配線と Web のテナント設定 API が担う
    # （settings.py 自体は変えない）
    require_approvals: bool = field(
        default_factory=lambda: os.environ.get(
            "KUMA_REQUIRE_APPROVALS", "") == "1")
    # テナント別頭脳で CLI 系を選べるようにする運用者スイッチ:
    # "1" のときだけ、テナントの頭脳の指定で CLI 系（claude-code /
    # gemini-cli / codex / antigravity-cli）も受け付ける。CLI 系では道具の実行が
    # 外部 CLI の側で行われ、kuma-agent 側の承認確認や道具の制限
    # は効かないため、画面操作だけで「承認の効かないテナント」を
    # 作れないよう、解禁は運用者の明示に限る（公開前チェック
    # 指摘1との連動。合成は brain.py の解決と Web のテナント
    # 設定 API が担う）
    allow_tenant_brain_cli: bool = field(
        default_factory=lambda: os.environ.get(
            "KUMA_ALLOW_TENANT_BRAIN_CLI", "") == "1")
    # shell 道具（run_shell）の有効化: "0" のとき道具自体を登録
    # しない（モデルの選択肢にも出ない）。既定（未設定を含む）は
    # 有効＝従来動作
    shell_enabled: bool = field(
        default_factory=lambda: os.environ.get("KUMA_SHELL_ENABLED", "") != "0")
    # CLI 系の頭脳の設定固定（公開前チェックの指摘1 (b)）: Codex の
    # サンドボックス段階。既定は従来どおり workspace-write で、
    # 絞りたいときは read-only を指定する。空なら既定に倒す
    codex_sandbox: str = field(
        default_factory=lambda: os.environ.get(
            "KUMA_CODEX_SANDBOX", "").strip() or "workspace-write")
    # CLI 系の頭脳へ渡す追加の引数（空白区切り、全 CLI 共通）。
    # 権限モード等の指定は導入先の CLI のバージョンに依存する
    # ため、kuma-agent が意味を知っている引数は勝手に付けず、
    # 運用者が CLI のヘルプで確認して入れる逃げ道として用意する
    cli_extra_args: str = field(
        default_factory=lambda: os.environ.get("KUMA_CLI_EXTRA_ARGS", ""))
    # Google カレンダーの読み取り連携（google_calendar.py）。
    # "0" のとき連携全体を無効化する（それ以外、未設定を含む、
    # は有効。ただしクライアント設定が無ければ未設定として扱う）
    google_calendar_enabled: bool = field(
        default_factory=lambda: os.environ.get(
            "KUMA_GOOGLE_CALENDAR", "") != "0")
    # OAuth クライアント設定ファイルへのパス（運用者単位の設定。
    # Google Cloud で作成したデスクトップ型 OAuth クライアントの
    # 設定ファイルを指す。ファイル自体はリポジトリやデータ
    # 書き出しへ入れず、アプリのプロセスだけが読める場所へ置く）。
    # 利用者ごとのリフレッシュトークンは、ここではなく利用者の
    # 置き場の google-calendar/credentials.json に分かれる
    google_calendar_client_secrets_file: str = field(
        default_factory=lambda: os.environ.get(
            "KUMA_GOOGLE_CALENDAR_CLIENT_SECRETS_FILE", ""))

    def cli_client_kwargs(self) -> dict:
        """CLI 系の頭脳を作るときの追加引数（API 系では空）。

        Codex のサンドボックスと全 CLI 共通の追加引数を、クライ
        アント生成（llm.create_client）へそのまま渡せる形で返す。
        API 系の頭脳では余分な引数を渡さないよう空にする。
        """
        if not self.is_cli_provider:
            return {}
        return {"sandbox": self.codex_sandbox,
                "extra_args": self.cli_extra_args.split()}

    @property
    def tenant_data_dir(self) -> Path:
        """このテナントの実効データ置き場。

        既定テナント（"default"）は後方互換のため data_dir をそのまま
        返し、それ以外は data_dir / "tenants" / <テナント名> を返す。
        人格・長期記憶・部屋・用事の記録はすべてこの置き場の下に分かれる。
        """
        return tenant_dir(self.data_dir, self.tenant)

    @property
    def user_data_dir(self) -> Path:
        """このユーザーの実効データ置き場。

        既定ユーザー（"default"）は後方互換のためテナントの置き場を
        そのまま返し、それ以外は
        <テナント置き場> / "users" / <ユーザー名> を返す。
        人格・長期記憶・部屋・用事の記録はすべてこの置き場の下に分かれる。
        """
        return user_dir(self.tenant_data_dir, self.user)

    @property
    def memory_file(self) -> Path:
        return self.user_data_dir / "MEMORY.md"

    @property
    def conversations_dir(self) -> Path:
        return self.user_data_dir / "conversations"
