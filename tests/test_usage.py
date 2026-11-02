"""使用量（usage）のテスト。

各 API クライアントが応答から使用量を取り出せること（形式が欠けて
いても壊れないこと）、会話すると usage.jsonl に呼び出しごとに記録
されること（道具の往復の回数分・委任の子の分も）、CLI 系は回数のみ
で記録されること、集計（合計・頭脳別・日別）の正しさ、Web API と
パネルの表示、CLI の表示を確認する。
"""
import json
import threading
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.delegate import DELEGATE_NOTICE
from kuma_agent.llm import (
    AnthropicClient,
    GeminiClient,
    Message,
    MetaModelClient,
    Usage,
)
from kuma_agent.session import Session
from kuma_agent.usage import (
    format_usage_summary,
    load_usage_records,
    record_usage,
    summarize_usage,
)
from kuma_agent.web import build_server


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------- クライアント

def test_muse_client_extracts_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "やあ"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                      "total_tokens": 18},
        })

    client = MetaModelClient("k", transport=httpx.MockTransport(handler))
    reply = client.chat("sys", [Message("user", "hi")], [])
    assert reply.usage == Usage(input_tokens=11, output_tokens=7,
                                total_tokens=18)


def test_muse_client_tolerates_missing_or_odd_usage():
    def no_usage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}]})

    client = MetaModelClient("k", transport=httpx.MockTransport(no_usage))
    reply = client.chat("sys", [Message("user", "hi")], [])
    assert reply.content == "ok"
    assert reply.usage is None

    def odd_usage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": "たくさん", "completion_tokens": 3,
                      "total_tokens": None},
        })

    client = MetaModelClient("k", transport=httpx.MockTransport(odd_usage))
    reply = client.chat("sys", [Message("user", "hi")], [])
    # 読める項目だけを取り、合計は入力が読めないため補わない
    assert reply.usage == Usage(input_tokens=None, output_tokens=3,
                                total_tokens=None)


def test_claude_client_extracts_usage_and_derives_total():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 21, "output_tokens": 9},
        })

    client = AnthropicClient("k", transport=httpx.MockTransport(handler))
    reply = client.chat("sys", [Message("user", "hi")], [])
    # Claude の応答に合計は無いので、入力と出力の和で補う
    assert reply.usage == Usage(input_tokens=21, output_tokens=9,
                                total_tokens=30)


def test_gemini_client_extracts_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {"promptTokenCount": 13,
                              "candidatesTokenCount": 5,
                              "totalTokenCount": 18},
        })

    client = GeminiClient("k", transport=httpx.MockTransport(handler))
    reply = client.chat("sys", [Message("user", "hi")], [])
    assert reply.usage == Usage(input_tokens=13, output_tokens=5,
                                total_tokens=18)

    def no_usage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "ok"}]}}]})

    client = GeminiClient("k", transport=httpx.MockTransport(no_usage))
    reply = client.chat("sys", [Message("user", "hi")], [])
    assert reply.usage is None


# ------------------------------------------------------------- 記録

class _UsageModel:
    """使用量つきで即答するスタブ。model 属性は記録のモデル名用。"""

    model = "stub-model-1"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        return Message("assistant", "答え", usage=Usage(10, 4, 14))


def test_session_records_usage_per_call(tmp_path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _UsageModel())
    assert session.say("こんにちは") == "答え"
    records = _records(tmp_path / "usage.jsonl")
    assert len(records) == 1
    record = records[0]
    assert record["provider"] == "muse"  # Config の既定
    assert record["model"] == "stub-model-1"
    assert record["input_tokens"] == 10
    assert record["output_tokens"] == 4
    assert record["total_tokens"] == 14
    assert record["session_id"] == "main"
    assert record["ts"]


class _ToolLoopModel:
    """1 回目は道具（now）を呼び、2 回目は使用量つきで答えるスタブ。"""

    def __init__(self) -> None:
        self.rounds = 0

    def chat(self, system_prompt, messages, tools):
        self.rounds += 1
        if self.rounds == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": "now", "args": {}}])
        return Message("assistant", "時刻です", usage=Usage(8, 2, 10))


def test_session_records_each_round_of_tool_loop(tmp_path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _ToolLoopModel())
    assert session.say("今何時？") == "時刻です"
    records = _records(tmp_path / "usage.jsonl")
    # 道具の往復で 2 回呼ぶので 2 行。使用量が無い回は None で記録
    assert len(records) == 2
    assert records[0]["total_tokens"] is None
    assert records[1]["total_tokens"] == 10


class _CountOnlyModel:
    """使用量を返さないスタブ（CLI 系の頭脳の代わり）。"""

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "答え")


def test_session_records_calls_only_without_usage(tmp_path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _CountOnlyModel())
    session.say("こんにちは")
    records = _records(tmp_path / "usage.jsonl")
    assert len(records) == 1
    assert records[0]["input_tokens"] is None
    assert records[0]["output_tokens"] is None
    assert records[0]["total_tokens"] is None


class _DelegateFlowModel:
    """親は delegate を呼び、子は使用量つきで報告するスタブ。"""

    def __init__(self) -> None:
        self.parent_calls = 0

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            return Message("assistant", "子の報告", usage=Usage(3, 1, 4))
        self.parent_calls += 1
        if self.parent_calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "d1", "name": "delegate",
                 "args": {"task": "調べて"}}])
        return Message("assistant", "親の答え", usage=Usage(5, 2, 7))


def test_delegate_child_usage_recorded_for_same_user(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path / "data")
    session = Session(cfg, _DelegateFlowModel())
    assert session.say("頼む") == "親の答え"
    records = _records(cfg.user_data_dir / "usage.jsonl")
    # 親 2 回（委任の呼び出しと最終応答）＋子 1 回
    assert len(records) == 3
    child = [r for r in records if r["session_id"].startswith("delegate-")]
    parent = [r for r in records if r["session_id"] == "main"]
    assert len(child) == 1 and child[0]["total_tokens"] == 4
    assert len(parent) == 2


def test_usage_record_failure_does_not_break_conversation(tmp_path):
    # usage.jsonl の場所にディレクトリを作り、追記できない状態にする
    (tmp_path / "usage.jsonl").mkdir()
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _UsageModel())
    assert session.say("こんにちは") == "答え"


def test_record_usage_appends_jsonl(tmp_path):
    record_usage(tmp_path, "claude", "m1", Usage(1, 2, 3), "main",
                 now=datetime(2026, 11, 3, 10, 0, 0))
    record_usage(tmp_path, "claude-code", "", None, "main")
    records = load_usage_records(tmp_path)
    assert len(records) == 2
    assert records[0]["ts"] == "2026-11-03T10:00:00"
    assert records[1]["total_tokens"] is None


# ------------------------------------------------------------- 集計

def _write_records(tmp_path: Path, rows: list[dict]) -> None:
    with open(tmp_path / "usage.jsonl", "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def test_summarize_usage_totals_providers_and_days(tmp_path):
    now = datetime(2026, 11, 3, 12, 0, 0)
    old_day = (now - timedelta(days=40)).date().isoformat()
    _write_records(tmp_path, [
        {"ts": "2026-11-03T09:00:00", "provider": "claude",
         "model": "m1", "input_tokens": 10, "output_tokens": 5,
         "total_tokens": 15, "session_id": "main"},
        {"ts": "2026-11-03T10:00:00", "provider": "claude",
         "model": "m1", "input_tokens": 20, "output_tokens": 7,
         "total_tokens": 27, "session_id": "main"},
        {"ts": "2026-11-02T10:00:00", "provider": "claude-code",
         "model": "", "input_tokens": None, "output_tokens": None,
         "total_tokens": None, "session_id": "main"},
        # 40 日前の記録は合計と頭脳別には入るが、日別（直近30日）には入らない
        {"ts": f"{old_day}T10:00:00", "provider": "gemini",
         "model": "g1", "input_tokens": 100, "output_tokens": 50,
         "total_tokens": 150, "session_id": "main"},
    ])
    with open(tmp_path / "usage.jsonl", "a", encoding="utf-8") as f:
        f.write("壊れた行\n")

    summary = summarize_usage(tmp_path, now=now)
    assert summary.total.calls == 4
    assert summary.total.input_tokens == 130
    assert summary.total.output_tokens == 62
    assert summary.total.total_tokens == 192
    by_key = {(p, m): t for p, m, t in summary.by_provider}
    assert by_key[("claude", "m1")].calls == 2
    assert by_key[("claude", "m1")].total_tokens == 42
    assert by_key[("claude-code", "")].calls == 1
    assert not by_key[("claude-code", "")].has_tokens
    assert by_key[("gemini", "g1")].total_tokens == 150
    assert [day for day, _ in summary.by_day] == ["2026-11-02", "2026-11-03"]
    day_totals = dict(summary.by_day)
    assert day_totals["2026-11-03"].calls == 2
    assert day_totals["2026-11-02"].calls == 1

    payload = summary.to_dict()
    assert payload["calls"] == 4
    assert payload["by_day"][0]["date"] == "2026-11-02"
    assert payload["by_provider"][0]["provider"] == "claude"

    text = format_usage_summary(summary)
    assert "呼び出し回数: 4 回" in text
    assert "claude / m1" in text
    assert "トークン数は記録されていません" in text  # CLI 系の行
    assert "2026-11-03" in text


def test_summarize_usage_without_records(tmp_path):
    summary = summarize_usage(tmp_path)
    assert summary.total.calls == 0
    assert summary.by_provider == []
    assert summary.by_day == []


# ------------------------------------------------------------- Web

@pytest.fixture()
def server(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _UsageModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get_json(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_usage_api_returns_summary(server):
    base, tmp_path = server
    _write_records(tmp_path, [
        {"ts": datetime.now().isoformat(timespec="seconds"),
         "provider": "muse", "model": "muse-wonder-1.0",
         "input_tokens": 30, "output_tokens": 12, "total_tokens": 42,
         "session_id": "main"},
    ])
    status, data = _get_json(base + "/api/usage")
    assert status == 200
    assert data["tenant"] == "default" and data["user"] == "default"
    assert data["calls"] == 1
    assert data["total_tokens"] == 42
    assert data["by_provider"][0]["provider"] == "muse"
    assert len(data["by_day"]) == 1

    # 記録の無い組み合わせは 0 件で返る
    status, data = _get_json(base + "/api/usage?tenant=acme&user=bob")
    assert status == 200
    assert data["calls"] == 0
    assert data["by_provider"] == []


def test_usage_api_reflects_conversation(server):
    base, tmp_path = server
    with urllib.request.urlopen(
            urllib.request.Request(
                base + "/api/chat",
                data=json.dumps({"message": "こんにちは"}).encode("utf-8"),
                headers={"Content-Type": "application/json"}),
            timeout=10) as resp:
        assert resp.status == 200
    status, data = _get_json(base + "/api/usage")
    assert status == 200
    assert data["calls"] == 1
    assert data["input_tokens"] == 10


def test_page_has_usage_panel(server):
    base, _tmp_path = server
    with urllib.request.urlopen(base + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert 'id="usageBtn"' in html
    assert ">使用量</button>" in html
    assert 'id="usagePanel"' in html


# ------------------------------------------------------------- CLI

def test_cli_usage_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    _write_records(tmp_path, [
        {"ts": "2026-11-03T09:00:00", "provider": "gemini",
         "model": "g1", "input_tokens": 10, "output_tokens": 5,
         "total_tokens": 15, "session_id": "main"},
    ])
    assert cli.main(["usage"]) == 0
    out = capsys.readouterr().out
    assert "呼び出し回数: 1 回" in out
    assert "gemini / g1" in out
    assert "合計 15" in out


def test_cli_usage_command_without_records(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    assert cli.main(["usage"]) == 0
    assert "使用量の記録はまだありません" in capsys.readouterr().out


def test_cli_usage_command_scoped_by_user(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    bob_dir = tmp_path / "users" / "bob"
    bob_dir.mkdir(parents=True)
    _write_records(bob_dir, [
        {"ts": "2026-11-03T09:00:00", "provider": "muse",
         "model": "m", "input_tokens": 1, "output_tokens": 2,
         "total_tokens": 3, "session_id": "main"},
    ])
    assert cli.main(["usage", "--user", "bob"]) == 0
    out = capsys.readouterr().out
    assert "呼び出し回数: 1 回" in out
    # 既定ユーザーでは記録が無い
    assert cli.main(["usage"]) == 0
    assert "使用量の記録はまだありません" in capsys.readouterr().out
