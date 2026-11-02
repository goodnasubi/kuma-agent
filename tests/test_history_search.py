"""過去の会話の横断検索（照合・道具・CLI）のテスト。

現在のユーザーの置き場にある全セッションの会話記録
（conversations/*.jsonl、委任の子の記録も含む）を発話単位で
キーワード検索し、セッション名・日時・話者・抜粋つきで返すこと、
道具 search_conversations がモードを問わず登録されること、CLI の
kuma-agent search-history でも同じ検索ができることを確認する。
"""
import json
from pathlib import Path

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import (Memory, format_conversation_hit,
                               search_conversation_records)
from kuma_agent.session import Session
from kuma_agent.tools import activity_label


def _write_records(data_dir: Path, session: str, records: list[dict]) -> None:
    conv = data_dir / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    with (conv / f"{session}.jsonl").open("a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _seed(data_dir: Path) -> None:
    _write_records(data_dir, "old", [
        {"ts": "2026-01-01T10:00:00", "session": "old", "role": "user",
         "content": "寿司が好き"},
    ])
    _write_records(data_dir, "mid", [
        {"ts": "2026-01-15T10:00:00", "session": "mid", "role": "user",
         "content": "寿司 寿司 寿司"},
    ])
    _write_records(data_dir, "new", [
        {"ts": "2026-02-01T10:00:00", "session": "new", "role": "assistant",
         "content": "寿司とラーメン、どちらも寿司が一番"},
    ])


# ---------------------------------------------------------------- 検索

def test_search_across_sessions_and_order(tmp_path: Path):
    _seed(tmp_path)
    hits = search_conversation_records(tmp_path, "寿司 ラーメン")
    # 両方の語に一致する発話が最上位、次に出現回数の多い発話
    assert [h.session for h in hits] == ["new", "mid", "old"]
    assert hits[0].role == "assistant"
    assert hits[0].ts == "2026-02-01T10:00:00"


def test_search_recency_breaks_ties(tmp_path: Path):
    _write_records(tmp_path, "a", [
        {"ts": "2026-01-02T10:00:00", "session": "a", "role": "user",
         "content": "寿司が好き"},
    ])
    _write_records(tmp_path, "b", [
        {"ts": "2026-01-03T10:00:00", "session": "b", "role": "user",
         "content": "寿司が好き"},
    ])
    hits = search_conversation_records(tmp_path, "寿司")
    # 語数も出現回数も同じなら新しい発話が先
    assert [h.session for h in hits] == ["b", "a"]


def test_search_includes_delegate_records(tmp_path: Path):
    _write_records(tmp_path, "delegate-20260101-1", [
        {"ts": "2026-01-01T10:00:00", "session": "delegate-20260101-1",
         "role": "assistant", "content": "調査の結果、寿司が一番でした"},
    ])
    hits = search_conversation_records(tmp_path, "寿司")
    assert [h.session for h in hits] == ["delegate-20260101-1"]


def test_search_no_hits_and_empty_query(tmp_path: Path):
    _seed(tmp_path)
    assert search_conversation_records(tmp_path, "うどん") == []
    assert search_conversation_records(tmp_path, "  ") == []
    # 会話記録が無い置き場でも落ちない
    assert search_conversation_records(tmp_path / "none", "寿司") == []


def test_search_limit(tmp_path: Path):
    _seed(tmp_path)
    hits = search_conversation_records(tmp_path, "寿司", limit=2)
    assert len(hits) == 2
    assert hits[0].session == "mid"  # 出現回数が一番多い発話


def test_search_skips_broken_lines(tmp_path: Path):
    conv = tmp_path / "conversations"
    conv.mkdir(parents=True)
    (conv / "main.jsonl").write_text(
        "これは JSON ではない行\n"
        "[1, 2, 3]\n"
        '{"role": "user"}\n'  # content が無い
        '{"role": "user", "content": 42}\n'  # content が文字列でない
        '{"ts": "2026-01-01T10:00:00", "role": "user",'
        ' "content": "寿司の話"}\n',
        encoding="utf-8")
    # 読めないファイル（ディレクトリ）があっても検索は続く
    (conv / "broken.jsonl").mkdir()
    hits = search_conversation_records(tmp_path, "寿司")
    assert len(hits) == 1
    assert hits[0].excerpt == "寿司の話"


def test_search_record_without_ts(tmp_path: Path):
    _write_records(tmp_path, "main", [
        {"role": "user", "content": "日時の無い寿司の発話"},
    ])
    hits = search_conversation_records(tmp_path, "寿司")
    assert len(hits) == 1
    assert hits[0].ts == ""
    # 日時が無いヒットは日時の部分を省いて整形される
    assert format_conversation_hit(hits[0]) == \
        "（main、利用者）日時の無い寿司の発話"


def test_format_hit_shape(tmp_path: Path):
    _seed(tmp_path)
    hits = search_conversation_records(tmp_path, "寿司 ラーメン")
    assert format_conversation_hit(hits[0]) == \
        "（new、2026-02-01 10:00:00、クマ）寿司とラーメン、どちらも寿司が一番"
    assert format_conversation_hit(hits[2]) == \
        "（old、2026-01-01 10:00:00、利用者）寿司が好き"


def test_long_content_excerpt_is_trimmed_around_match(tmp_path: Path):
    content = "あ" * 150 + "寿司" + "い" * 150
    _write_records(tmp_path, "main", [
        {"ts": "2026-01-01T10:00:00", "session": "main", "role": "user",
         "content": content},
    ])
    hits = search_conversation_records(tmp_path, "寿司")
    assert len(hits) == 1
    excerpt = hits[0].excerpt
    assert "寿司" in excerpt
    assert excerpt.startswith("…") and excerpt.endswith("…")
    assert len(excerpt) <= 202  # 目安 200 字＋前後の「…」


def test_memory_method_searches_all_sessions(tmp_path: Path):
    _seed(tmp_path)
    memory = Memory(tmp_path, "old")  # セッション old の Memory でも
    hits = memory.search_conversations("ラーメン")
    # 他セッション（new）の発話が見つかる
    assert [h.session for h in hits] == ["new"]


def test_activity_label_search_conversations():
    assert activity_label("search_conversations") == "過去の会話を検索中"


# ---------------------------------------------------------------- 道具

class _HistoryModel:
    """1 回目で search_conversations を呼び、2 回目で最終応答を返すスタブ。"""

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": "search_conversations",
                 "args": {"query": "寿司"}}])
        return Message("assistant", "前に話していましたね")


def _make_session(tmp_path: Path, monkeypatch, **cfg_kwargs) -> Session:
    monkeypatch.chdir(tmp_path)
    _seed(tmp_path)
    cfg = Config(data_dir=tmp_path, **cfg_kwargs)
    return Session(cfg, _HistoryModel())


def test_tool_registered_and_roundtrip_in_ledger_mode(tmp_path: Path,
                                                      monkeypatch):
    session = _make_session(tmp_path, monkeypatch)
    names = [t["name"] for t in session.tools.schemas()]
    assert "search_conversations" in names
    assert session.say("前に寿司の話したっけ") == "前に話していましたね"
    tool_results = [m.content for m in session.history if m.role == "tool"]
    assert len(tool_results) == 1
    assert "（mid、2026-01-15 10:00:00、利用者）" in tool_results[0]
    assert "寿司" in tool_results[0]


def test_tool_registered_in_wall_mode(tmp_path: Path, monkeypatch):
    session = _make_session(tmp_path, monkeypatch, mode="wall")
    names = [t["name"] for t in session.tools.schemas()]
    # スレッドモードでも会話の検索は使える（用事系とは独立）
    assert "search_conversations" in names
    assert "search_memory" not in names  # 長期記憶の方は従来どおり出ない
    result = session.tools.call(
        "search_conversations", {"query": "ラーメン"})
    assert "new" in result and "ラーメン" in result


def test_tool_no_hits(tmp_path: Path, monkeypatch):
    session = _make_session(tmp_path, monkeypatch)
    assert session.tools.call(
        "search_conversations", {"query": "うどん"}) == "見つかりませんでした"


# ---------------------------------------------------------------- CLI

def test_cli_search_history(monkeypatch, tmp_path: Path, capsys):
    _seed(tmp_path)
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    assert cli.main(["search-history", "寿司 ラーメン"]) == 0
    out = capsys.readouterr().out
    assert "（new、2026-02-01 10:00:00、クマ）" in out
    assert "寿司とラーメン" in out
    assert cli.main(["search-history", "うどん"]) == 0
    assert "見つかりませんでした" in capsys.readouterr().out


def test_cli_search_history_limit(monkeypatch, tmp_path: Path, capsys):
    _seed(tmp_path)
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    assert cli.main(["search-history", "寿司", "--limit", "1"]) == 0
    lines = [line for line in capsys.readouterr().out.splitlines()
             if line.strip()]
    assert len(lines) == 1
    assert "（mid、" in lines[0]
