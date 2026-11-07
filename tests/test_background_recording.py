"""巡回の裏側モデル呼び出しの記録（歯止めの漏れ点検 L3 の直し）。

巡回（proactive の sweep）での自動抽出・自動学習の実行が、活動の
記録（activity の background_model）と使用量（usage）に残ること、
CLI の明示実行（extract_memory / learn_skills の直接呼び出し）では
残らないこと、記録処理の失敗で巡回（自動実行）が落ちないことを
確認する。モデルはスタブで差し替え、ネットワークや実モデルには
出ない。
"""
import json
from datetime import datetime
from pathlib import Path

import pytest

from kuma_agent import memory_extract, skill_learn
from kuma_agent.activity import (
    KIND_BACKGROUND_MODEL,
    load_activity_records,
)
from kuma_agent.config import Config
from kuma_agent.llm import Message, Usage
from kuma_agent.memory import Memory
from kuma_agent.memory_extract import auto_extract_memory, extract_memory
from kuma_agent.proactive import check_and_post
from kuma_agent.session import Session
from kuma_agent.skill_learn import auto_learn_skills, learn_skills
from kuma_agent.usage import load_usage_records

NOW = datetime(2026, 11, 7, 12, 0, 0)

BULLETS = "- 猫を飼っている\n- 仕事はエンジニア"

GOOD_JSON = json.dumps({"skills": [{
    "name": "報告書の作り方",
    "description": "週次報告書を作る手順",
    "body": "# 報告書の作り方\n\n1. 数値を集める\n2. 型に流す",
}]}, ensure_ascii=False)

USAGE = Usage(input_tokens=100, output_tokens=20, total_tokens=120)


class _BackgroundModel:
    """決まった応答と使用量を返すスタブ頭脳。"""

    model = "stub-model-1"

    def __init__(self, reply: str = BULLETS,
                 exc: Exception | None = None) -> None:
        self.reply = reply
        self.exc = exc
        self.calls: list = []

    def chat(self, system_prompt, messages, tools):
        self.calls.append((system_prompt, messages, tools))
        if self.exc is not None:
            raise self.exc
        return Message("assistant", self.reply, usage=USAGE)


def _seed_conversation(user_dir: Path,
                       turns: list[tuple[str, str]]) -> None:
    conv = user_dir / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    with (conv / "main.jsonl").open("a", encoding="utf-8") as f:
        for role, content in turns:
            f.write(json.dumps({"role": role, "content": content},
                               ensure_ascii=False) + "\n")


def _seed_default(user_dir: Path) -> None:
    _seed_conversation(user_dir, [
        ("user", "猫を飼い始めたんだ。報告書は数値を集めてから型に流してね"),
        ("assistant", "分かりました"),
    ])


def _background_records(user_dir: Path) -> list[dict]:
    return [r for r in load_activity_records(user_dir)
            if r.get("kind") == KIND_BACKGROUND_MODEL]


# ------------------------------------------------------------ 自動抽出

def test_auto_extract_records_activity_and_usage(tmp_path: Path):
    _seed_default(tmp_path)
    model = _BackgroundModel()
    result = auto_extract_memory(tmp_path, lambda: model, now=NOW,
                                 provider_of=lambda: "claude")
    assert result is not None and result.applied

    records = _background_records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["tool"] == "memory_extract"
    assert record["ok"] is True
    assert record["summary"] == "追加 2 件"
    assert record["result"] == "applied"
    # プロンプトや会話の全文は記録に残らない
    assert "猫を飼い始めたんだ" not in json.dumps(record, ensure_ascii=False)

    usage = load_usage_records(tmp_path)
    assert len(usage) == 1
    assert usage[0]["provider"] == "claude"
    assert usage[0]["model"] == "stub-model-1"
    assert usage[0]["input_tokens"] == 100
    assert usage[0]["output_tokens"] == 20
    assert usage[0]["total_tokens"] == 120
    assert usage[0]["session_id"] == "background:memory_extract"


def test_auto_extract_model_error_records_activity_only(tmp_path: Path):
    _seed_default(tmp_path)
    model = _BackgroundModel(exc=RuntimeError("boom"))
    result = auto_extract_memory(tmp_path, lambda: model, now=NOW,
                                 provider_of=lambda: "claude")
    assert result is not None and result.reason == "model_failed"

    records = _background_records(tmp_path)
    assert len(records) == 1
    assert records[0]["tool"] == "memory_extract"
    assert records[0]["ok"] is False
    assert records[0]["error_kind"] == "error"
    assert records[0]["result"] == "model_failed"
    # 応答が返らなかった呼び出しは使用量に残さない
    assert load_usage_records(tmp_path) == []


def test_auto_extract_unparseable_reply_records_usage(tmp_path: Path):
    _seed_default(tmp_path)
    model = _BackgroundModel(reply="箇条書きではない応答")
    result = auto_extract_memory(tmp_path, lambda: model, now=NOW,
                                 provider_of=lambda: "claude")
    assert result is not None and result.reason == "model_failed"

    records = _background_records(tmp_path)
    assert len(records) == 1 and records[0]["ok"] is False
    # 応答は返っているので、使用量は読めた分だけ残る
    usage = load_usage_records(tmp_path)
    assert len(usage) == 1 and usage[0]["total_tokens"] == 120


def test_auto_extract_without_model_records_nothing(tmp_path: Path):
    _seed_default(tmp_path)
    assert auto_extract_memory(tmp_path, lambda: None, now=NOW) is None
    assert _background_records(tmp_path) == []
    assert load_usage_records(tmp_path) == []


# ------------------------------------------------------------ 自動学習

def test_auto_learn_records_activity_and_usage(tmp_path: Path):
    _seed_default(tmp_path)
    model = _BackgroundModel(reply=GOOD_JSON)
    result = auto_learn_skills(tmp_path, tmp_path, lambda: model, now=NOW,
                               provider_of=lambda: "gemini")
    assert result is not None and result.learned == ["報告書の作り方"]

    records = _background_records(tmp_path)
    assert len(records) == 1
    record = records[0]
    assert record["tool"] == "skill_learn"
    assert record["ok"] is True
    assert "保存 1 件" in record["summary"]
    assert "報告書の作り方" in record["summary"]
    assert record["result"] == "learned"

    usage = load_usage_records(tmp_path)
    assert len(usage) == 1
    assert usage[0]["provider"] == "gemini"
    assert usage[0]["model"] == "stub-model-1"
    assert usage[0]["total_tokens"] == 120
    assert usage[0]["session_id"] == "background:skill_learn"


def test_auto_learn_model_error_records_activity_only(tmp_path: Path):
    _seed_default(tmp_path)
    model = _BackgroundModel(exc=RuntimeError("boom"))
    result = auto_learn_skills(tmp_path, tmp_path, lambda: model, now=NOW,
                               provider_of=lambda: "gemini")
    assert result is not None and result.reason == "model_failed"

    records = _background_records(tmp_path)
    assert len(records) == 1
    assert records[0]["tool"] == "skill_learn"
    assert records[0]["ok"] is False
    assert load_usage_records(tmp_path) == []


# ------------------------------------------------------------ CLI 明示実行

def test_direct_calls_record_nothing(tmp_path: Path):
    """CLI が使う本体関数の直接呼び出しでは、記録は残らない。"""
    _seed_default(tmp_path)
    extracted = extract_memory(Memory(tmp_path), _BackgroundModel())
    assert extracted.applied
    learned = learn_skills(tmp_path, tmp_path,
                           _BackgroundModel(reply=GOOD_JSON))
    assert learned.learned == ["報告書の作り方"]

    assert not (tmp_path / "activity.jsonl").exists()
    assert not (tmp_path / "usage.jsonl").exists()


# ------------------------------------------------------------ 記録の失敗

def test_recording_failure_does_not_break_auto_extract(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_default(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("record failed")

    monkeypatch.setattr(memory_extract, "record_background_model", boom)
    monkeypatch.setattr(memory_extract, "record_usage", boom)
    result = auto_extract_memory(tmp_path, lambda: _BackgroundModel(),
                                 now=NOW, provider_of=lambda: "claude")
    assert result is not None and result.applied
    assert "- 猫を飼っている" in \
        (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


def test_recording_failure_does_not_break_auto_learn(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _seed_default(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("record failed")

    monkeypatch.setattr(skill_learn, "record_background_model", boom)
    monkeypatch.setattr(skill_learn, "record_usage", boom)
    result = auto_learn_skills(tmp_path, tmp_path,
                               lambda: _BackgroundModel(reply=GOOD_JSON),
                               now=NOW, provider_of=lambda: "gemini")
    assert result is not None and result.learned == ["報告書の作り方"]


def test_provider_failure_does_not_break_auto_extract(tmp_path: Path):
    """頭脳名を引く関数が失敗しても、実行と活動の記録は残る。"""
    _seed_default(tmp_path)

    def bad_provider() -> str:
        raise RuntimeError("no session")

    result = auto_extract_memory(tmp_path, lambda: _BackgroundModel(),
                                 now=NOW, provider_of=bad_provider)
    assert result is not None and result.applied
    assert len(_background_records(tmp_path)) == 1
    assert load_usage_records(tmp_path) == []


# ------------------------------------------------------------ 巡回の配線

class _SweepModel:
    """巡回の中で呼ばれる抽出・学習に、それぞれの応答を返す。"""

    model = "stub-model-1"

    def chat(self, system_prompt, messages, tools):
        if "手順書（スキル）の学習者" in system_prompt:
            return Message("assistant", '{"skills": []}', usage=USAGE)
        if "抽出者" in system_prompt:
            return Message("assistant", BULLETS, usage=USAGE)
        return Message("assistant", "返答")


def test_sweep_records_both_background_runs(tmp_path: Path):
    _seed_default(tmp_path)
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _SweepModel())

    assert check_and_post(cfg, now=NOW,
                          session_provider=lambda: session) is None

    records = {(r["tool"], r["ok"]) for r in _background_records(tmp_path)}
    assert records == {("memory_extract", True), ("skill_learn", True)}

    usage = load_usage_records(tmp_path)
    by_session = {u["session_id"]: u for u in usage}
    assert set(by_session) == {"background:memory_extract",
                               "background:skill_learn"}
    # 頭脳名はセッションの実効の頭脳（cfg.provider）から引かれる
    assert by_session["background:memory_extract"]["provider"] == \
        cfg.provider
    assert by_session["background:memory_extract"]["model"] == \
        "stub-model-1"
