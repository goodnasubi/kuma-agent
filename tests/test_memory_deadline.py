"""AM-4（期限の近い記憶と用事の突き合わせ）のテスト。

属性 until（AM-2）を持ち期限の近い長期記憶の項目が、朝のまとめ
の材料にだけ入り、遠い期限・期限なし・期限切れは入らないこと。
進行中の用事と強く重なる項目は、用事側の知らせに任せて材料
から外れること。日報への倒し込みには混ざらないこと。
モデルはスタブで差し替え、ネットワークや実モデルには出ない。
"""
import json
from datetime import datetime
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import Memory
from kuma_agent.proactive import check_and_post, near_deadline_text
from kuma_agent.session import Session
from kuma_agent.tasks import Task

# 2026-11-07 は土曜日。まとめの既定時刻は 07:30
NOW = datetime(2026, 11, 7, 8, 0, 0)
SUMMARY = "おはようございます。今日のまとめです。"


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "応答です")


class _SpyExecutor:
    """朝のまとめの子の差し替え。指示文を記録し、決めた結果を返す。"""

    def __init__(self, result: str = SUMMARY) -> None:
        self.result = result
        self.prompts: list[str] = []

    def __call__(self, parent, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.result


def _remember(user_dir: Path, fact: str, until: str | None = None) -> None:
    Memory(user_dir).remember(fact, until=until, now=NOW)


def _write_json(user_dir: Path, name: str, data: object) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / name).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _task_dict(task_id: str, title: str) -> dict:
    stamp = NOW.isoformat(timespec="seconds")
    return {"id": task_id, "title": title, "status": "open", "memo": "",
            "created_at": stamp, "updated_at": stamp}


def _records(user_dir: Path) -> list[dict]:
    path = user_dir / "conversations" / "main.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------ 材料の拾い方（単体）

def test_near_deadline_items_are_picked(tmp_path: Path):
    _remember(tmp_path, "図書館の本を返す約束をした", until="2026-11-09")
    _remember(tmp_path, "今日が期限の連絡", until="2026-11-07")
    _remember(tmp_path, "三日後の予定の確認", until="2026-11-10")

    text = near_deadline_text(tmp_path, [], NOW)
    assert text is not None
    lines = text.splitlines()
    assert len(lines) == 3
    # 期限の近い順に並ぶ
    assert "今日が期限の連絡" in lines[0] and "期限は今日まで" in lines[0]
    assert "図書館の本を返す約束をした" in lines[1]
    assert "あと2日" in lines[1]
    assert "三日後の予定の確認" in lines[2] and "あと3日" in lines[2]


def test_far_and_no_deadline_items_are_not_picked(tmp_path: Path):
    _remember(tmp_path, "四日後の期限", until="2026-11-11")
    _remember(tmp_path, "来月の期限", until="2026-12-31")
    _remember(tmp_path, "期限のない近況")
    assert near_deadline_text(tmp_path, [], NOW) is None

    # 近い項目が混ざれば、それだけが材料になる
    _remember(tmp_path, "明日の期限", until="2026-11-08")
    text = near_deadline_text(tmp_path, [], NOW)
    assert text is not None
    assert "明日の期限" in text
    assert "四日後の期限" not in text
    assert "来月の期限" not in text
    assert "期限のない近況" not in text


def test_expired_items_are_not_picked(tmp_path: Path):
    # 期限を過ぎた項目は棚卸し（AM-3）の担当で、こちらには入らない
    _remember(tmp_path, "期限を過ぎた約束", until="2026-11-06")
    assert near_deadline_text(tmp_path, [], NOW) is None


def test_near_deadline_cap(tmp_path: Path):
    _remember(tmp_path, "期限1", until="2026-11-10")
    _remember(tmp_path, "期限2", until="2026-11-08")
    _remember(tmp_path, "期限3", until="2026-11-07")
    _remember(tmp_path, "期限4", until="2026-11-09")
    _remember(tmp_path, "期限5", until="2026-11-08")

    text = near_deadline_text(tmp_path, [], NOW)
    assert text is not None
    lines = text.splitlines()
    assert len(lines) == 3  # 最大 3 件まで
    assert "期限3" in lines[0]  # 一番近いものが先頭


def test_no_memory_or_no_meta_returns_none(tmp_path: Path):
    assert near_deadline_text(tmp_path, [], NOW) is None
    # 属性（memory_meta.json）が無い項目は期限なしとして扱う
    (tmp_path / "MEMORY.md").write_text(
        "- 属性のない項目\n", encoding="utf-8")
    assert near_deadline_text(tmp_path, [], NOW) is None


# ------------------------------------------------ 用事との重なり

def test_overlapping_task_items_are_excluded(tmp_path: Path):
    _remember(tmp_path, "図書館の本を返す約束をした", until="2026-11-09")
    # 用事の題名がそのまま項目に含まれる（語 1 つだが具体的）
    task = Task(id="1", title="図書館の本を返す")
    assert near_deadline_text(tmp_path, [task], NOW) is None


def test_two_term_overlap_is_excluded(tmp_path: Path):
    _remember(tmp_path, "図書館の本を返す約束をした", until="2026-11-09")
    # 語が 2 つ取れて両方とも項目に一致する場合は強い重なり
    task = Task(id="1", title="図書館の本 を返す")
    assert near_deadline_text(tmp_path, [task], NOW) is None


def test_weak_overlap_is_not_excluded(tmp_path: Path):
    _remember(tmp_path, "図書館の本を返す約束をした", until="2026-11-09")
    # 短い語（3 文字）1 つだけの一致は「はっきり重なる」とは
    # みなさない（知らせが消えすぎないよう保守的に倒す）
    task = Task(id="1", title="図書館")
    text = near_deadline_text(tmp_path, [task], NOW)
    assert text is not None and "図書館の本を返す約束をした" in text
    # 無関係な用事でも外れない
    task = Task(id="2", title="買い物")
    text = near_deadline_text(tmp_path, [task], NOW)
    assert text is not None and "図書館の本を返す約束をした" in text


def test_only_overlapping_items_are_excluded(tmp_path: Path):
    _remember(tmp_path, "図書館の本を返す約束をした", until="2026-11-09")
    _remember(tmp_path, "健康診断の予約の期限", until="2026-11-08")
    task = Task(id="1", title="図書館の本を返す")

    text = near_deadline_text(tmp_path, [task], NOW)
    assert text is not None
    assert "図書館の本を返す約束をした" not in text
    assert "健康診断の予約の期限" in text


# ------------------------------------------------ 朝のまとめへの接続

def test_briefing_includes_near_deadline(tmp_path: Path):
    _remember(tmp_path, "図書館の本を返す約束をした", until="2026-11-09")
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    # 用事が無くても、期限の近い記憶自体が子を動かす材料になる
    text = check_and_post(cfg, now=NOW, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == SUMMARY
    assert len(spy.prompts) == 1
    prompt = spy.prompts[0]
    assert "近づいている期限のある記憶" in prompt
    assert "図書館の本を返す約束をした" in prompt
    assert "あと2日" in prompt
    assert len(_records(tmp_path)) == 1


def test_overlapping_task_excluded_from_briefing_prompt(tmp_path: Path):
    _write_json(tmp_path, "tasks.json",
                [_task_dict("1", "図書館の本を返す")])
    _remember(tmp_path, "図書館の本を返す約束をした", until="2026-11-09")
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    text = check_and_post(cfg, now=NOW, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == SUMMARY
    assert len(spy.prompts) == 1
    prompt = spy.prompts[0]
    # 用事としては材料に入るが、記憶の節には二重に出ない
    assert "図書館の本を返す" in prompt
    assert "約束をした" not in prompt
    assert "近づいている期限のある記憶:" not in prompt


def test_deadline_not_in_digest_when_briefing_off(tmp_path: Path):
    _write_json(tmp_path, "tasks.json", [_task_dict("1", "買い物")])
    _remember(tmp_path, "図書館の本を返す約束をした", until="2026-11-09")
    cfg = Config(data_dir=tmp_path, briefing_time="off")
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    # 朝のまとめが無効なら日報だけが出て、期限の近い記憶は混ざらない
    text = check_and_post(cfg, now=NOW, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text is not None and "お知らせです" in text
    assert "買い物" in text
    assert "図書館の本を返す約束をした" not in text
    assert spy.prompts == []


def test_deadline_not_in_digest_on_briefing_failure(tmp_path: Path):
    _write_json(tmp_path, "tasks.json", [_task_dict("1", "買い物")])
    _remember(tmp_path, "図書館の本を返す約束をした", until="2026-11-09")
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor(result="error: 子の側で問題が起きました")

    # 子が失敗して日報に倒れた場合も、期限の近い記憶は混ざらない
    text = check_and_post(cfg, now=NOW, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text is not None and "お知らせです" in text
    assert "図書館の本を返す約束をした" not in text
    assert len(spy.prompts) == 1  # 子への材料には期限が渡っている
    assert "図書館の本を返す約束をした" in spy.prompts[0]
