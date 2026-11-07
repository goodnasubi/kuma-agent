"""CLI 系の頭脳の明示と設定の読み取り（0.69.0、指摘1 (a)(b)）のテスト。

起動時注意（CLI 系の頭脳が実際に使える状態でのみ出す）、画面の
頭脳表示の注意書き、Config の環境変数読み取り（KUMA_CODEX_SANDBOX /
KUMA_CLI_EXTRA_ARGS）と client 生成への引き渡しを確認する。
"""
import shutil
from pathlib import Path

import pytest

from kuma_agent import web as web_module
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.web import cli_provider_note, render_page


class _EchoModel:
    """受け取った発話をそのまま返す偽モデル。"""

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


# ---------------------------------------------------------- Config の読み取り

def test_config_reads_cli_env(monkeypatch: pytest.MonkeyPatch,
                              tmp_path: Path):
    monkeypatch.delenv("KUMA_CODEX_SANDBOX", raising=False)
    monkeypatch.delenv("KUMA_CLI_EXTRA_ARGS", raising=False)
    cfg = Config(data_dir=tmp_path)
    # 既定は従来どおり（workspace-write、追加引数なし）
    assert cfg.codex_sandbox == "workspace-write"
    assert cfg.cli_extra_args == ""

    monkeypatch.setenv("KUMA_CODEX_SANDBOX", "read-only")
    monkeypatch.setenv("KUMA_CLI_EXTRA_ARGS", "--foo bar")
    cfg = Config(data_dir=tmp_path)
    assert cfg.codex_sandbox == "read-only"
    assert cfg.cli_extra_args == "--foo bar"

    # 空のサンドボックス指定は既定に倒す
    monkeypatch.setenv("KUMA_CODEX_SANDBOX", "")
    assert Config(data_dir=tmp_path).codex_sandbox == "workspace-write"


def test_cli_client_kwargs_only_for_cli_providers(tmp_path: Path):
    cli_cfg = Config(data_dir=tmp_path, provider="codex",
                     codex_sandbox="read-only",
                     cli_extra_args="--foo  bar")
    assert cli_cfg.cli_client_kwargs() == {
        "sandbox": "read-only", "extra_args": ["--foo", "bar"]}
    # API 系では余分な引数を渡さない
    api_cfg = Config(data_dir=tmp_path, provider="claude",
                     codex_sandbox="read-only", cli_extra_args="--foo")
    assert api_cfg.cli_client_kwargs() == {}


# ---------------------------------------------------------- 起動時注意の条件

def _which_only(*available: str):
    def fake_which(name: str):
        return f"/usr/bin/{name}" if name in available else None
    return fake_which


def test_note_only_when_cli_available(monkeypatch: pytest.MonkeyPatch,
                                      tmp_path: Path):
    monkeypatch.setattr(shutil, "which", _which_only("claude"))
    cfg = Config(data_dir=tmp_path, provider="claude-code")
    note = cli_provider_note(cfg)
    assert note is not None
    assert "承認確認" in note and "制限は" in note

    # CLI が入っていない環境（Docker 等）では出さない
    monkeypatch.setattr(shutil, "which", _which_only())
    assert cli_provider_note(cfg) is None

    # API 系の頭脳では、CLI が入っていても出さない
    monkeypatch.setattr(shutil, "which", _which_only("claude"))
    api_cfg = Config(data_dir=tmp_path, provider="claude")
    assert cli_provider_note(api_cfg) is None


def test_note_for_each_cli_provider(monkeypatch: pytest.MonkeyPatch,
                                    tmp_path: Path):
    monkeypatch.setattr(shutil, "which",
                        _which_only("claude", "gemini", "codex"))
    for provider in ("claude-code", "gemini-cli", "codex"):
        cfg = Config(data_dir=tmp_path, provider=provider)
        assert cli_provider_note(cfg) is not None


class _FakeServer:
    """serve の流れだけを確かめるための偽サーバー。"""

    tenants = None
    server_address = ("127.0.0.1", 18765)

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        pass


def test_serve_prints_cli_note(monkeypatch: pytest.MonkeyPatch,
                               tmp_path: Path,
                               capsys: pytest.CaptureFixture):
    monkeypatch.setattr(web_module, "build_server",
                        lambda *args, **kwargs: _FakeServer())
    monkeypatch.setattr(web_module, "_start_proactive_loop",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(web_module, "start_telegram_polling",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(web_module, "line_enabled", lambda cfg: False)
    monkeypatch.setattr(shutil, "which", _which_only("claude"))

    session = Session(Config(data_dir=tmp_path, provider="claude-code"),
                      _EchoModel())
    web_module.serve(session, host="127.0.0.1", port=18765)
    out = capsys.readouterr().out
    assert "注意: 頭脳（claude-code）" in out

    # API 系では注意は出ない
    session = Session(Config(data_dir=tmp_path, provider="claude"),
                      _EchoModel())
    web_module.serve(session, host="127.0.0.1", port=18765)
    assert "注意: 頭脳" not in capsys.readouterr().out


# ---------------------------------------------------------- 画面の注意書き

def test_page_note_only_for_cli_providers():
    for provider in ("claude-code", "gemini-cli", "codex"):
        html = render_page(provider)
        assert "承認確認や道具の制限は効きません" in html
    for provider in ("muse", "claude", "gemini"):
        html = render_page(provider)
        assert "承認確認や道具の制限は効きません" not in html
