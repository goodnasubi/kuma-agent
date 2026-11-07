"""長期記憶の属性（AM-2）と棚卸し（AM-3）のテスト。

属性が memory_meta.json に付き MEMORY.md の形式が変わらない
こと、自動抽出の期限の印が本文に残らず属性へ回ること、期限を
過ぎた一時的な項目が控えつきで外れてフィードに残ること、
確認候補が朝のまとめに 1 日 1 件までしか混ざらないこと、
属性の無い・壊れた既存データでも壊れないことを確認する。
モデルはスタブで差し替え、ネットワークや実モデルには出ない。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.feed import load_feed_entries
from kuma_agent.llm import Message
from kuma_agent.memory import Memory
from kuma_agent.memory_audit import (
    STATE_FILENAME,
    audit_memory,
    pending_confirm,
)
from kuma_agent.memory_extract import extract_memory
from kuma_agent.memory_meta import (
    META_FILENAME,
    load_memory_meta,
    record_memory_items,
)
from kuma_agent.memory_tidy import tidy_memory_with_model
from kuma_agent.proactive import check_and_post
from kuma_agent.session import Session

NOW = datetime(2026, 11, 7, 9, 0, 0)
TODAY = "2026-11-07"


def _meta_values(user_dir: Path) -> list[dict]:
    return list(load_memory_meta(user_dir).values())


def _write_conversation(user_dir: Path, turns: list[tuple[str, str]]) -> None:
    memory = Memory(user_dir)
    for role, content in turns:
        memory.append_turn(role, content)


class _StubModel:
    """決まった応答を返すスタブ頭脳（抽出・統合用）。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list = []

    def chat(self, system_prompt, messages, tools):
        self.calls.append((system_prompt, messages, tools))
        return Message("assistant", self.reply)


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "応答です")


class _SpyExecutor:
    """朝のまとめの子の差し替え。指示文を記録し、決めた結果を返す。"""

    def __init__(self, result: str = "おはようございます。今日のまとめです。"):
        self.result = result
        self.prompts: list[str] = []

    def __call__(self, parent, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.result


# ---------------------------------------------------------- AM-2: 属性の記録

def test_remember_records_meta_without_changing_format(tmp_path: Path):
    memory = Memory(tmp_path)
    memory.remember("好きな食べ物は寿司", now=NOW)
    # MEMORY.md の形式は従来どおり（属性は本文に混ざらない）
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 好きな食べ物は寿司\n"
    assert _meta_values(tmp_path) == [{"since": TODAY}]

    memory.remember("今週末まで実家に帰省する", until="2026-11-09",
                    now=NOW)
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == (
        "- 好きな食べ物は寿司\n- 今週末まで実家に帰省する\n")
    entries = sorted(_meta_values(tmp_path),
                     key=lambda e: e.get("until", ""))
    assert entries[0] == {"since": TODAY}
    assert entries[1] == {"since": TODAY, "until": "2026-11-09"}


def test_remember_again_refreshes_since_and_keeps_until(tmp_path: Path):
    memory = Memory(tmp_path)
    memory.remember("今週末まで実家に帰省する", until="2026-11-09",
                    now=NOW - timedelta(days=10))
    later = NOW + timedelta(days=3)
    record_memory_items(tmp_path, [("- 今週末まで実家に帰省する", None)],
                        now=later)
    # 改めて触れられたので since は新しい日になり、期限は残る
    assert _meta_values(tmp_path) == [
        {"since": "2026-11-10", "until": "2026-11-09"}]


def test_extract_records_meta_and_strips_until_marker(tmp_path: Path):
    _write_conversation(tmp_path, [
        ("user", "今週末まで実家に帰省することにしたよ"),
        ("assistant", "分かりました"),
    ])
    model = _StubModel(
        "- 今週末まで実家に帰省する（期限: 2026-11-09）\n- 猫を飼っている")
    result = extract_memory(Memory(tmp_path), model, now=NOW)
    assert result.applied is True
    # 本文に期限の印は残らない（MEMORY.md の形式は従来どおり）
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == (
        "- 今週末まで実家に帰省する\n- 猫を飼っている\n")
    entries = _meta_values(tmp_path)
    assert {"since": TODAY, "until": "2026-11-09"} in entries
    assert {"since": TODAY} in entries
    # 期限を計算できるよう、プロンプトに今日の日付が示される
    prompt = model.calls[0][1][0].content
    assert f"# 今日の日付\n{TODAY}" in prompt
    assert "期限: YYYY-MM-DD" in model.calls[0][0]


def test_extract_plain_bullets_record_since_only(tmp_path: Path):
    """従来形式（印なし）の応答でも、属性は覚えた日だけ付く。"""
    _write_conversation(tmp_path, [("user", "猫を飼い始めたんだ")])
    result = extract_memory(Memory(tmp_path),
                            _StubModel("- 猫を飼っている"), now=NOW)
    assert result.applied is True
    assert _meta_values(tmp_path) == [{"since": TODAY}]


# ------------------------------------------------- AM-3: 期限切れの外し込み

def _seed_expiry_fixture(user_dir: Path) -> str:
    """期限切れ・期限内・期限なしの項目を持つ MEMORY.md と属性を用意する。"""
    text = ("- 今週末まで実家に帰省する\n"
            "- 今日まで在宅勤務\n"
            "- 来週まで出張する\n"
            "- 猫を飼っている\n")
    (user_dir / "MEMORY.md").write_text(text, encoding="utf-8")
    past = NOW - timedelta(days=6)
    record_memory_items(user_dir, [
        ("- 今週末まで実家に帰省する", "2026-11-06"),  # 期限切れ
        ("- 今日まで在宅勤務", TODAY),                   # 当日はまだ有効
        ("- 来週まで出張する", "2026-11-14"),            # 期限内
        ("- 猫を飼っている", None),                      # 期限なし
    ], now=past)
    return text


def test_audit_removes_expired_with_backup_and_feed(tmp_path: Path):
    original = _seed_expiry_fixture(tmp_path)

    result = audit_memory(tmp_path, now=NOW)
    assert result is not None
    assert result.removed == ["- 今週末まで実家に帰省する"]
    # 控えに外す前の本文が残る
    assert result.backup_path is not None
    assert result.backup_path.name.startswith("MEMORY.md.bak-")
    assert result.backup_path.read_text(encoding="utf-8") == original
    # 期限切れだけが外れ、当日・期限内・期限なしは残る
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == (
        "- 今日まで在宅勤務\n- 来週まで出張する\n- 猫を飼っている\n")
    # 外した項目の属性も消える
    remaining = load_memory_meta(tmp_path)
    assert len(remaining) == 3
    assert all(e.get("until") != "2026-11-06" for e in remaining.values())
    # フィードに 1 件だけ残る（種別は proactive、会話には投稿しない）
    entries = load_feed_entries(tmp_path)
    assert len(entries) == 1
    assert entries[0].kind == "proactive"
    assert "期限を過ぎた一時的な記憶を 1 件外しました" in entries[0].body
    assert "今週末まで実家に帰省する" in entries[0].body
    assert not (tmp_path / "conversations" / "main.jsonl").exists()

    # 同じ日はもう実施しない（フィードも増えない）
    assert audit_memory(tmp_path, now=NOW) is None
    assert len(load_feed_entries(tmp_path)) == 1


def test_audit_disabled_by_env(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("KUMA_MEMORY_AUDIT", "0")
    original = _seed_expiry_fixture(tmp_path)
    assert audit_memory(tmp_path, now=NOW) is None
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == original
    assert load_feed_entries(tmp_path) == []
    assert not (tmp_path / STATE_FILENAME).exists()


# ------------------------------------------------------- AM-3: 確認の候補

ITEM_A = "- 去年、京都に引っ越した"
ITEM_B = "- 大阪の会社に通っている"
ITEM_C = "- 好きな食べ物は寿司"


def _seed_confirm_fixture(user_dir: Path) -> None:
    """90 日以上動きのない「変わり得る事実」2 件と、対象外 1 件を用意する。"""
    (user_dir / "MEMORY.md").write_text(
        f"{ITEM_A}\n{ITEM_B}\n{ITEM_C}\n", encoding="utf-8")
    record_memory_items(user_dir, [(ITEM_A, None)],
                        now=NOW - timedelta(days=100))
    record_memory_items(user_dir, [(ITEM_B, None)],
                        now=NOW - timedelta(days=95))
    record_memory_items(user_dir, [(ITEM_C, None)],
                        now=NOW - timedelta(days=100))


def test_audit_picks_oldest_changeable_item_as_confirm(tmp_path: Path):
    _seed_confirm_fixture(tmp_path)
    result = audit_memory(tmp_path, now=NOW)
    assert result is not None
    assert result.removed == []
    # since が一番古い A が候補（B は次の機会、C は状態の語が無い）
    assert result.confirm_text == ITEM_A
    assert pending_confirm(tmp_path, now=NOW) is not None
    # 翌日に棚卸しをしていない状態では、古い候補は表に出ない
    assert pending_confirm(tmp_path, now=NOW + timedelta(days=1)) is None


def test_confirm_asked_once_a_day_through_briefing(tmp_path: Path):
    _seed_confirm_fixture(tmp_path)
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    # 1 日目: 候補 A だけが朝のまとめの材料に入る（用事なしでも子が動く）
    text = check_and_post(cfg, now=NOW, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == spy.result
    assert len(spy.prompts) == 1
    assert "確認したい記憶" in spy.prompts[0]
    assert "京都に引っ越した" in spy.prompts[0]
    assert "大阪の会社" not in spy.prompts[0]  # 1 日 1 件まで
    # 投稿されたので「尋ねた」印が付き、候補は当日分としては消える
    assert pending_confirm(tmp_path, now=NOW) is None

    # 2 日目: A は間隔（30 日）の間は選ばれず、候補は B になる
    text = check_and_post(cfg, now=NOW + timedelta(days=1),
                          session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == spy.result
    assert len(spy.prompts) == 2
    assert "大阪の会社に通っている" in spy.prompts[1]
    assert "京都に引っ越した" not in spy.prompts[1]

    # 3 日目: A・B とも尋ねたばかりで、候補は無い（子も動かない）
    assert check_and_post(cfg, now=NOW + timedelta(days=2),
                          session_provider=lambda: session,
                          briefing_executor=spy) is None
    assert len(spy.prompts) == 2


def test_confirm_not_mixed_into_digest_when_briefing_off(
        tmp_path: Path):
    _seed_confirm_fixture(tmp_path)
    (tmp_path / "tasks.json").write_text(json.dumps([{
        "id": "1", "title": "買い物", "status": "open", "memo": "",
        "created_at": NOW.isoformat(timespec="seconds"),
        "updated_at": NOW.isoformat(timespec="seconds"),
    }], ensure_ascii=False), encoding="utf-8")
    cfg = Config(data_dir=tmp_path, briefing_time="off")

    # 朝のまとめが無効な構成では、日報に確認は混ざらない
    text = check_and_post(cfg, now=NOW)
    assert text is not None and "買い物" in text
    assert "引っ越した" not in text
    # 尋ねたことにはならず、候補は残る（翌日以降のまとめで使える）
    assert pending_confirm(tmp_path, now=NOW) is not None


# ------------------------------------------- 既存データでの頑健さ（属性なし等）

def test_audit_without_meta_does_not_break(tmp_path: Path):
    """手書きの MEMORY.md だけで属性ファイルが無い場合。"""
    (tmp_path / "MEMORY.md").write_text("- 猫を飼っている\n",
                                        encoding="utf-8")
    result = audit_memory(tmp_path, now=NOW)
    assert result is not None
    assert result.removed == [] and result.confirm_text is None
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 猫を飼っている\n"
    # 属性ファイルは作られない（読み取りだけ）
    assert not (tmp_path / META_FILENAME).exists()
    assert load_feed_entries(tmp_path) == []


def test_corrupt_meta_does_not_break_audit_or_remember(tmp_path: Path):
    (tmp_path / "MEMORY.md").write_text("- 猫を飼っている\n",
                                        encoding="utf-8")
    (tmp_path / META_FILENAME).write_text("{壊れた内容", encoding="utf-8")
    result = audit_memory(tmp_path, now=NOW)
    assert result is not None and result.removed == []
    # 壊れた属性は「属性なし」として扱われ、記録し直せば直る
    Memory(tmp_path).remember("犬も飼っている", now=NOW)
    assert _meta_values(tmp_path) == [{"since": TODAY}]


def test_audit_prunes_orphans_after_hand_edit(tmp_path: Path):
    """MEMORY.md を手で編集して項目が消えたら、属性も掃除される。"""
    memory = Memory(tmp_path)
    memory.remember("猫を飼っている", now=NOW)
    memory.remember("犬も飼っている", now=NOW)
    assert len(load_memory_meta(tmp_path)) == 2
    # 利用者が 1 行を手で消した
    (tmp_path / "MEMORY.md").write_text("- 猫を飼っている\n",
                                        encoding="utf-8")
    audit_memory(tmp_path, now=NOW)
    remaining = _meta_values(tmp_path)
    assert len(remaining) == 1


def test_tidy_prunes_orphan_meta(tmp_path: Path):
    """整理（モデル統合）で項目が消えたら、その属性も掃除される。"""
    memory = Memory(tmp_path)
    memory.remember("猫を飼っている", now=NOW)
    memory.remember("犬も飼っている", now=NOW)
    result = tidy_memory_with_model(
        memory, _StubModel("- 猫を飼っている"), now=NOW)
    assert result.applied is True
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") \
        == "- 猫を飼っている\n"
    assert len(load_memory_meta(tmp_path)) == 1
