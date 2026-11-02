"""長期記憶の整理（memory_tidy）のテスト。

機械的な重複除去（完全重複・空白や記号だけ違う重複が消え、
異なる項目と見出しは残り、順序が保たれる）、モデルによる統合
（スタブ頭脳で置換が適用されバックアップができる、空応答・
極端に短い応答では変更されない）、CLI の --dry-run（本体を
変えず提案ファイルを作る）、道具 tidy_memory の往復、能動発信
の巡回での自動除去（大きい記憶だけ・1 日 1 回）を確認する。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import Memory
from kuma_agent.memory_tidy import (
    AUTO_STATE_FILENAME,
    apply_mechanical_tidy,
    consolidate_memory_text,
    dedupe_memory_text,
    propose_memory_tidy,
    tidy_memory_with_model,
)
from kuma_agent.proactive import check_and_post
from kuma_agent.session import Session
from kuma_agent.tools import activity_label

NOW = datetime(2026, 11, 2, 9, 0, 0)


# ---------------------------------------------------------------- 機械的除去

def test_dedupe_removes_exact_duplicates():
    text = "- 寿司が好き\n- 緑茶が好き\n- 寿司が好き\n"
    tidied, removed = dedupe_memory_text(text)
    assert removed == 1
    assert tidied == "- 寿司が好き\n- 緑茶が好き\n"


def test_dedupe_removes_whitespace_and_symbol_variants():
    text = ("- 寿司が好き\n"
            "-  寿司が好き  \n"
            "* 寿司が好き。\n"
            "1. 寿司が好き\n"
            "- 緑茶が好き\n")
    tidied, removed = dedupe_memory_text(text)
    assert removed == 3
    assert tidied.splitlines() == ["- 寿司が好き", "- 緑茶が好き"]


def test_dedupe_keeps_different_items_headings_and_order():
    text = ("# 好み\n"
            "- 寿司が好き\n"
            "- ラーメンも好き\n"
            "# 好み\n"
            "- 緑茶が好き\n"
            "- 寿司が好き\n")
    tidied, removed = dedupe_memory_text(text)
    assert removed == 1
    lines = tidied.splitlines()
    # 見出し行そのものは対象にしない（重複した見出しも残る）
    assert lines.count("# 好み") == 2
    # 異なる項目は残り、順序は保たれる
    assert lines == ["# 好み", "- 寿司が好き", "- ラーメンも好き",
                     "# 好み", "- 緑茶が好き"]


def test_dedupe_no_duplicates_returns_original():
    text = "- 寿司が好き\n\n- 緑茶が好き\n"
    tidied, removed = dedupe_memory_text(text)
    assert removed == 0
    assert tidied == text


def test_dedupe_multiline_segment():
    text = ("- 会議は月曜の朝\n  9時から始める\n"
            "- 会議は月曜の朝\n  9時から始める\n")
    tidied, removed = dedupe_memory_text(text)
    assert removed == 1
    assert "9時から始める" in tidied


def test_apply_mechanical_tidy_writes_backup(tmp_path: Path):
    memory = Memory(tmp_path)
    (tmp_path / "MEMORY.md").write_text(
        "- 寿司が好き\n- 寿司が好き\n", encoding="utf-8")
    result = apply_mechanical_tidy(memory)
    assert result.applied is True
    assert result.removed_count == 1
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 寿司が好き\n"
    backups = list(tmp_path.glob("MEMORY.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") \
        == "- 寿司が好き\n- 寿司が好き\n"


def test_apply_mechanical_tidy_no_change_no_backup(tmp_path: Path):
    memory = Memory(tmp_path)
    (tmp_path / "MEMORY.md").write_text("- 寿司が好き\n", encoding="utf-8")
    result = apply_mechanical_tidy(memory)
    assert result.applied is False
    assert list(tmp_path.glob("MEMORY.md.bak-*")) == []


# ---------------------------------------------------------------- モデル統合

CONSOLIDATED = "# 好み\n- 寿司と緑茶が好き\n- 仕事はエンジニアです\n"


class _ConsolidatingModel:
    """長期記憶の全文を受けて整理後の全文を返すスタブ頭脳。"""

    def __init__(self, reply: str = CONSOLIDATED) -> None:
        self.reply = reply
        self.calls: list = []

    def chat(self, system_prompt, messages, tools):
        self.calls.append((system_prompt, messages, tools))
        return Message("assistant", self.reply)


ORIGINAL = ("# 好み\n"
            "- 寿司が好き\n"
            "- 寿司が好き\n"
            "- 緑茶が好き\n"
            "- 仕事はエンジニア\n")


def test_consolidate_prompt_includes_safeguards():
    model = _ConsolidatingModel()
    result = consolidate_memory_text(model, ORIGINAL)
    assert result == CONSOLIDATED
    system_prompt, messages, tools = model.calls[0]
    # 道具なしの単発 chat で、全文を渡している
    assert tools == []
    assert messages[0].content == ORIGINAL
    # 統合用の指示に安全策が含まれている
    assert "事実・日付・固有名詞を落とさない" in system_prompt
    assert "書かれていないことを補わない" in system_prompt
    assert "形式を保つ" in system_prompt


def test_tidy_with_model_applies_and_backs_up(tmp_path: Path):
    memory = Memory(tmp_path)
    (tmp_path / "MEMORY.md").write_text(ORIGINAL, encoding="utf-8")
    result = tidy_memory_with_model(memory, _ConsolidatingModel())
    assert result.applied is True
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == CONSOLIDATED
    assert result.backup_path is not None
    assert result.backup_path.name.startswith("MEMORY.md.bak-")
    assert result.backup_path.read_text(encoding="utf-8") == ORIGINAL
    assert result.before_lines == 5
    assert result.after_lines == 3


def test_tidy_with_model_empty_response_changes_nothing(tmp_path: Path):
    memory = Memory(tmp_path)
    (tmp_path / "MEMORY.md").write_text(ORIGINAL, encoding="utf-8")
    result = tidy_memory_with_model(memory, _ConsolidatingModel(reply="  "))
    assert result.applied is False
    assert result.reason == "model_failed"
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == ORIGINAL
    assert list(tmp_path.glob("MEMORY.md.bak-*")) == []


def test_tidy_with_model_too_short_response_changes_nothing(tmp_path: Path):
    memory = Memory(tmp_path)
    (tmp_path / "MEMORY.md").write_text(ORIGINAL, encoding="utf-8")
    # 元の 2 割未満の応答は失敗として扱う
    result = tidy_memory_with_model(memory, _ConsolidatingModel(reply="- 寿司"))
    assert result.applied is False
    assert result.reason == "model_failed"
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == ORIGINAL
    assert list(tmp_path.glob("MEMORY.md.bak-*")) == []


def test_tidy_no_memory_is_noop(tmp_path: Path):
    memory = Memory(tmp_path)
    result = tidy_memory_with_model(memory, _ConsolidatingModel())
    assert result.applied is False
    assert result.reason == "empty"
    assert not (tmp_path / "MEMORY.md").exists()


def test_propose_writes_proposed_file_only(tmp_path: Path):
    memory = Memory(tmp_path)
    (tmp_path / "MEMORY.md").write_text(ORIGINAL, encoding="utf-8")
    result = propose_memory_tidy(memory, _ConsolidatingModel())
    assert result.applied is False
    assert result.proposed_path == tmp_path / "MEMORY.md.proposed"
    assert (tmp_path / "MEMORY.md.proposed").read_text(encoding="utf-8") \
        == CONSOLIDATED
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == ORIGINAL
    assert list(tmp_path.glob("MEMORY.md.bak-*")) == []


# ---------------------------------------------------------------- CLI

def _setup_cli_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KUMA_TENANT", raising=False)
    monkeypatch.delenv("KUMA_USER", raising=False)
    monkeypatch.setenv("META_MODEL_API_KEY", "dummy-key")
    monkeypatch.setattr(cli, "create_client",
                        lambda provider, api_key, model=None:
                        _ConsolidatingModel())


def test_cli_memory_tidy_dry_run(monkeypatch, tmp_path: Path, capsys):
    (tmp_path / "MEMORY.md").write_text(ORIGINAL, encoding="utf-8")
    _setup_cli_env(monkeypatch, tmp_path)

    assert cli.main(["memory-tidy", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "差分" in out and "行" in out
    # 本体は変わらず、提案ファイルだけが作られる
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == ORIGINAL
    assert (tmp_path / "MEMORY.md.proposed").read_text(encoding="utf-8") \
        == CONSOLIDATED
    assert list(tmp_path.glob("MEMORY.md.bak-*")) == []


def test_cli_memory_tidy_applies(monkeypatch, tmp_path: Path, capsys):
    (tmp_path / "MEMORY.md").write_text(ORIGINAL, encoding="utf-8")
    _setup_cli_env(monkeypatch, tmp_path)

    assert cli.main(["memory-tidy"]) == 0
    out = capsys.readouterr().out
    assert "バックアップ" in out
    assert "5 行 → 3 行" in out
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == CONSOLIDATED
    backups = list(tmp_path.glob("MEMORY.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == ORIGINAL


def test_cli_memory_tidy_no_memory(monkeypatch, tmp_path: Path, capsys):
    _setup_cli_env(monkeypatch, tmp_path)
    assert cli.main(["memory-tidy"]) == 0
    assert "ありません" in capsys.readouterr().out


def test_cli_memory_tidy_requires_api_key(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    # API キーが無いコマンドとしては配置されていない（キー必須）
    assert cli.main(["memory-tidy", "--dry-run"]) == 2


# ---------------------------------------------------------------- 道具

class _SessionModel:
    """整理の chat には統合結果を、それ以外には通常応答を返すスタブ。"""

    def chat(self, system_prompt, messages, tools):
        if tools == [] and "長期記憶" in system_prompt \
                and "整理者" in system_prompt:
            return Message("assistant", CONSOLIDATED)
        return Message("assistant", "はい")


def _make_session(tmp_path: Path, monkeypatch, **cfg_kwargs) -> Session:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "MEMORY.md").write_text(ORIGINAL, encoding="utf-8")
    cfg = Config(data_dir=tmp_path, **cfg_kwargs)
    return Session(cfg, _SessionModel())


def test_tidy_memory_tool_roundtrip(tmp_path: Path, monkeypatch):
    session = _make_session(tmp_path, monkeypatch)
    names = [t["name"] for t in session.tools.schemas()]
    assert "tidy_memory" in names
    assert activity_label("tidy_memory") == "長期記憶を整理中"

    result = session.tools.call("tidy_memory", {})
    assert result == ("5行→3行に整理しました"
                      f"（バックアップ: "
                      f"{list(tmp_path.glob('MEMORY.md.bak-*'))[0].name}）")
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == CONSOLIDATED
    backups = list(tmp_path.glob("MEMORY.md.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == ORIGINAL


def test_tidy_memory_tool_not_offered_in_wall_mode(tmp_path: Path,
                                                    monkeypatch):
    session = _make_session(tmp_path, monkeypatch, mode="wall")
    names = [t["name"] for t in session.tools.schemas()]
    assert "tidy_memory" not in names
    assert session.tools.call("tidy_memory", {}) \
        .startswith("error: unknown tool")


# ---------------------------------------------------------------- 自動除去

def _big_memory_with_dup() -> str:
    # 全文注入の上限（テストでは 10 文字にする）の 3 倍を十分超える
    return ("- 好きな食べ物は寿司です\n"
            "- 好きな飲み物は緑茶です\n"
            "- 好きな食べ物は寿司です\n"
            "- 仕事はエンジニアです\n")


def test_auto_tidy_in_sweep_removes_duplicates(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text(_big_memory_with_dup(),
                                        encoding="utf-8")
    cfg = Config(data_dir=tmp_path, memory_full_inject_chars=10)
    # 用事は無いので発言はしないが、自動除去だけが働く
    assert check_and_post(cfg, now=NOW) is None
    text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert text.count("好きな食べ物は寿司です") == 1
    assert "好きな飲み物は緑茶です" in text
    # バックアップが残り、実施日が状態ファイルに記録される
    backups = list(tmp_path.glob("MEMORY.md.bak-*"))
    assert len(backups) == 1
    state = json.loads(
        (tmp_path / AUTO_STATE_FILENAME).read_text(encoding="utf-8"))
    assert state == {"last_auto_tidy_on": "2026-11-02"}
    # 会話への投稿はしていない
    assert not (tmp_path / "conversations" / "main.jsonl").exists()


def test_auto_tidy_skips_small_memory(tmp_path: Path):
    small = "- 寿司が好き\n- 寿司が好き\n"
    (tmp_path / "MEMORY.md").write_text(small, encoding="utf-8")
    # 既定の上限（6000）の 3 倍には遠く及ばない
    cfg = Config(data_dir=tmp_path)
    assert check_and_post(cfg, now=NOW) is None
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == small
    assert list(tmp_path.glob("MEMORY.md.bak-*")) == []
    assert not (tmp_path / AUTO_STATE_FILENAME).exists()


def test_auto_tidy_once_a_day(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text(_big_memory_with_dup(),
                                        encoding="utf-8")
    cfg = Config(data_dir=tmp_path, memory_full_inject_chars=10)
    check_and_post(cfg, now=NOW)
    assert len(list(tmp_path.glob("MEMORY.md.bak-*"))) == 1

    # 同じ日に新しい重複が増えても、その日はもう除去しない
    with (tmp_path / "MEMORY.md").open("a", encoding="utf-8") as f:
        f.write("- 好きな飲み物は緑茶です\n")
    check_and_post(cfg, now=NOW + timedelta(hours=3))
    text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert text.count("好きな飲み物は緑茶です") == 2
    assert len(list(tmp_path.glob("MEMORY.md.bak-*"))) == 1

    # 翌日になれば再び除去される
    check_and_post(cfg, now=NOW + timedelta(days=1))
    text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert text.count("好きな飲み物は緑茶です") == 1
    assert len(list(tmp_path.glob("MEMORY.md.bak-*"))) == 2
