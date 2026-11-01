import stat
from pathlib import Path

import pytest

from kuma_agent.agent_cli import ClaudeCodeClient, GeminiCliClient
from kuma_agent.llm import Message, create_client


def _fake_cli(tmp_path: Path, name: str, output: str) -> Path:
    script = tmp_path / name
    script.write_text(
        "#!/bin/sh\n"
        f"echo \"{output}\"\n"
        "echo \"$@\" > \"$(dirname \"$0\")/args.txt\"\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_claude_code_backend(tmp_path: Path):
    exe = _fake_cli(tmp_path, "fake-claude", "やりました")
    client = ClaudeCodeClient(command=str(exe), work_dir=tmp_path)
    reply = client.chat("人格テキスト", [Message("user", "やって")], tools=[{"name": "now"}])
    assert reply.content == "やりました"
    assert reply.tool_calls == []
    args = (tmp_path / "args.txt").read_text(encoding="utf-8")
    assert "-p" in args and "人格テキスト" in args and "やって" in args


def test_gemini_cli_backend_with_model(tmp_path: Path):
    exe = _fake_cli(tmp_path, "fake-gemini", "ok")
    client = GeminiCliClient(command=str(exe), work_dir=tmp_path, model="gemini-x")
    client.chat("sys", [Message("user", "hi")], tools=[])
    args = (tmp_path / "args.txt").read_text(encoding="utf-8")
    assert "--model" in args and "gemini-x" in args


def test_missing_cli_is_clear_error(tmp_path: Path):
    client = ClaudeCodeClient(command=str(tmp_path / "nope"), work_dir=tmp_path)
    with pytest.raises(RuntimeError, match="見つかりません"):
        client.chat("sys", [Message("user", "hi")], tools=[])


def test_factory_cli_providers_need_no_key():
    assert isinstance(create_client("claude-code"), ClaudeCodeClient)
    assert isinstance(create_client("gemini-cli"), GeminiCliClient)


def test_codex_backend_args(tmp_path: Path):
    from kuma_agent.agent_cli import CodexClient

    exe = _fake_cli(tmp_path, "fake-codex", "codex-done")
    client = CodexClient(command=str(exe), work_dir=tmp_path)
    reply = client.chat("人格", [Message("user", "直して")], tools=[])
    assert reply.content == "codex-done"
    args = (tmp_path / "args.txt").read_text(encoding="utf-8")
    assert "exec" in args and "workspace-write" in args and "直して" in args


def test_cli_default_models():
    from kuma_agent.agent_cli import ClaudeCodeClient, GeminiCliClient

    cc = ClaudeCodeClient(command="claude", work_dir=Path("."))
    assert "--model" in cc.build_args("x")
    assert "claude-opus-5-5" in cc.build_args("x")
    gem = GeminiCliClient(command="gemini", work_dir=Path("."))
    assert "gemini-4-argon" in gem.build_args("x")


def test_factory_codex():
    from kuma_agent.agent_cli import CodexClient

    assert isinstance(create_client("codex"), CodexClient)
