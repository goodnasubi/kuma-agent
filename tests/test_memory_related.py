"""AM-1（関連する記憶の差し込み、0.70.0）のテスト。

長期記憶が全文注入の上限を超えたときだけ、上限で外れた古い項目の
中から今の発話に関連するものを拾い直し、プロンプトへ「関連する
記憶（前に覚えたこと）」の節として足すことを確認する。上限以下
では従来どおり（差し込みなし）、関連が無ければ節自体が付かない
ことも押さえる。選び方はキーワードの点数付け（search_memory_text）
の再利用で、追加のモデル呼び出しは無い。
"""
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import related_memory_text
from kuma_agent.session import Session


def _filler(i: int) -> str:
    # 既存テストと同じ形: 「- 項目N：」+ あ 20 文字 = 26 文字
    return f"- 項目{i}：" + "あ" * 20


def _big_memory_text() -> str:
    """先頭に図書館の項目、後ろに filler 5 件の記憶。

    上限 60 字では末尾の filler 2 件だけが一括載せに残り、
    図書館の項目は外れる。
    """
    lines = ["- 図書館の本の返却期限は金曜日まで"]
    lines += [_filler(i) for i in range(2, 7)]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 部品

def test_related_picks_dropped_item_matching_query():
    text = _big_memory_text()
    picked = related_memory_text(text, "図書館の本、返却期限はいつ", 60)
    assert picked == "- 図書館の本の返却期限は金曜日まで"


def test_related_returns_empty_below_limit():
    text = _big_memory_text()
    # 上限以下で全文が載る間は、差し込む余地が無いので働かない
    assert related_memory_text(text, "図書館の本", 6000) == ""


def test_related_returns_empty_without_match():
    text = _big_memory_text()
    assert related_memory_text(text, "今日の天気はどう", 60) == ""


def test_related_does_not_repeat_injected_items():
    lines = ["- 寿司は好物だと言っていた"]
    lines += [_filler(i) for i in range(2, 6)]
    lines.append("- 寿司の店を予約した")
    text = "\n".join(lines) + "\n"
    # 上限 60 では末尾の「寿司の店」と filler 1 件が一括載せに残る。
    # 発話に両方関係しても、載っている方は拾い直しの対象にしない
    picked = related_memory_text(text, "寿司", 60)
    assert picked == "- 寿司は好物だと言っていた"


def test_related_caps_item_count():
    lines = [f"- 寿司メモ{i}" for i in range(1, 6)]
    lines += [_filler(i) for i in range(6, 9)]
    text = "\n".join(lines) + "\n"
    picked = related_memory_text(text, "寿司", 60)
    # 関連する古い項目が 5 件あっても最大 3 件まで
    assert len(picked.splitlines()) == 3
    assert len(picked) <= 800


def test_related_skips_oversize_item_and_respects_total_chars():
    big = "- 寿司" + "あ" * 900
    lines = [big] + [_filler(i) for i in range(2, 5)]
    text = "\n".join(lines) + "\n"
    # 800 字を超える項目は、関連しても載せない
    assert related_memory_text(text, "寿司", 60) == ""

    first = "- 寿司" + "あ" * 496   # 500 字
    second = "- 寿司" + "い" * 496  # 500 字
    lines = [first, second] + [_filler(i) for i in range(3, 6)]
    text = "\n".join(lines) + "\n"
    picked = related_memory_text(text, "寿司", 60)
    # 合計 800 字に収まる分だけ（先の 1 件のみ）
    assert picked == first


# ---------------------------------------------------------------- 会話

class _CaptureModel:
    """受け取ったプロンプトを覚え、道具なしで即答するスタブ。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def chat(self, system_prompt, messages, tools):
        self.prompts.append(system_prompt)
        return Message("assistant", "分かりました")


def _make_session(tmp_path: Path, monkeypatch, memory_text: str,
                  **cfg_kwargs) -> tuple[Session, _CaptureModel]:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "MEMORY.md").write_text(memory_text, encoding="utf-8")
    model = _CaptureModel()
    cfg = Config(data_dir=tmp_path, **cfg_kwargs)
    return Session(cfg, model), model


def test_session_injects_related_section_above_threshold(
        tmp_path: Path, monkeypatch):
    session, model = _make_session(
        tmp_path, monkeypatch, _big_memory_text(),
        memory_full_inject_chars=60)
    # 一括載せ（システムプロンプト）には図書館の項目は載っていない
    assert "図書館" not in session.system_prompt
    assert session.say("図書館の本、返却期限はいつだっけ") == "分かりました"
    prompt = model.prompts[0]
    assert "# 関連する記憶（前に覚えたこと）" in prompt
    assert "- 図書館の本の返却期限は金曜日まで" in prompt


def test_session_no_related_section_below_threshold(
        tmp_path: Path, monkeypatch):
    session, model = _make_session(
        tmp_path, monkeypatch,
        "- 好きな食べ物は寿司\n- 好きな飲み物は緑茶\n")
    assert "好きな食べ物は寿司" in session.system_prompt
    assert session.say("寿司の話をしよう") == "分かりました"
    # 全文が載っている間は差し込み節は付かない（従来どおり）
    assert "関連する記憶" not in model.prompts[0]


def test_session_no_related_section_without_match(
        tmp_path: Path, monkeypatch):
    session, model = _make_session(
        tmp_path, monkeypatch, _big_memory_text(),
        memory_full_inject_chars=60)
    assert session.say("今日の天気はどうかな") == "分かりました"
    assert "関連する記憶" not in model.prompts[0]


def test_session_related_section_also_in_wall_mode(
        tmp_path: Path, monkeypatch):
    session, model = _make_session(
        tmp_path, monkeypatch, _big_memory_text(),
        mode="wall", memory_full_inject_chars=60)
    assert "図書館" not in session.system_prompt
    assert session.say("図書館の本、返却期限はいつだっけ") == "分かりました"
    assert "# 関連する記憶（前に覚えたこと）" in model.prompts[0]
    assert "- 図書館の本の返却期限は金曜日まで" in model.prompts[0]
