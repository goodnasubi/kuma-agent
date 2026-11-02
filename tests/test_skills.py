"""手順書（スキル）のテスト。

置き場（データ置き場ルートの skills/）の走査（フォルダ形式・
単一ファイル・ヘッダー無し・壊れたファイル）、名前はファイル/
フォルダ名とヘッダーから、説明はヘッダーか本文から取ること、
システムプロンプトには目次（名前＋説明）だけが入り本文は入ら
ないこと、read_skill / save_skill 道具の往復と名前バリデーション、
手順書ゼロでは目次は出ない（道具は出す。最初の手順書を会話から
覚えさせるため）ことを確認する。
"""
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.skills import (
    is_valid_skill_name,
    load_skill,
    load_skills,
    save_skill,
)
from kuma_agent.tools import activity_label


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


FOLDER_SKILL = """\
---
name: release-check
description: リリース前の確認手順
---
# 手順
1. テストを全部回す
2. 版番号を確認する
"""

SINGLE_SKILL = """\
---
description: メールの下書きの作法
---
丁寧な文面で書くこと。SECRET_BODY_MARKER
"""


# ---------------------------------------------------------------- 走査

def test_scan_folder_and_single_file(tmp_path: Path):
    _write(tmp_path / "skills" / "release-check" / "SKILL.md", FOLDER_SKILL)
    _write(tmp_path / "skills" / "mail.md", SINGLE_SKILL)
    skills = {s.name: s for s in load_skills(tmp_path)}
    assert set(skills) == {"release-check", "mail"}
    folder = skills["release-check"]
    assert folder.description == "リリース前の確認手順"
    assert "テストを全部回す" in folder.body
    assert "name:" not in folder.body  # ヘッダーは本文に含めない
    single = skills["mail"]
    # 単一ファイルはヘッダーに name が無ければファイル名が名前になる
    assert single.description == "メールの下書きの作法"
    assert "SECRET_BODY_MARKER" in single.body


def test_scan_without_header_uses_heading_then_first_line(tmp_path: Path):
    _write(tmp_path / "skills" / "trip" / "SKILL.md",
           "# 旅行の計画\n\nまず予算を決める\n")
    _write(tmp_path / "skills" / "memo.md",
           "一行目が説明になる\n二行目以降は本文\n")
    skills = {s.name: s for s in load_skills(tmp_path)}
    assert skills["trip"].description == "旅行の計画"
    assert skills["memo"].description == "一行目が説明になる"
    # ヘッダー無しでは本文はファイル全体
    assert "二行目以降は本文" in skills["memo"].body


def test_scan_broken_and_irrelevant_files_are_skipped(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    # 壊れた（UTF-8 でない）ファイルは読み飛ばす
    (root / "broken.md").write_bytes(b"\xff\xfe\x00bad")
    # .md でないファイルと、SKILL.md の無いフォルダは対象外
    (root / "note.txt").write_text("手順ではない", encoding="utf-8")
    (root / "empty-dir").mkdir()
    _write(root / "ok.md", "大丈夫な手順\n")
    assert [s.name for s in load_skills(tmp_path)] == ["ok"]


def test_scan_missing_dir_and_duplicate_names(tmp_path: Path):
    assert load_skills(tmp_path) == []
    # 同名（フォルダ形式と単一ファイル）が重なったら先勝ち
    _write(tmp_path / "skills" / "dup" / "SKILL.md", "フォルダ側の本文\n")
    _write(tmp_path / "skills" / "dup.md", "ファイル側の本文\n")
    skills = load_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].body == "フォルダ側の本文"


def test_load_skill_by_name(tmp_path: Path):
    _write(tmp_path / "skills" / "a.md", "手順A\n")
    assert load_skill(tmp_path, "a") is not None
    assert load_skill(tmp_path, "ない名前") is None


# ---------------------------------------------------------------- 保存

def test_save_skill_round_trip(tmp_path: Path):
    path = save_skill(tmp_path, "買い物メモ", "買い物の手順",
                      "# 手順\n- 牛乳を買う\n")
    assert path == tmp_path / "skills" / "買い物メモ" / "SKILL.md"
    skill = load_skill(tmp_path, "買い物メモ")
    assert skill is not None
    assert skill.description == "買い物の手順"
    assert skill.body == "# 手順\n- 牛乳を買う"
    # 上書きできる
    save_skill(tmp_path, "買い物メモ", "買い物の手順（改訂）", "新しい本文")
    skill = load_skill(tmp_path, "買い物メモ")
    assert skill is not None
    assert skill.description == "買い物の手順（改訂）"
    assert skill.body == "新しい本文"


def test_skill_name_validation():
    assert is_valid_skill_name("release-check_v2")
    assert is_valid_skill_name("日本語の名前")
    for bad in ("", " ", "..", ".", "a/b", "a\\b", "a b", "a.b", "../x"):
        assert not is_valid_skill_name(bad), bad


# ---------------------------------------------------------------- 道具とプロンプト

class _StubModel:
    """道具を呼ばずに最終応答だけを返すスタブ。"""

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "はい")


def _make_session(tmp_path: Path, monkeypatch, **cfg_kwargs) -> Session:
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path, **cfg_kwargs)
    return Session(cfg, _StubModel())


def test_prompt_has_catalog_without_body(tmp_path: Path, monkeypatch):
    _write(tmp_path / "skills" / "release-check" / "SKILL.md", FOLDER_SKILL)
    session = _make_session(tmp_path, monkeypatch)
    assert "# 使える手順書" in session.system_prompt
    assert "release-check" in session.system_prompt
    assert "リリース前の確認手順" in session.system_prompt
    assert "read_skill" in session.system_prompt
    # 本文そのものは目次に載せない
    assert "テストを全部回す" not in session.system_prompt


def test_no_skills_no_catalog_but_tools_available(tmp_path: Path,
                                                  monkeypatch):
    session = _make_session(tmp_path, monkeypatch)
    # 手順書が 0 件なら目次は出ない
    assert "使える手順書" not in session.system_prompt
    # 道具自体は出す（save_skill で最初の手順書を覚えさせられるため）
    names = [t["name"] for t in session.tools.schemas()]
    assert "read_skill" in names
    assert "save_skill" in names
    assert session.tools.call("read_skill", {"name": "x"}) \
        .startswith("error:")


def test_skill_tools_not_offered_in_wall_mode(tmp_path: Path, monkeypatch):
    _write(tmp_path / "skills" / "release-check" / "SKILL.md", FOLDER_SKILL)
    session = _make_session(tmp_path, monkeypatch, mode="wall")
    names = [t["name"] for t in session.tools.schemas()]
    assert "read_skill" not in names
    assert "save_skill" not in names
    assert "使える手順書" not in session.system_prompt


def test_read_skill_tool(tmp_path: Path, monkeypatch):
    _write(tmp_path / "skills" / "mail.md", SINGLE_SKILL)
    session = _make_session(tmp_path, monkeypatch)
    body = session.tools.call("read_skill", {"name": "mail"})
    assert "SECRET_BODY_MARKER" in body
    missing = session.tools.call("read_skill", {"name": "ありません"})
    assert missing.startswith("error:")


def test_save_skill_tool_round_trip(tmp_path: Path, monkeypatch):
    session = _make_session(tmp_path, monkeypatch)
    result = session.tools.call("save_skill", {
        "name": "掃除の手順", "description": "掃除の進め方",
        "body": "まず窓を開ける"})
    assert result.startswith("保存しました")
    assert (tmp_path / "skills" / "掃除の手順" / "SKILL.md").is_file()
    body = session.tools.call("read_skill", {"name": "掃除の手順"})
    assert body == "まず窓を開ける"
    # 不正な名前は error で返し、ファイルは作らない
    for bad in ("../escape", "a/b", "", "a b"):
        result = session.tools.call("save_skill", {
            "name": bad, "description": "x", "body": "y"})
        assert result.startswith("error:"), bad
    assert not (tmp_path / "skills" / "escape").exists()


def test_activity_labels_for_skill_tools():
    assert activity_label("read_skill") == "手順書を読んでいます"
    assert activity_label("save_skill") == "手順書を書き留め中"
