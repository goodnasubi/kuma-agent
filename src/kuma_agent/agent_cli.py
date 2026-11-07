"""エージェント実行環境（CLI）をバックエンドにするクライアント。

Claude Code や Gemini CLI は、それ自身が道具の実行ループを持つ。
kuma-agent は会話の文脈をプロンプトとして渡し、作業ディレクトリで
CLI をヘッドレス実行して、最終出力を受け取る。kuma-agent 側の道具
（tools 引数）は使わない（CLI 側の道具が働く）。

認証は各 CLI 自身のログインを使うため、API キーは不要。

なお、添付の保存先（作業ディレクトリ配下の .kuma-attachments/）は
作業対象のファイルの一部として扱い、テナントの区切りの対象外とする
（相手のエージェントが作業ディレクトリで読める必要があるため）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .llm import Attachment, Message, quoted_content

ATTACHMENTS_DIR = ".kuma-attachments"


def _render_prompt(system_prompt: str, messages: list[Message],
                   attachment_paths: dict[int, Path] | None = None) -> str:
    lines = [system_prompt, ""]
    for msg in messages:
        if msg.role == "user":
            lines.append(f"利用者: {quoted_content(msg)}")
            for att in msg.attachments:
                # 添付はファイルとして保存し、相手のエージェントに
                # 自分で読んでもらう。保存できなかったものは存在だけ伝える
                path = (attachment_paths or {}).get(id(att))
                if path is not None:
                    lines.append(f"[添付ファイル: {path}]")
                else:
                    lines.append(att.placeholder())
                # 音声の文字起こしがあれば、ファイルに加えて文章も
                # 渡す（相手が聞き取れなくても内容が伝わるよう）
                if att.is_audio and att.transcript:
                    lines.append(f"（文字起こし）{att.transcript}")
        elif msg.role == "assistant" and (msg.content or msg.quote):
            lines.append(f"クマ: {quoted_content(msg)}")
        elif msg.role == "tool":
            lines.append(f"（道具の結果: {msg.content}）")
    lines.append("")
    lines.append("上の文脈を踏まえ、利用者の最新の発話に応答してください。"
                 "作業が必要なら、あなたの道具で実際に実行し、結果を報告してください。")
    return "\n".join(lines)


class CliAgentClient:
    """外部 CLI エージェントを subprocess で実行する共通実装。"""

    COMMAND: str = ""
    PROVIDER: str = ""

    def __init__(self, model: str | None = None, work_dir: Path | None = None,
                 command: str | None = None, timeout: float = 1800.0,
                 extra_args: list[str] | None = None) -> None:
        self.model = model
        self.work_dir = work_dir or Path.cwd()
        self.command = command or self.COMMAND
        self.timeout = timeout
        # 運用者が環境変数 KUMA_CLI_EXTRA_ARGS で渡す追加の引数
        # （全 CLI 共通）。権限モード等の指定は CLI のバージョン
        # 依存のため、kuma-agent は中身の意味を解釈しない
        self.extra_args = list(extra_args or [])

    def build_args(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def _save_attachments(self, messages: list[Message]) -> dict[int, Path]:
        """履歴中の添付を作業ディレクトリ配下に保存し、対応表を返す。

        同名で同サイズのファイルが既にあれば再保存しない。同名で
        中身が違う場合は連番を付けて衝突を避ける。
        """
        saved: dict[int, Path] = {}
        pending = [att for msg in messages for att in msg.attachments]
        if not pending:
            return saved
        dest_dir = self.work_dir / ATTACHMENTS_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        for att in pending:
            path = dest_dir / Path(att.name).name
            suffix = 1
            while path.exists() and path.stat().st_size != len(att.data):
                suffix += 1
                path = dest_dir / f"{path.stem}-{suffix}{path.suffix}"
            if not path.exists():
                path.write_bytes(att.data)
            saved[id(att)] = path
        return saved

    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        prompt = _render_prompt(system_prompt, messages, self._save_attachments(messages))
        try:
            proc = subprocess.run(
                self.build_args(prompt),
                cwd=self.work_dir, capture_output=True, text=True, timeout=self.timeout,
            )
        except FileNotFoundError:
            raise RuntimeError(
                f"{self.command} コマンドが見つかりません。{self.PROVIDER} の CLI を"
                "インストールしてログインしてください"
            ) from None
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[-1000:]
            raise RuntimeError(f"{self.command} が失敗しました (exit {proc.returncode}): {detail}")
        return Message("assistant", proc.stdout.strip())


class ClaudeCodeClient(CliAgentClient):
    """Claude Code CLI（`claude -p` ヘッドレス実行）。"""

    COMMAND = "claude"
    PROVIDER = "claude-code"
    DEFAULT_MODEL = "claude-opus-5-5"  # Claude Code の現在の既定（2026-11 時点）

    def build_args(self, prompt: str) -> list[str]:
        args = [self.command, "-p", prompt]
        model = self.model or self.DEFAULT_MODEL
        if model:
            args += ["--model", model]
        args += self.extra_args
        return args


class GeminiCliClient(CliAgentClient):
    """Gemini CLI（`gemini -p` ヘッドレス実行）。"""

    COMMAND = "gemini"
    PROVIDER = "gemini-cli"
    DEFAULT_MODEL = "gemini-4-argon"  # 最新の旗艦（2026-11 時点）

    def build_args(self, prompt: str) -> list[str]:
        args = [self.command, "-p", prompt]
        model = self.model or self.DEFAULT_MODEL
        if model:
            args += ["--model", model]
        args += self.extra_args
        return args


class CodexClient(CliAgentClient):
    """OpenAI Codex CLI（`codex exec` ヘッドレス実行）。

    exec は既定で読み取り専用サンドボックス＋承認なしのため、
    作業できるよう workspace-write と承認ポリシー never を明示する
    （--full-auto は CLI 0.151 で廃止された）。承認ポリシーを
    never にするのは、ヘッドレス実行では承認する相手がおらず、
    承認待ちにしても止まるだけだからで、権限を絞る手段は
    サンドボックス側に寄せている。サンドボックスは環境変数
    KUMA_CODEX_SANDBOX（既定 workspace-write、read-only を
    選択可）で指定できる。
    """

    COMMAND = "codex"
    PROVIDER = "codex"
    DEFAULT_MODEL = None  # CLI 側の推奨既定（GPT-6 系）に任せる

    def __init__(self, *args, sandbox: str = "workspace-write",
                 **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.sandbox = sandbox or "workspace-write"

    def build_args(self, prompt: str) -> list[str]:
        args = [
            self.command, "exec",
            "--sandbox", self.sandbox,
            "-c", 'approval_policy="never"',
            "--skip-git-repo-check",
        ]
        if self.model:
            args += ["--model", self.model]
        # 追加の引数はプロンプトより前（引数列の末尾）へ足し、
        # プロンプトが最後の引数である形を保つ
        args += self.extra_args
        args.append(prompt)
        return args


CLI_PROVIDERS = ("claude-code", "gemini-cli", "codex")

_CLI_CLIENTS = {
    "claude-code": ClaudeCodeClient,
    "gemini-cli": GeminiCliClient,
    "codex": CodexClient,
}


def create_cli_client(provider: str, model: str | None = None, **kwargs) -> CliAgentClient:
    cls = _CLI_CLIENTS.get(provider)
    if cls is None:
        raise ValueError(f"未知の CLI プロバイダ: {provider}")
    # サンドボックス指定は Codex 専用。他の CLI へは渡さない
    sandbox = kwargs.pop("sandbox", None)
    if cls is CodexClient:
        return cls(model=model, sandbox=sandbox or "workspace-write",
                   **kwargs)
    return cls(model=model, **kwargs)


def cli_command_name(provider: str) -> str | None:
    """プロバイダに対応する CLI コマンド名（CLI 系でなければ None）。"""
    cls = _CLI_CLIENTS.get(provider)
    return cls.COMMAND if cls is not None else None
