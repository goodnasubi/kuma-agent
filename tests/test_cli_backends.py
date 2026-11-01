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
