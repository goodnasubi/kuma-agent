"""手順書の自動学習（会話 → skills/）のテスト。

応答の解析（JSON の形の検証）、候補の絞り込み（既存との
重なり・不正な名前・上限）、保存とフィードへの記録、処理済み
位置の進め方（成功で進む・失敗で進まない）、自動実行の条件
（1 日 1 回・無効化・モデルなし）、巡回への組み込み、CLI の
dry-run を確認する。既存の手順書が上書きされないことも確かめる。
"""
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.feed import load_feed_entries
from kuma_agent.llm import Message
from kuma_agent.proactive import check_and_post
from kuma_agent.session import Session
from kuma_agent.skill_learn import (
    _parse_candidates,
    auto_learn_skills,
    learn_skills,
)
from kuma_agent.skills import load_skill, load_skills, save_skill

NOW = datetime(2026, 11, 7, 12, 0, 0)

GOOD_JSON = json.dumps({"skills": [{
    "name": "報告書の作り方",
    "description": "週次報告書を作る手順",
    "body": "# 報告書の作り方\n\n1. 数値を集める\n2. 型に流す",
}]}, ensure_ascii=False)


class _LearnModel:
    """学習用の応答を固定で返すスタブ。受けた指示も覚える。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.systems: list[str] = []

    def chat(self, system_prompt, messages, tools):
        self.systems.append(system_prompt)
        return Message("assistant", self.reply)


def _seed_conversation(user_dir: Path, turns: list[tuple[str, str]]):
    conv = user_dir / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    with (conv / "main.jsonl").open("a", encoding="utf-8") as f:
        for role, content in turns:
            f.write(json.dumps({"role": role, "content": content},
                               ensure_ascii=False) + "\n")


def _seed_default(tmp_path: Path) -> None:
    _seed_conversation(tmp_path, [
        ("user", "報告書は、まず数値を集めて、それから型に流してね"),
        ("assistant", "分かりました。その順番で進めます"),
    ])


# ------------------------------------------------------------ 応答の解析

def test_parse_candidates():
    parsed = _parse_candidates(GOOD_JSON)
    assert parsed is not None and parsed[0]["name"] == "報告書の作り方"
    fenced = _parse_candidates(f"```json\n{GOOD_JSON}\n```")
    assert fenced is not None and len(fenced) == 1
    assert _parse_candidates('{"skills": []}') == []
    assert _parse_candidates("JSON ではありません") is None
    assert _parse_candidates('{"skills": "x"}') is None
    assert _parse_candidates('{"skills": [{"name": 1}]}') is None
    assert _parse_candidates("") is None


# ------------------------------------------------------------ 学習の実行

def test_learn_saves_new_skill_and_feeds(tmp_path: Path):
    _seed_default(tmp_path)
    result = learn_skills(tmp_path, tmp_path, _LearnModel(GOOD_JSON),
                          now=NOW)
    assert result.learned == ["報告書の作り方"]
    skill = load_skill(tmp_path, "報告書の作り方")
    assert skill is not None
    assert skill.description == "週次報告書を作る手順"
    assert "数値を集める" in skill.body
    # フィードに学習の記録が残る
    entries = load_feed_entries(tmp_path)
    assert any("手順書" in e.body and "報告書の作り方" in e.body
               for e in entries)
    # 位置が進んだので、同じ材料では二度目は材料なし
    again = learn_skills(tmp_path, tmp_path, _LearnModel(GOOD_JSON),
                         now=NOW)
    assert again.reason == "no_new_turns"


def test_learn_never_overwrites_existing(tmp_path: Path):
    save_skill(tmp_path, "報告書の作り方", "元からある説明", "元の本文")
    _seed_default(tmp_path)
    result = learn_skills(tmp_path, tmp_path, _LearnModel(GOOD_JSON),
                          now=NOW)
    assert result.learned == [] and result.reason == "nothing"
    skill = load_skill(tmp_path, "報告書の作り方")
    assert skill is not None and skill.body == "元の本文"


def test_learn_filters_candidates(tmp_path: Path):
    reply = json.dumps({"skills": [
        {"name": "悪い 名前", "description": "空白入り", "body": "本文"},
        {"name": "ok1", "description": "説明", "body": "本文1"},
        {"name": "ok1", "description": "重複", "body": "本文"},
        {"name": "ok2", "description": "説明", "body": "あ" * 5000},
        {"name": "ok3", "description": "説明", "body": "本文3"},
        {"name": "ok4", "description": "説明", "body": "本文4"},
        {"name": "ok5", "description": "説明", "body": "本文5"},
    ]}, ensure_ascii=False)
    _seed_default(tmp_path)
    result = learn_skills(tmp_path, tmp_path, _LearnModel(reply), now=NOW)
    # 不正な名前・同名の重複・長すぎる本文は落ち、上限 3 件まで
    assert result.learned == ["ok1", "ok3", "ok4"]
    assert [s.name for s in load_skills(tmp_path)] == ["ok1", "ok3", "ok4"]


def test_learn_model_failure_keeps_position(tmp_path: Path):
    _seed_default(tmp_path)
    result = learn_skills(tmp_path, tmp_path,
                          _LearnModel("箇条書きで返してしまった"), now=NOW)
    assert result.reason == "model_failed"
    state = json.loads(
        (tmp_path / "skill_learn_state.json").read_text(encoding="utf-8")) \
        if (tmp_path / "skill_learn_state.json").exists() else {}
    assert not state.get("positions")  # 位置は進んでいない
    # 翌日なら同じ材料から再挑戦でき、今度は成功する
    result2 = learn_skills(tmp_path, tmp_path, _LearnModel(GOOD_JSON),
                           now=NOW)
    assert result2.learned == ["報告書の作り方"]


def test_learn_dry_run_changes_nothing(tmp_path: Path):
    _seed_default(tmp_path)
    result = learn_skills(tmp_path, tmp_path, _LearnModel(GOOD_JSON),
                          now=NOW, dry_run=True)
    assert [c["name"] for c in result.candidates] == ["報告書の作り方"]
    assert load_skills(tmp_path) == []
    assert not (tmp_path / "skill_learn_state.json").exists()


def test_learn_no_material(tmp_path: Path):
    result = learn_skills(tmp_path, tmp_path, _LearnModel(GOOD_JSON),
                          now=NOW)
    assert result.reason == "no_new_turns"


# ------------------------------------------------------------ 自動実行の条件

def test_auto_learn_once_a_day(tmp_path: Path):
    _seed_default(tmp_path)
    model = _LearnModel(GOOD_JSON)
    result = auto_learn_skills(tmp_path, tmp_path, lambda: model, now=NOW)
    assert result is not None and result.learned == ["報告書の作り方"]
    # 同じ日は二度目を実行しない（材料が増えても）
    _seed_conversation(tmp_path, [("user", "追加の会話")])
    assert auto_learn_skills(tmp_path, tmp_path, lambda: model,
                             now=NOW) is None


def test_auto_learn_disabled_by_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KUMA_SKILL_LEARN", "0")
    _seed_default(tmp_path)
    assert auto_learn_skills(tmp_path, tmp_path,
                             lambda: _LearnModel(GOOD_JSON),
                             now=NOW) is None
    assert load_skills(tmp_path) == []


def test_auto_learn_without_model_records_nothing(tmp_path: Path):
    _seed_default(tmp_path)
    assert auto_learn_skills(tmp_path, tmp_path, lambda: None,
                             now=NOW) is None
    # 試行の記録が残っていないので、モデルが引ければそのまま実行できる
    result = auto_learn_skills(tmp_path, tmp_path,
                               lambda: _LearnModel(GOOD_JSON), now=NOW)
    assert result is not None and result.learned == ["報告書の作り方"]


# ------------------------------------------------------------ 巡回への組み込み

class _SweepModel:
    """巡回の中で呼ばれる各モデル処理に、それぞれの応答を返す。"""

    def chat(self, system_prompt, messages, tools):
        if "手順書（スキル）の学習者" in system_prompt:
            return Message("assistant", GOOD_JSON)
        if "抽出者" in system_prompt:
            return Message("assistant", "なし")
        return Message("assistant", "返答")


def test_check_and_post_learns_skills(tmp_path: Path):
    _seed_default(tmp_path)
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _SweepModel())
    check_and_post(cfg, now=NOW, session_provider=lambda: session)
    assert load_skill(tmp_path, "報告書の作り方") is not None


# ------------------------------------------------------------ CLI

def test_cli_skill_learn_dry_run(tmp_path: Path, monkeypatch, capsys):
    _seed_default(tmp_path)
    from kuma_agent.cli import _run_skill_learn
    cfg = Config(data_dir=tmp_path)
    code = _run_skill_learn(cfg, _LearnModel(GOOD_JSON), dry_run=True)
    assert code == 0
    out = capsys.readouterr().out
    assert "報告書の作り方" in out
    assert load_skills(tmp_path) == []  # dry-run では保存しない
