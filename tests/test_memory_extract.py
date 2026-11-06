"""長期記憶の自動抽出（memory_extract）のテスト。

新しい会話から箇条書きが MEMORY.md に足され控えが残ること、
処理済み位置より前は再抽出されないこと、「なし」・失敗では
変わらないこと、能動発信の投稿・要約・委任の子の会話が材料に
入らないこと、巡回では 1 日 1 回まで・モデルなし経路と
KUMA_MEMORY_EXTRACT=0 では動かないこと、CLI の dry-run が
書かずに表示することを確認する。モデルはスタブで差し替え、
ネットワークや実モデルには出ない。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import Memory
from kuma_agent.memory_extract import (
    STATE_FILENAME,
    auto_extract_memory,
    extract_memory,
)
from kuma_agent.proactive import check_and_post
from kuma_agent.session import Session

NOW = datetime(2026, 11, 2, 9, 0, 0)

BULLETS = "- 猫を飼っている\n- 仕事はエンジニア"


class _ExtractingModel:
    """会話を受けて決まった箇条書きを返すスタブ頭脳。"""

    def __init__(self, reply: str = BULLETS,
                 exc: Exception | None = None) -> None:
        self.reply = reply
        self.exc = exc
        self.calls: list = []

    def chat(self, system_prompt, messages, tools):
        self.calls.append((system_prompt, messages, tools))
        if self.exc is not None:
            raise self.exc
        return Message("assistant", self.reply)


def _write_conversation(user_dir: Path, turns: list[tuple[str, str]],
                        session_id: str = "main") -> None:
    memory = Memory(user_dir, session_id)
    for role, content in turns:
        memory.append_turn(role, content)


def _state(user_dir: Path) -> dict:
    path = user_dir / STATE_FILENAME
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _prompt_of(model: _ExtractingModel, index: int = 0) -> str:
    return model.calls[index][1][0].content


def _extract_call_count(model: _ExtractingModel) -> int:
    """抽出の呼び出しだけを数える。

    巡回では手順書の自動学習（skill_learn）も同じモデルを呼ぶ
    ため、抽出の回数は指示文で区別して数える。
    """
    return sum(1 for system, _messages, _tools in model.calls
               if "抽出者" in system)


# ---------------------------------------------------------------- 抽出の本体

def test_extract_appends_bullets_and_backs_up(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("- 寿司が好き\n", encoding="utf-8")
    _write_conversation(tmp_path, [
        ("user", "猫を飼い始めたんだ"),
        ("assistant", "いいですね。名前は決めましたか？"),
    ])
    model = _ExtractingModel()
    result = extract_memory(Memory(tmp_path), model, now=NOW)

    assert result.applied is True
    assert result.added_count == 2
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == (
        "- 寿司が好き\n- 猫を飼っている\n- 仕事はエンジニア\n")
    # 控えに書き込み前の本文が残る
    assert result.backup_path is not None
    assert result.backup_path.name.startswith("MEMORY.md.bak-")
    assert result.backup_path.read_text(encoding="utf-8") == "- 寿司が好き\n"
    # 道具なしの単発 chat で、現在の記憶と新しい会話を渡している
    system_prompt, _messages, tools = model.calls[0]
    assert tools == []
    assert "すでに長期記憶に書いてある内容は繰り返さない" in system_prompt
    prompt = _prompt_of(model)
    assert "- 寿司が好き" in prompt and "猫を飼い始めたんだ" in prompt
    # 処理済み位置が記録される
    assert _state(tmp_path)["positions"] == {"main": 2}


def test_extract_does_not_reprocess_old_turns(tmp_path: Path):
    _write_conversation(tmp_path, [
        ("user", "猫を飼い始めたんだ"),
        ("assistant", "いいですね"),
    ])
    extract_memory(Memory(tmp_path), _ExtractingModel(), now=NOW)

    _write_conversation(tmp_path, [
        ("user", "犬も飼いたいな"),
        ("assistant", "にぎやかになりますね"),
    ])
    model = _ExtractingModel(reply="- 犬を飼いたい")
    result = extract_memory(Memory(tmp_path), model, now=NOW)
    assert result.applied is True
    prompt = _prompt_of(model)
    # 前回の分は材料に入らず、新しい分だけが渡る
    assert "猫を飼い始めたんだ" not in prompt
    assert "犬も飼いたいな" in prompt
    text = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "- 猫を飼っている" in text and "- 犬を飼いたい" in text


def test_extract_nothing_when_model_says_none(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("- 寿司が好き\n", encoding="utf-8")
    _write_conversation(tmp_path, [("user", "こんにちは"),
                                   ("assistant", "こんにちは")])
    result = extract_memory(Memory(tmp_path),
                            _ExtractingModel(reply="なし"), now=NOW)
    assert result.applied is False
    assert result.reason == "nothing"
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 寿司が好き\n"
    assert list(tmp_path.glob("MEMORY.md.bak-*")) == []
    # 位置は進むので、同じ分は二度と材料にならない
    assert _state(tmp_path)["positions"] == {"main": 2}
    again = extract_memory(Memory(tmp_path), _ExtractingModel(), now=NOW)
    assert again.reason == "no_new_turns"


def test_extract_failure_changes_nothing_and_keeps_position(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("- 寿司が好き\n", encoding="utf-8")
    _write_conversation(tmp_path, [("user", "猫を飼い始めたんだ")])
    model = _ExtractingModel(exc=RuntimeError("boom"))
    result = extract_memory(Memory(tmp_path), model, now=NOW)
    assert result.applied is False
    assert result.reason == "model_failed"
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 寿司が好き\n"
    assert list(tmp_path.glob("MEMORY.md.bak-*")) == []
    # 状態は書かれず、位置も進まない（翌日に同じ分から再挑戦できる）
    assert not (tmp_path / STATE_FILENAME).exists()


def test_extract_rejects_non_bullet_reply(tmp_path: Path):
    _write_conversation(tmp_path, [("user", "猫を飼い始めたんだ")])
    result = extract_memory(
        Memory(tmp_path),
        _ExtractingModel(reply="利用者は猫を飼い始めました。"),
        now=NOW)
    assert result.applied is False
    assert result.reason == "model_failed"
    assert not (tmp_path / "MEMORY.md").exists()


def test_extract_material_excludes_proactive_summary_delegate(
        tmp_path: Path):
    memory = Memory(tmp_path)
    memory.append_turn("user", "来週から京都に引っ越します")
    memory.append_turn("assistant", "引っ越しの準備を手伝いますね")
    memory.append_turn("assistant", "PROACTIVE-MARKER お知らせです",
                       proactive=True)
    memory.append_summary("SUMMARY-MARKER これまでの要約")
    _write_conversation(tmp_path,
                        [("user", "DELEGATE-MARKER 調べておいて")],
                        session_id="delegate-20261102-1")

    model = _ExtractingModel(reply="- 京都に引っ越す")
    result = extract_memory(Memory(tmp_path), model, now=NOW)
    assert result.applied is True
    prompt = _prompt_of(model)
    assert "京都に引っ越します" in prompt
    assert "PROACTIVE-MARKER" not in prompt
    assert "SUMMARY-MARKER" not in prompt
    assert "DELEGATE-MARKER" not in prompt
    # 材料にならない発話も含め、main の全レコードが消費される
    assert _state(tmp_path)["positions"] == {"main": 4}


def test_extract_caps_input_and_advances_partially(tmp_path: Path):
    memory = Memory(tmp_path)
    long_text = "あ" * 4000
    for i in range(5):
        memory.append_turn("user", long_text + str(i))
    model = _ExtractingModel(reply="なし")

    # 1 回では上限（12000 字）に収まる 2 件分だけが処理される
    extract_memory(memory, model, now=NOW)
    material = _prompt_of(model).split("# 新しい会話\n", 1)[1]
    assert len(material) <= 12000
    assert _state(tmp_path)["positions"] == {"main": 2}

    # 残りは次の抽出で順に処理され、最後は全件が消費される
    extract_memory(memory, model, now=NOW)
    assert _state(tmp_path)["positions"] == {"main": 4}
    extract_memory(memory, model, now=NOW)
    assert _state(tmp_path)["positions"] == {"main": 5}
    assert len(model.calls) == 3


def test_extract_truncates_single_oversized_turn(tmp_path: Path):
    memory = Memory(tmp_path)
    memory.append_turn("user", "あ" * 15000)
    model = _ExtractingModel(reply="なし")
    extract_memory(memory, model, now=NOW)
    material = _prompt_of(model).split("# 新しい会話\n", 1)[1]
    assert len(material) == 12000
    # 詰まらないよう、その発話は消費したものとして扱う
    assert _state(tmp_path)["positions"] == {"main": 1}


def test_extract_dry_run_changes_nothing(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("- 寿司が好き\n", encoding="utf-8")
    _write_conversation(tmp_path, [("user", "猫を飼い始めたんだ")])
    result = extract_memory(Memory(tmp_path), _ExtractingModel(),
                            now=NOW, dry_run=True)
    assert result.applied is False
    assert result.bullets == ["- 猫を飼っている", "- 仕事はエンジニア"]
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 寿司が好き\n"
    assert not (tmp_path / STATE_FILENAME).exists()
    assert list(tmp_path.glob("MEMORY.md.bak-*")) == []


# ---------------------------------------------------------------- 自動実行

def test_auto_failure_records_attempt_but_not_positions(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("- 寿司が好き\n", encoding="utf-8")
    _write_conversation(tmp_path, [("user", "猫を飼い始めたんだ")])
    model = _ExtractingModel(exc=RuntimeError("boom"))

    result = auto_extract_memory(tmp_path, lambda: model, now=NOW)
    assert result is not None and result.reason == "model_failed"
    state = _state(tmp_path)
    assert state["last_attempt_on"] == "2026-11-02"
    assert not state.get("positions")
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 寿司が好き\n"

    # 失敗した日は再試行しない
    assert auto_extract_memory(tmp_path, lambda: model, now=NOW) is None
    assert len(model.calls) == 1


def test_auto_without_new_conversation_records_nothing(tmp_path: Path):
    model = _ExtractingModel()
    assert auto_extract_memory(tmp_path, lambda: model, now=NOW) is None
    assert model.calls == []
    assert not (tmp_path / STATE_FILENAME).exists()


def test_sweep_extracts_once_a_day(tmp_path: Path):
    _write_conversation(tmp_path, [
        ("user", "猫を飼い始めたんだ"),
        ("assistant", "いいですね"),
    ])
    model = _ExtractingModel()
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, model)

    # 巡回では投稿はせず、抽出だけが静かに働く
    assert check_and_post(cfg, now=NOW,
                          session_provider=lambda: session) is None
    assert _extract_call_count(model) == 1
    assert "- 猫を飼っている" in \
        (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
    state = _state(tmp_path)
    assert state["last_attempt_on"] == "2026-11-02"
    assert state["positions"] == {"main": 2}

    # 同じ日に新しい会話が増えても、その日はもう抽出しない
    _write_conversation(tmp_path, [("user", "犬も飼いたいな")])
    check_and_post(cfg, now=NOW + timedelta(hours=2),
                   session_provider=lambda: session)
    assert _extract_call_count(model) == 1

    # 翌日になれば再び抽出される
    check_and_post(cfg, now=NOW + timedelta(days=1),
                   session_provider=lambda: session)
    assert _extract_call_count(model) == 2


def test_sweep_without_model_does_not_extract(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("- 寿司が好き\n", encoding="utf-8")
    _write_conversation(tmp_path, [("user", "猫を飼い始めたんだ")])
    cfg = Config(data_dir=tmp_path)
    # session_provider なし＝モデルなしの一時 Session の経路。
    # 抽出は実行されず、試行の記録も残らない
    assert check_and_post(cfg, now=NOW) is None
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 寿司が好き\n"
    assert not (tmp_path / STATE_FILENAME).exists()


def test_sweep_extract_disabled_by_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KUMA_MEMORY_EXTRACT", "0")
    _write_conversation(tmp_path, [("user", "猫を飼い始めたんだ")])
    model = _ExtractingModel()
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, model)
    assert check_and_post(cfg, now=NOW,
                          session_provider=lambda: session) is None
    # 抽出の呼び出しは無い（手順書の自動学習は別スイッチで、
    # ここでは対象にしない）
    assert _extract_call_count(model) == 0
    assert not (tmp_path / "MEMORY.md").exists()
    assert not (tmp_path / STATE_FILENAME).exists()


# ---------------------------------------------------------------- CLI

def _setup_cli_env(monkeypatch, tmp_path: Path, stub) -> None:
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KUMA_TENANT", raising=False)
    monkeypatch.delenv("KUMA_USER", raising=False)
    monkeypatch.setenv("META_MODEL_API_KEY", "dummy-key")
    monkeypatch.setattr(cli, "create_client",
                        lambda provider, api_key, model=None: stub)


def test_cli_memory_extract_dry_run(monkeypatch, tmp_path: Path, capsys):
    (tmp_path / "MEMORY.md").write_text("- 寿司が好き\n", encoding="utf-8")
    _write_conversation(tmp_path, [("user", "猫を飼い始めたんだ")])
    _setup_cli_env(monkeypatch, tmp_path, _ExtractingModel())

    assert cli.main(["memory-extract", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "- 猫を飼っている" in out
    assert "変更していません" in out
    # 本体も状態も変わらない
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 寿司が好き\n"
    assert not (tmp_path / STATE_FILENAME).exists()
    assert list(tmp_path.glob("MEMORY.md.bak-*")) == []


def test_cli_memory_extract_applies(monkeypatch, tmp_path: Path, capsys):
    (tmp_path / "MEMORY.md").write_text("- 寿司が好き\n", encoding="utf-8")
    _write_conversation(tmp_path, [("user", "猫を飼い始めたんだ")])
    _setup_cli_env(monkeypatch, tmp_path, _ExtractingModel())

    assert cli.main(["memory-extract"]) == 0
    out = capsys.readouterr().out
    assert "2 件の項目を長期記憶に足しました" in out
    assert "バックアップ" in out
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == (
        "- 寿司が好き\n- 猫を飼っている\n- 仕事はエンジニア\n")
    assert len(list(tmp_path.glob("MEMORY.md.bak-*"))) == 1
    assert _state(tmp_path)["positions"] == {"main": 1}


def test_cli_memory_extract_no_new_conversation(
        monkeypatch, tmp_path: Path, capsys):
    _setup_cli_env(monkeypatch, tmp_path, _ExtractingModel())
    assert cli.main(["memory-extract"]) == 0
    assert "新しい会話がありませんでした" in capsys.readouterr().out


def test_cli_memory_extract_requires_api_key(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    # API キーが無いコマンドとしては配置されていない（キー必須）
    assert cli.main(["memory-extract", "--dry-run"]) == 2
