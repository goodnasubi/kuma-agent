"""スレッド（セッション）の管理: 作成・選択・削除のテスト。

記録の削除は conversations/<名前>.jsonl だけを対象とし、台帳
（tasks.json）と長期記憶（MEMORY.md）には触れないこと、Web の
削除 API と CLI（threads）の両方から扱えることを確認する。
"""
import json
import threading
import urllib.request
from pathlib import Path

import pytest

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import Memory, delete_session_record
from kuma_agent.session import Session
from kuma_agent.web import build_server


def _write_record(user_dir: Path, name: str, content: str = "x") -> Path:
    conv = user_dir / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    path = conv / f"{name}.jsonl"
    path.write_text(json.dumps({"role": "user", "content": content},
                               ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


# ------------------------------------------------------------ 削除関数

def test_delete_session_record_removes_only_that_file(tmp_path: Path):
    target = _write_record(tmp_path, "work")
    other = _write_record(tmp_path, "main")
    tasks = tmp_path / "tasks.json"
    tasks.write_text("[]", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("- 覚えていること\n", encoding="utf-8")

    assert delete_session_record(tmp_path, "work") is True
    assert not target.exists()
    assert other.exists()  # 別のスレッドは残る
    assert tasks.exists()  # 台帳には触れない
    assert (tmp_path / "MEMORY.md").exists()  # 長期記憶にも触れない


def test_delete_session_record_missing_returns_false(tmp_path: Path):
    assert delete_session_record(tmp_path, "ない部屋") is False
    assert delete_session_record(tmp_path, "main") is False


def test_delete_session_record_normalizes_name(tmp_path: Path):
    target = _write_record(tmp_path, "main")
    # パス区切りを含む名前でも、正規化されたファイルだけが対象になる
    assert delete_session_record(tmp_path, "../main") is True
    assert not target.exists()


def test_memory_history_is_gone_after_delete(tmp_path: Path):
    _write_record(tmp_path, "work", "こんにちは")
    assert Memory(tmp_path, "work").load_turns()
    delete_session_record(tmp_path, "work")
    assert Memory(tmp_path, "work").load_turns() == []


# ------------------------------------------------------------ Web API

class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


@pytest.fixture()
def server(tmp_path: Path):
    # スレッドを扱うテストなので、部屋を分けられるスレッドモードにする
    # （相棒モードでは部屋が main に固定される）
    session = Session(Config(data_dir=tmp_path, mode="wall"), _EchoModel())
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


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_web_delete_session_removes_record_and_listing(server):
    base, data_dir = server
    _post_json(base + "/api/chat", {"message": "やあ", "session": "work"})
    record = data_dir / "conversations" / "work.jsonl"
    assert record.exists()
    assert "work" in _get_json(base + "/api/sessions")["sessions"]

    status, data = _post_json(base + "/api/sessions/delete",
                              {"session": "work"})
    assert status == 200
    assert data == {"deleted": True, "session": "work"}
    assert not record.exists()
    assert "work" not in _get_json(base + "/api/sessions")["sessions"]


def test_web_delete_open_session_then_history_works(server):
    base, data_dir = server
    _post_json(base + "/api/chat", {"message": "やあ", "session": "work"})
    _post_json(base + "/api/sessions/delete", {"session": "work"})
    # 表示中だったスレッドを消しても、履歴取得は空で問題なく動き、
    # 再び話しかければ空の記録で再開する
    assert _get_json(base + "/api/history?session=work")["messages"] == []
    status, data = _post_json(base + "/api/chat",
                              {"message": "またね", "session": "work"})
    assert status == 200
    assert data["reply"] == "返答: またね"


def test_web_delete_main_session(server):
    base, data_dir = server
    _post_json(base + "/api/chat", {"message": "やあ"})
    record = data_dir / "conversations" / "main.jsonl"
    assert record.exists()
    status, data = _post_json(base + "/api/sessions/delete",
                              {"session": "main"})
    assert status == 200
    assert data["deleted"] is True
    assert not record.exists()


def test_web_delete_missing_session_reports_false(server):
    base, _ = server
    status, data = _post_json(base + "/api/sessions/delete",
                              {"session": "ない部屋"})
    assert status == 200
    assert data["deleted"] is False


def test_web_page_has_delete_button_and_thread_wording():
    from kuma_agent.web import render_page

    page = render_page("muse")
    assert "deleteSession" in page
    assert "/api/sessions/delete" in page
    assert "スレッド" in page  # 呼称の切り替えが入っている


# ------------------------------------------------------------ CLI

def test_cli_threads_lists_sessions(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch,
                                    capsys: pytest.CaptureFixture):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    _write_record(tmp_path, "main")
    _write_record(tmp_path, "work")
    assert cli.main(["threads"]) == 0
    out = capsys.readouterr().out
    assert "main" in out and "work" in out
    assert "最終更新" in out


def test_cli_threads_empty(tmp_path: Path,
                           monkeypatch: pytest.MonkeyPatch,
                           capsys: pytest.CaptureFixture):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    assert cli.main(["threads"]) == 0
    assert "スレッドはありません" in capsys.readouterr().out


def test_cli_threads_delete(tmp_path: Path,
                            monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    target = _write_record(tmp_path, "work")
    keep = _write_record(tmp_path, "main")
    assert cli.main(["threads", "--delete", "work"]) == 0
    out = capsys.readouterr().out
    assert "work" in out  # 対象名を表示してから消す
    assert not target.exists()
    assert keep.exists()
    # 無いものを消そうとしてもエラーにはならない
    assert cli.main(["threads", "--delete", "work"]) == 0
    assert "見つかりません" in capsys.readouterr().out


def test_cli_threads_tenant_and_user(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    udir = tmp_path / "tenants" / "acme" / "users" / "taro"
    target = _write_record(udir, "kaigi")
    _write_record(tmp_path, "main")  # 既定の置き場は対象外
    assert cli.main(["threads", "--tenant", "acme", "--user", "taro"]) == 0
    out = capsys.readouterr().out
    assert "kaigi" in out
    assert "main" not in out
    assert cli.main(["threads", "--tenant", "acme", "--user", "taro",
                     "--delete", "kaigi"]) == 0
    assert not target.exists()
