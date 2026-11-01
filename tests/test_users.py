"""ユーザー層（テナントの下の区切り）のテスト。

同じテナントでもユーザーごとに人格・長期記憶・部屋・台帳の置き場が
分かれ互いに見えないこと、既定ユーザーが従来の置き場（テナントの
置き場そのもの）を使うこと、テナント×ユーザーの組み合わせで混ざら
ないことを確認する。ユーザー管理をしない場合（既定ユーザーのまま）
は従来どおり単一運用になる。
"""
import json
import threading
import urllib.request
from pathlib import Path

import pytest

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import (
    list_user_names,
    normalize_user_name,
    user_dir,
)
from kuma_agent.session import Session
from kuma_agent.tasks import TaskBoard
from kuma_agent.web import build_server


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


# ---------------------------------------------------------------- 名前と置き場

def test_normalize_user_name():
    assert normalize_user_name("default") == "default"
    assert normalize_user_name(" 仕事 用 ") == "仕事-用"
    assert normalize_user_name("../etc/passwd").endswith("etc-passwd")
    assert normalize_user_name("") == "default"
    assert normalize_user_name(None) == "default"
    assert normalize_user_name("///") == "default"


def test_user_dir_default_is_tenant_dir(tmp_path: Path):
    assert user_dir(tmp_path, "default") == tmp_path
    assert user_dir(tmp_path, None) == tmp_path
    assert user_dir(tmp_path, "bob") == tmp_path / "users" / "bob"


def test_config_user_data_dir(tmp_path: Path):
    assert Config(data_dir=tmp_path).user_data_dir == tmp_path
    cfg = Config(data_dir=tmp_path, user="bob")
    assert cfg.user_data_dir == tmp_path / "users" / "bob"
    # 既存のパスプロパティもユーザーの実効置き場を指す
    assert cfg.memory_file == tmp_path / "users" / "bob" / "MEMORY.md"
    assert cfg.conversations_dir == tmp_path / "users" / "bob" / "conversations"


def test_config_user_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KUMA_USER", "env-user")
    cfg = Config(data_dir=tmp_path)
    assert cfg.user == "env-user"
    assert cfg.user_data_dir == tmp_path / "users" / "env-user"


def test_config_tenant_and_user_compose(tmp_path: Path):
    cfg = Config(data_dir=tmp_path, tenant="work", user="bob")
    assert cfg.tenant_data_dir == tmp_path / "tenants" / "work"
    assert cfg.user_data_dir == tmp_path / "tenants" / "work" / "users" / "bob"


def test_list_user_names(tmp_path: Path):
    assert list_user_names(tmp_path) == ["default"]
    (tmp_path / "users" / "bob").mkdir(parents=True)
    (tmp_path / "users" / "alice").mkdir(parents=True)
    (tmp_path / "users" / "file.txt").write_text("x", encoding="utf-8")
    assert list_user_names(tmp_path) == ["default", "alice", "bob"]
    # 別テナントの users/ は混ざらない
    assert list_user_names(tmp_path / "tenants" / "work") == ["default"]


# ---------------------------------------------------------------- 既定ユーザーの互換性

def test_default_user_uses_legacy_layout(tmp_path: Path):
    # ユーザー管理をしない運用: 何も指定しなければ従来レイアウトのまま
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    assert session.user == "default"
    session.say("従来どおり")
    assert (tmp_path / "conversations" / "main.jsonl").exists()
    assert not (tmp_path / "users").exists()
    assert not (tmp_path / "tenants").exists()


# ---------------------------------------------------------------- 隔離（同一テナント内）

def test_users_in_same_tenant_are_isolated(tmp_path: Path):
    cfg_a = Config(data_dir=tmp_path, tenant="work", user="alice")
    cfg_b = Config(data_dir=tmp_path, tenant="work", user="bob")
    cfg_default = Config(data_dir=tmp_path, tenant="work")

    tenant_dir = tmp_path / "tenants" / "work"
    dir_a = tenant_dir / "users" / "alice"
    dir_b = tenant_dir / "users" / "bob"
    dir_a.mkdir(parents=True)
    dir_b.mkdir(parents=True)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    # 人格ファイルをユーザーごとに用意する
    (dir_a / "SOUL.md").write_text("# 魂\nアリス専用の人格", encoding="utf-8")
    (dir_b / "SOUL.md").write_text("# 魂\nボブ専用の人格", encoding="utf-8")
    (tenant_dir / "SOUL.md").write_text("# 魂\n既定ユーザーの人格", encoding="utf-8")

    a = Session(cfg_a, _EchoModel(), "room")
    b = Session(cfg_b, _EchoModel(), "room")
    d = Session(cfg_default, _EchoModel(), "room")
    assert "アリス専用の人格" in a.system_prompt
    assert "ボブ専用の人格" not in a.system_prompt
    assert "ボブ専用の人格" in b.system_prompt
    assert "既定ユーザーの人格" in d.system_prompt
    assert "アリス専用の人格" not in d.system_prompt

    # 長期記憶: アリスに覚えさせてもボブ・既定ユーザーからは見えない
    a.memory.remember("アリスだけの事実")
    a2 = Session(cfg_a, _EchoModel(), "room")
    b2 = Session(cfg_b, _EchoModel(), "room")
    d2 = Session(cfg_default, _EchoModel(), "room")
    assert "アリスだけの事実" in a2.system_prompt
    assert "アリスだけの事実" not in b2.system_prompt
    assert "アリスだけの事実" not in d2.system_prompt
    assert (dir_a / "MEMORY.md").exists()
    assert not (dir_b / "MEMORY.md").exists()
    assert not (tenant_dir / "MEMORY.md").exists()

    # 部屋の記録: 同じ部屋名でもユーザーが違えば混ざらない
    a.say("アリスの相談")
    b_sessions = Session(cfg_b, _EchoModel(), "room")
    assert all("アリスの相談" not in m.content for m in b_sessions.history)
    d_sessions = Session(cfg_default, _EchoModel(), "room")
    assert all("アリスの相談" not in m.content for m in d_sessions.history)
    assert (dir_a / "conversations" / "room.jsonl").exists()
    assert not (dir_b / "conversations" / "room.jsonl").exists()
    assert not (tenant_dir / "conversations" / "room.jsonl").exists()

    # 台帳: アリスの用事はボブの台帳・プロンプトに見えない
    a.task_board.add("アリスの用事", "アリスのメモ")
    assert TaskBoard(dir_b).open_tasks() == []
    assert TaskBoard(tenant_dir).open_tasks() == []
    b3 = Session(cfg_b, _EchoModel(), "room")
    assert "アリスの用事" not in b3.system_prompt
    a3 = Session(cfg_a, _EchoModel(), "room")
    assert "アリスの用事" in a3.system_prompt
    assert (dir_a / "tasks.json").exists()
    assert not (dir_b / "tasks.json").exists()


def test_tenant_user_combinations_are_isolated(tmp_path: Path):
    # 同じユーザー名でもテナントが違えば別物になる
    work_bob = Session(Config(data_dir=tmp_path, tenant="work", user="bob"),
                       _EchoModel(), "room")
    play_bob = Session(Config(data_dir=tmp_path, tenant="play", user="bob"),
                       _EchoModel(), "room")
    work_bob.say("仕事テナントのボブの話")
    assert all("仕事テナント" not in m.content for m in play_bob.history)
    play_bob2 = Session(Config(data_dir=tmp_path, tenant="play", user="bob"),
                        _EchoModel(), "room")
    assert all("仕事テナント" not in m.content for m in play_bob2.history)
    assert (tmp_path / "tenants" / "work" / "users" / "bob"
            / "conversations" / "room.jsonl").exists()
    assert not (tmp_path / "tenants" / "play" / "users" / "bob"
                / "conversations" / "room.jsonl").exists()


# ---------------------------------------------------------------- CLI

def _run_cli_chat(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                  argv: list[str]) -> dict:
    captured: dict = {}

    class _FakeSession:
        def __init__(self, cfg, model, session_id="main"):
            captured["tenant"] = cfg.tenant
            captured["user"] = cfg.user
            captured["user_data_dir"] = cfg.user_data_dir

        def say(self, text):
            return ""

    def _eof(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr(cli, "Session", _FakeSession)
    monkeypatch.setattr(cli, "create_client", lambda *args: object())
    monkeypatch.setenv("KUMA_LLM_PROVIDER", "claude-code")  # API キー不要の系統
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("builtins.input", _eof)
    assert cli.main(argv) == 0
    return captured


def test_cli_user_flag(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _run_cli_chat(monkeypatch, tmp_path, ["chat", "--user", "bob"])
    assert captured["user"] == "bob"
    assert captured["user_data_dir"] == tmp_path / "users" / "bob"


def test_cli_user_flag_overrides_env(monkeypatch: pytest.MonkeyPatch,
                                     tmp_path: Path):
    monkeypatch.setenv("KUMA_USER", "env-user")
    captured = _run_cli_chat(monkeypatch, tmp_path, ["chat", "--user", "cli-user"])
    assert captured["user"] == "cli-user"


def test_cli_user_and_tenant_flags(monkeypatch: pytest.MonkeyPatch,
                                   tmp_path: Path):
    captured = _run_cli_chat(
        monkeypatch, tmp_path,
        ["chat", "--tenant", "work", "--user", "bob"])
    assert captured["tenant"] == "work"
    assert captured["user"] == "bob"
    assert captured["user_data_dir"] == (
        tmp_path / "tenants" / "work" / "users" / "bob")


# ---------------------------------------------------------------- Web

@pytest.fixture()
def web_server(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_web_users_are_separate(web_server):
    base, tmp_path = web_server
    reply = _post_json(base + "/api/chat",
                       {"message": "ボブの話", "user": "bob"})
    assert reply == {"reply": "返答: ボブの話"}
    _post_json(base + "/api/chat", {"message": "既定の話"})

    # ユーザーごとに履歴が分かれる
    hist_bob = _get_json(base + "/api/history?user=bob")["messages"]
    assert [m["content"] for m in hist_bob] == ["ボブの話", "返答: ボブの話"]
    hist_default = _get_json(base + "/api/history")["messages"]
    assert all("ボブの話" not in m["content"] for m in hist_default)
    assert any("既定の話" in m["content"] for m in hist_default)
    hist_alice = _get_json(base + "/api/history?user=alice")["messages"]
    assert hist_alice == []

    # 部屋一覧もユーザーごと
    sessions_bob = _get_json(base + "/api/sessions?user=bob")["sessions"]
    assert sessions_bob == ["main"]

    # ユーザー一覧（current は起動時ユーザー）
    data = _get_json(base + "/api/users")
    assert data["current"] == "default"
    assert "default" in data["users"] and "bob" in data["users"]

    # 記録ファイルもユーザーの置き場に分かれる
    assert (tmp_path / "users" / "bob" / "conversations" / "main.jsonl").exists()
    assert (tmp_path / "conversations" / "main.jsonl").exists()


def test_web_users_within_tenant(web_server):
    base, tmp_path = web_server
    # テナント×ユーザーの組み合わせで履歴が混ざらない
    _post_json(base + "/api/chat",
               {"message": "仕事のボブ", "tenant": "work", "user": "bob"})
    _post_json(base + "/api/chat",
               {"message": "仕事の既定", "tenant": "work"})
    _post_json(base + "/api/chat",
               {"message": "遊びのボブ", "tenant": "play", "user": "bob"})

    hist = _get_json(base + "/api/history?tenant=work&user=bob")["messages"]
    assert [m["content"] for m in hist] == ["仕事のボブ", "返答: 仕事のボブ"]
    hist = _get_json(base + "/api/history?tenant=work")["messages"]
    assert [m["content"] for m in hist] == ["仕事の既定", "返答: 仕事の既定"]
    hist = _get_json(base + "/api/history?tenant=play&user=bob")["messages"]
    assert [m["content"] for m in hist] == ["遊びのボブ", "返答: 遊びのボブ"]

    # ユーザー一覧はテナントごとに答える
    users_work = _get_json(base + "/api/users?tenant=work")
    assert "bob" in users_work["users"]
    users_play = _get_json(base + "/api/users?tenant=play")
    assert "bob" in users_play["users"]
    users_default = _get_json(base + "/api/users")
    assert users_default["users"] == ["default"]

    assert (tmp_path / "tenants" / "work" / "users" / "bob"
            / "conversations" / "main.jsonl").exists()
    assert (tmp_path / "tenants" / "work" / "conversations" / "main.jsonl").exists()


def test_web_page_has_user_switcher(web_server):
    base, _tmp_path = web_server
    with urllib.request.urlopen(base + "/", timeout=10) as resp:
        page = resp.read().decode("utf-8")
    assert "userSelect" in page and "createUser" in page
    assert "/api/users" in page
