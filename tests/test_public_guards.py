"""公開時の歯止め（0.66.0）のテスト。

起動時ガード（ループバック以外＋パスワード未設定で強い警告）、
承認の運用者ロック（KUMA_REQUIRE_APPROVALS: テナント設定で承認を
OFF にできない）、shell 道具の無効化（KUMA_SHELL_ENABLED=0 で
run_shell を登録しない）を確認する。
"""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kuma_agent import web as web_module
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.settings import (
    approvals_enabled_for,
    load_tenant_settings,
    set_approvals_enabled,
)
from kuma_agent.tools import ToolBox
from kuma_agent.web import build_server, startup_guard_warning


class _EchoModel:
    """受け取った発話をそのまま返す偽モデル。"""

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


# ---------------------------------------------------------- 起動時ガード

def test_guard_warns_only_for_public_without_password(tmp_path: Path):
    no_password = Config(data_dir=tmp_path, web_password="")
    with_password = Config(data_dir=tmp_path, web_password="secret")
    # ループバック以外＋パスワード未設定のときだけ警告
    assert startup_guard_warning("0.0.0.0", no_password) is not None
    assert startup_guard_warning("192.168.1.10", no_password) is not None
    # ループバックは従来どおり（警告なし）
    for host in ("127.0.0.1", "::1", "localhost"):
        assert startup_guard_warning(host, no_password) is None
    # パスワード設定済みなら外向きでも警告なし
    assert startup_guard_warning("0.0.0.0", with_password) is None


class _FakeServer:
    """serve の流れだけを確かめるための偽サーバー。"""

    tenants = None
    server_address = ("0.0.0.0", 18765)

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        pass


def _run_serve(monkeypatch: pytest.MonkeyPatch, session: Session,
               host: str) -> None:
    monkeypatch.setattr(web_module, "build_server",
                        lambda *args, **kwargs: _FakeServer())
    monkeypatch.setattr(web_module, "_start_proactive_loop",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(web_module, "start_telegram_polling",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(web_module, "line_enabled", lambda cfg: False)
    web_module.serve(session, host=host, port=18765)


def test_serve_prints_guard_warning(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture):
    session = Session(Config(data_dir=tmp_path, web_password=""),
                      _EchoModel())
    _run_serve(monkeypatch, session, "0.0.0.0")
    out = capsys.readouterr().out
    assert "警告" in out
    assert "KUMA_WEB_PASSWORD" in out


def test_serve_no_warning_on_loopback_or_with_password(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture):
    session = Session(Config(data_dir=tmp_path, web_password=""),
                      _EchoModel())
    _run_serve(monkeypatch, session, "127.0.0.1")
    assert "警告" not in capsys.readouterr().out

    session = Session(Config(data_dir=tmp_path, web_password="secret"),
                      _EchoModel())
    _run_serve(monkeypatch, session, "0.0.0.0")
    out = capsys.readouterr().out
    assert "警告" not in out
    assert "ログインが有効です" in out


# ---------------------------------------------------------- Config の読み取り

def test_config_reads_guard_env(monkeypatch: pytest.MonkeyPatch,
                                tmp_path: Path):
    monkeypatch.delenv("KUMA_REQUIRE_APPROVALS", raising=False)
    monkeypatch.delenv("KUMA_SHELL_ENABLED", raising=False)
    cfg = Config(data_dir=tmp_path)
    assert cfg.require_approvals is False
    assert cfg.shell_enabled is True

    monkeypatch.setenv("KUMA_REQUIRE_APPROVALS", "1")
    monkeypatch.setenv("KUMA_SHELL_ENABLED", "0")
    cfg = Config(data_dir=tmp_path)
    assert cfg.require_approvals is True
    assert cfg.shell_enabled is False

    monkeypatch.setenv("KUMA_REQUIRE_APPROVALS", "0")
    monkeypatch.setenv("KUMA_SHELL_ENABLED", "1")
    cfg = Config(data_dir=tmp_path)
    assert cfg.require_approvals is False
    assert cfg.shell_enabled is True


# ---------------------------------------------------------- 承認の運用者ロック

def test_lock_forces_approvals_despite_tenant_off(tmp_path: Path):
    # テナント設定では承認 OFF にしてある
    set_approvals_enabled(tmp_path, "default", False)
    assert approvals_enabled_for(tmp_path, "default") is False

    locked = Session(Config(data_dir=tmp_path, require_approvals=True),
                     _EchoModel())
    # ロック中は設定によらず承認が配線される
    assert locked.approvals_enabled is True
    assert locked.tools.approval_check is not None
    # 設定ファイル自体は書き換えられない（合成側の責務）
    assert approvals_enabled_for(tmp_path, "default") is False


def test_without_lock_tenant_off_disables_approvals(tmp_path: Path):
    set_approvals_enabled(tmp_path, "default", False)
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    assert session.approvals_enabled is False
    assert session.tools.approval_check is None


# ---------------------------------------------------------- テナント設定 API

@pytest.fixture()
def locked_server(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path, require_approvals=True),
                      _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_locked_settings_payload(locked_server):
    base, _tmp_path = locked_server
    status, data = _get(base + "/api/tenant_settings")
    assert status == 200
    # 実効値はオン、ロック中であることが返る
    assert data["approvals_enabled"] is True
    assert data["approvals_locked"] is True


def test_locked_settings_reject_turning_approvals_off(locked_server):
    base, tmp_path = locked_server
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "default", "approvals_enabled": False})
    assert status == 400 and data["error"]
    # 設定ファイルに OFF は書かれていない
    saved = load_tenant_settings(tmp_path).get("default", {})
    assert saved.get("approvals_enabled") is not False

    # オンへの保存は通る
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "default", "approvals_enabled": True})
    assert status == 200
    assert data["approvals_enabled"] is True

    # 承認以外の項目（能動発信）はロック対象外で保存できる
    status, data = _post(base + "/api/tenant_settings",
                         {"tenant": "default", "proactive_enabled": False})
    assert status == 200
    assert data["proactive_enabled"] is False


def test_locked_page_has_note_and_disable_logic(locked_server):
    base, _tmp_path = locked_server
    with urllib.request.urlopen(base + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    # ロック中は切り替えを無効化し、理由の注記を出す画面側の処理
    assert "approvals_locked" in html
    assert "運用者の設定で固定されています" in html


# ---------------------------------------------------------- shell 道具の無効化

def _tool_names(box: ToolBox) -> set[str]:
    return {schema["name"] for schema in box.schemas()}


def test_shell_tool_registered_by_default(tmp_path: Path):
    assert "run_shell" in _tool_names(ToolBox(tmp_path))


def test_shell_tool_not_registered_when_disabled(tmp_path: Path):
    box = ToolBox(tmp_path, shell_enabled=False)
    assert "run_shell" not in _tool_names(box)
    # 道具が無いので、呼び出しも未知の道具として返る
    assert box.call("run_shell", {"command": "echo hi"}).startswith(
        "error: unknown tool")
    # 他の道具は従来どおり使える
    assert "read_file" in _tool_names(box)


def test_session_shell_disabled_in_both_modes(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, shell_enabled=False)
    session = Session(cfg, _EchoModel())
    assert "run_shell" not in _tool_names(session.tools)

    wall = Session(Config(data_dir=tmp_path, mode="wall",
                          shell_enabled=False), _EchoModel())
    assert "run_shell" not in _tool_names(wall.tools)

    enabled = Session(Config(data_dir=tmp_path), _EchoModel())
    assert "run_shell" in _tool_names(enabled.tools)
