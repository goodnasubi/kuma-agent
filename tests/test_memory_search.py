"""長期記憶の検索（分割・照合・道具・注入の切り替え・CLI）のテスト。

MEMORY.md を項目単位に割り（開始行を保持）、キーワードの部分一致で
点数付けして返すこと、道具 search_memory としてモデルの道具ループに
載ること、記憶が閾値を超えたら注入が新しい方の項目だけに切り替わり
案内文が付くことを確認する。
"""
from pathlib import Path

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import (Memory, memory_inject_text,
                               search_memory_text, split_memory_segments)
from kuma_agent.session import Session
from kuma_agent.tools import activity_label


# ---------------------------------------------------------------- 分割

SAMPLE = """\
# 好み
- 好きな食べ物は寿司
- 好きな飲み物は緑茶

# 予定の決まり
- 会議は月曜の朝
  9時から始める
"""


def test_split_segments_keep_line_numbers():
    segments = split_memory_segments(SAMPLE)
    assert [s.line for s in segments] == [1, 2, 3, 5, 6]
    assert segments[0].text == "# 好み"
    assert segments[1].text == "- 好きな食べ物は寿司"
    # 続き行は直前の項目に取り込まれる
    assert segments[4].text == "- 会議は月曜の朝\n  9時から始める"


def test_split_plain_lines_and_empty():
    assert split_memory_segments("") == []
    assert split_memory_segments("\n\n") == []
    segments = split_memory_segments("ただの文章\n次の行\n")
    # 見出しでも箇条書きでもない行は、1 つ目の項目とその続きになる
    assert len(segments) == 1
    assert segments[0].line == 1
    assert segments[0].text == "ただの文章\n次の行"


# ---------------------------------------------------------------- 検索

def test_search_ranked_prefers_more_matching_terms():
    text = ("- 寿司が好き\n"
            "- ラーメンも好き\n"
            "- 寿司とラーメンの店を開きたい\n")
    hits = search_memory_text(text, "寿司 ラーメン")
    # 両方の語に一致する断片が最上位
    assert hits[0].line == 3
    assert {h.line for h in hits} == {1, 2, 3}


def test_search_japanese_substring_and_punctuation_split():
    mem = "- 好きな食べ物は寿司です\n- 仕事はエンジニア\n"
    # 語の途中への部分一致（分かち書きなし）で当たる
    hits = search_memory_text(mem, "食べ物")
    assert [h.line for h in hits] == [1]
    # 句読点で区切っても複数語として扱う
    hits = search_memory_text(mem, "寿司、エンジニア")
    assert [h.line for h in hits] == [1, 2]


def test_search_no_hits_and_via_memory_class(tmp_path: Path):
    assert search_memory_text("- 寿司が好き", "ラーメン") == []
    assert search_memory_text("- 寿司が好き", "  ") == []
    mem = Memory(tmp_path)
    mem.remember("好きな食べ物は寿司")
    mem.remember("好きな飲み物は緑茶")
    hits = mem.search_ranked("緑茶")
    assert len(hits) == 1
    assert hits[0].line == 2
    assert "緑茶" in hits[0].text
    # limit で件数を絞る
    assert len(mem.search_ranked("好きな", limit=1)) == 1


def test_activity_label_search_memory():
    assert activity_label("search_memory") == "記憶を検索中"


# ---------------------------------------------------------------- 道具

class _SearchModel:
    """1 回目で search_memory を呼び、2 回目で最終応答を返すスタブ。"""

    def __init__(self) -> None:
        self.calls = 0
        self.seen_tools: list[dict] = []

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        self.seen_tools = tools
        if self.calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": "search_memory",
                 "args": {"query": "寿司"}}])
        return Message("assistant", "思い出しました")


def _make_session(tmp_path: Path, monkeypatch, **cfg_kwargs) -> Session:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "MEMORY.md").write_text(
        "- 好きな食べ物は寿司\n- 好きな飲み物は緑茶\n", encoding="utf-8")
    cfg = Config(data_dir=tmp_path, **cfg_kwargs)
    return Session(cfg, _SearchModel())


def test_session_model_can_call_search_memory(tmp_path: Path, monkeypatch):
    session = _make_session(tmp_path, monkeypatch)
    assert session.say("好きな食べ物なんだっけ") == "思い出しました"
    names = [t["name"] for t in session.tools.schemas()]
    assert "search_memory" in names
    # 道具の結果が出どころつきで履歴に戻っている
    tool_results = [m.content for m in session.history if m.role == "tool"]
    assert len(tool_results) == 1
    assert "MEMORY.md の 1 行目" in tool_results[0]
    assert "寿司" in tool_results[0]
    assert "緑茶" not in tool_results[0]


def test_search_memory_tool_not_offered_in_wall_mode(tmp_path: Path,
                                                      monkeypatch):
    session = _make_session(tmp_path, monkeypatch, mode="wall")
    names = [t["name"] for t in session.tools.schemas()]
    assert "search_memory" not in names
    # 道具が無いのでモデルが呼んでも unknown tool として返る
    assert session.tools.call("search_memory", {"query": "寿司"}) \
        .startswith("error: unknown tool")


def test_search_memory_tool_no_hits(tmp_path: Path, monkeypatch):
    session = _make_session(tmp_path, monkeypatch)
    assert session.tools.call("search_memory", {"query": "ラーメン"}) \
        == "見つかりませんでした"


# ---------------------------------------------------------------- 注入

def _big_memory_text() -> str:
    # 各項目は「- 項目N：」＋ あ 20 文字（26〜27 文字）
    return "".join(f"- 項目{i}：" + "あ" * 20 + "\n" for i in range(1, 7))


def test_memory_inject_text_full_when_small():
    text = "- 寿司が好き\n"
    assert memory_inject_text(text, 6000) == (text, False)


def test_memory_inject_text_partial_keeps_whole_segments():
    text = _big_memory_text()
    injected, partial = memory_inject_text(text, 60)
    assert partial is True
    # 末尾側の 2 項目だけが項目単位で残る（途中で切れない）
    assert "項目6" in injected and "項目5" in injected
    assert "項目4" not in injected
    for line in injected.splitlines():
        assert line.startswith("- 項目")


def test_memory_inject_text_keeps_last_segment_even_if_over_limit():
    text = "- 短い\n- " + "あ" * 100 + "\n"
    injected, partial = memory_inject_text(text, 10)
    assert partial is True
    # 最後の 1 項目は制限を超えても丸ごと残す
    assert "あ" * 100 in injected
    assert "短い" not in injected


def test_session_injects_full_memory_below_threshold(tmp_path: Path,
                                                     monkeypatch):
    session = _make_session(tmp_path, monkeypatch)
    assert "好きな食べ物は寿司" in session.system_prompt
    assert "好きな飲み物は緑茶" in session.system_prompt
    assert "一部のみ表示" not in session.system_prompt


def test_session_injects_partial_memory_above_threshold(tmp_path: Path,
                                                        monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "MEMORY.md").write_text(_big_memory_text(), encoding="utf-8")
    cfg = Config(data_dir=tmp_path, memory_full_inject_chars=60)
    session = Session(cfg, _SearchModel())
    # 新しい方の項目だけが載り、古い方は載らない。案内文が付く
    assert "項目6" in session.system_prompt
    assert "項目1" not in session.system_prompt
    assert "一部のみ表示しています" in session.system_prompt
    assert "search_memory" in session.system_prompt


def test_session_partial_note_in_wall_mode_omits_tool_name(tmp_path: Path,
                                                           monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "MEMORY.md").write_text(_big_memory_text(), encoding="utf-8")
    cfg = Config(data_dir=tmp_path, mode="wall", memory_full_inject_chars=60)
    session = Session(cfg, _SearchModel())
    assert "一部のみ表示しています" in session.system_prompt
    assert "search_memory" not in session.system_prompt


def test_memory_threshold_from_env(monkeypatch):
    monkeypatch.setenv("KUMA_MEMORY_FULL_INJECT_CHARS", "123")
    assert Config().memory_full_inject_chars == 123
    monkeypatch.delenv("KUMA_MEMORY_FULL_INJECT_CHARS")
    assert Config().memory_full_inject_chars == 6000


# ---------------------------------------------------------------- CLI

def _setup_cli_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KUMA_TENANT", raising=False)
    monkeypatch.delenv("KUMA_USER", raising=False)
    # API キーが無くても動く経路であること
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)


def test_cli_memory_search(monkeypatch, tmp_path: Path, capsys):
    (tmp_path / "MEMORY.md").write_text(
        "- 好きな食べ物は寿司\n- 好きな飲み物は緑茶\n", encoding="utf-8")
    _setup_cli_env(monkeypatch, tmp_path)

    assert cli.main(["memory-search", "寿司"]) == 0
    out = capsys.readouterr().out
    assert "MEMORY.md の 1 行目" in out
    assert "寿司" in out
    assert "緑茶" not in out

    assert cli.main(["memory-search", "存在しない語"]) == 0
    assert "見つかりませんでした" in capsys.readouterr().out


def test_cli_memory_search_tenant(monkeypatch, tmp_path: Path, capsys):
    acme_dir = tmp_path / "tenants" / "acme"
    acme_dir.mkdir(parents=True)
    (acme_dir / "MEMORY.md").write_text("- acme だけの事実\n",
                                        encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("- 既定の事実\n", encoding="utf-8")
    _setup_cli_env(monkeypatch, tmp_path)

    assert cli.main(["memory-search", "事実", "--tenant", "acme"]) == 0
    out = capsys.readouterr().out
    assert "acme だけの事実" in out
    assert "既定の事実" not in out
