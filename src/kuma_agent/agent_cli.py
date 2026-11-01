"""エージェント実行環境（CLI）をバックエンドにするクライアント。

Claude Code や Gemini CLI は、それ自身が道具の実行ループを持つ。
kuma-agent は会話の文脈をプロンプトとして渡し、作業ディレクトリで
CLI をヘッドレス実行して、最終出力を受け取る。kuma-agent 側の道具
（tools 引数）は使わない（CLI 側の道具が働く）。

認証は各 CLI 自身のログインを使うため、API キーは不要。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .llm import Message


def _render_prompt(system_prompt: str, messages: list[Message]) -> str:
    lines = [system_prompt, ""]
    for msg in messages:
        if msg.role == "user":
            lines.append(f"利用者: {msg.content}")
        elif msg.role == "assistant" and msg.content:
            lines.append(f"クマ: {msg.content}")
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
                 command: str | None = None, timeout: float = 1800.0) -> None:
        self.model = model
        self.work_dir = work_dir or Path.cwd()
        self.command = command or self.COMMAND
        self.timeout = timeout

    def build_args(self, prompt: str) -> list[str]:
        raise NotImplementedError

    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        prompt = _render_prompt(system_prompt, messages)
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
        return args


class CodexClient(CliAgentClient):
    """OpenAI Codex CLI（`codex exec` ヘッドレス実行）。

    exec は既定で読み取り専用サンドボックス＋承認なしのため、
    作業できるよう workspace-write と承認ポリシー never を明示する
    （--full-auto は CLI 0.151 で廃止された）。
    """

    COMMAND = "codex"
    PROVIDER = "codex"
    DEFAULT_MODEL = None  # CLI 側の推奨既定（GPT-6 系）に任せる

    def build_args(self, prompt: str) -> list[str]:
        args = [
            self.command, "exec",
            "--sandbox", "workspace-write",
            "-c", 'approval_policy="never"',
            "--skip-git-repo-check",
        ]
        if self.model:
            args += ["--model", self.model]
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
    return cls(model=model, **kwargs)
