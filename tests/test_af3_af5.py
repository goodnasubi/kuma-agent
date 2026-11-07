"""AF-3（手順の再開の声かけ）と AF-5（目標のプロンプトへの
読み取り）のテスト。

AF-3: 手順の項目に updated_at（最後に状態が変わった日時）が
付き、旧データでも壊れないこと。朝のまとめの材料に、進行中・
停滞の手順だけが日数つきで入り、完了・中止・未着手は入らない
こと。日報への倒し込みには混ざらないこと。

AF-5: 進行中の目標が、紐づいた用事の完了割合つきで会話の
プロンプトに載ること。目標が無い・全て達成済みの場合は節が
出ないこと。両モードで載ること。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.proactive import check_and_post, open_steps_text
from kuma_agent.session import Session
from kuma_agent.worksteps import (
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    WorkSteps,
)

# 2026-11-03 は火曜日。まとめの既定時刻は 07:30
AFTER = datetime(2026, 11, 3, 8, 0, 0)
SUMMARY = "おはようございます。今日のまとめです。"


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


class _SpyExecutor:
    """朝のまとめの子の差し替え。指示文を記録し、決めた結果を返す。"""

    def __init__(self, result: str = SUMMARY) -> None:
        self.result = result
        self.prompts: list[str] = []

    def __call__(self, parent, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.result


def _write_json(user_dir: Path, name: str, data: object) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / name).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _task(task_id: str, title: str, status: str = "open",
          goal_id: str = "") -> dict:
    stamp = AFTER.isoformat(timespec="seconds")
    return {"id": task_id, "title": title, "status": status, "memo": "",
            "goal_id": goal_id,
            "created_at": stamp, "updated_at": stamp}


def _goal(goal_id: str, title: str, status: str = "active") -> dict:
    return {"id": goal_id, "title": title, "description": "",
            "status": status,
            "created_at": AFTER.isoformat(timespec="seconds"),
            "notes": []}


def _records(user_dir: Path) -> list[dict]:
    path = user_dir / "conversations" / "main.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ------------------------------------------------------------ AF-3: updated_at

def test_updated_at_stamped_and_preserved(tmp_path: Path):
    steps = WorkSteps(tmp_path)
    t0 = datetime(2026, 11, 1, 9, 0, 0)
    steps.set_items([("調べる", STATUS_IN_PROGRESS),
                     ("まとめる", STATUS_PENDING)], now=t0)
    loaded = steps.items()
    assert [s.updated_at for s in loaded] == [
        "2026-11-01T09:00:00", "2026-11-01T09:00:00"]
    # ファイルにも updated_at が残る
    raw = json.loads((tmp_path / "work-steps" / "main.json")
                     .read_text(encoding="utf-8"))
    assert raw[0]["updated_at"] == "2026-11-01T09:00:00"

    # 状態が変わった項目だけ新しい日時になり、変わらない項目は
    # 前回の日時を引き継ぐ
    t1 = datetime(2026, 11, 3, 9, 0, 0)
    steps.set_items([("調べる", STATUS_COMPLETED),
                     ("まとめる", STATUS_PENDING)], now=t1)
    by_text = {s.text: s for s in steps.items()}
    assert by_text["調べる"].updated_at == "2026-11-03T09:00:00"
    assert by_text["まとめる"].updated_at == "2026-11-01T09:00:00"


def test_old_data_without_updated_at(tmp_path: Path):
    path = tmp_path / "work-steps" / "main.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(
        [{"text": "旧い手順", "status": "blocked"},
         {"text": "日時の無い手順", "status": "in_progress",
          "updated_at": 123}]  # 形が不正な日時は空として読む
    ), encoding="utf-8")

    loaded = WorkSteps(tmp_path).items()
    assert [s.updated_at for s in loaded] == ["", ""]
    # 壊れずに使え、朝のまとめの材料にも日数なしで出る
    text = open_steps_text(tmp_path, AFTER)
    assert text is not None
    assert "旧い手順" in text and "日この状態のまま" not in text
    # 次に保存し直せば日時が付く
    WorkSteps(tmp_path).set_items(
        [("旧い手順", STATUS_BLOCKED)], now=AFTER)
    assert WorkSteps(tmp_path).items()[0].updated_at == \
        "2026-11-03T08:00:00"


# ------------------------------------------------------------ AF-3: まとめの材料

def test_open_steps_text_picks_only_in_progress_and_blocked(
        tmp_path: Path):
    steps = WorkSteps(tmp_path)
    steps.set_items([
        ("進んでいる作業", STATUS_IN_PROGRESS),
        ("止まっている作業", STATUS_BLOCKED),
        ("これからの作業", STATUS_PENDING),
        ("終わった作業", STATUS_COMPLETED),
        ("やめた作業", STATUS_CANCELLED),
    ], now=datetime(2026, 10, 30, 10, 0, 0))

    text = open_steps_text(tmp_path, AFTER)
    assert text is not None
    assert "進んでいる作業" in text
    assert "止まっている作業" in text
    # 未着手・完了・中止は再開の声かけの対象ではない
    assert "これからの作業" not in text
    assert "終わった作業" not in text
    assert "やめた作業" not in text
    # 10/30 10:00 → 11/3 08:00 は 3 日
    assert "3日この状態のまま" in text


def test_open_steps_text_none_and_cap(tmp_path: Path):
    assert open_steps_text(tmp_path, AFTER) is None
    steps = WorkSteps(tmp_path)
    steps.set_items([("未着手だけ", STATUS_PENDING)], now=AFTER)
    assert open_steps_text(tmp_path, AFTER) is None

    steps.set_items([(f"作業{i}", STATUS_BLOCKED) for i in range(5)],
                    now=AFTER)
    text = open_steps_text(tmp_path, AFTER)
    assert text is not None
    assert len(text.splitlines()) == 3  # 最大 3 件まで


def test_briefing_includes_open_steps(tmp_path: Path):
    steps = WorkSteps(tmp_path)
    steps.set_items([("止まっている作業", STATUS_BLOCKED)],
                    now=datetime(2026, 10, 31, 9, 0, 0))
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    # 用事が無くても、手順の残り自体が子を動かす材料になる
    text = check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == SUMMARY
    assert len(spy.prompts) == 1
    prompt = spy.prompts[0]
    assert "エージェントの作業の手順の残り" in prompt
    assert "止まっている作業" in prompt
    assert "2日この状態のまま" in prompt
    assert len(_records(tmp_path)) == 1


def test_steps_not_in_digest_when_briefing_off(tmp_path: Path):
    _write_json(tmp_path, "tasks.json", [_task("1", "買い物")])
    steps = WorkSteps(tmp_path)
    steps.set_items([("止まっている作業", STATUS_BLOCKED)], now=AFTER)
    cfg = Config(data_dir=tmp_path, briefing_time="off")
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()

    # 朝のまとめが無効なら日報だけが出て、手順の残りは混ざらない
    text = check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text is not None and "お知らせです" in text
    assert "買い物" in text
    assert "止まっている作業" not in text
    assert spy.prompts == []


def test_steps_not_in_digest_on_briefing_failure(tmp_path: Path):
    _write_json(tmp_path, "tasks.json", [_task("1", "買い物")])
    steps = WorkSteps(tmp_path)
    steps.set_items([("止まっている作業", STATUS_BLOCKED)], now=AFTER)
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor(result="error: 子の側で問題が起きました")

    # 子が失敗して日報に倒れた場合も、手順の残りは混ざらない
    text = check_and_post(cfg, now=AFTER, session_provider=lambda: session,
                          briefing_executor=spy)
    assert text is not None and "お知らせです" in text
    assert "止まっている作業" not in text
    assert len(spy.prompts) == 1  # 子への材料には手順が渡っている
    assert "止まっている作業" in spy.prompts[0]


# ------------------------------------------------------------ AF-5: 目標の節

def test_active_goals_in_prompt_with_progress(tmp_path: Path):
    _write_json(tmp_path, "goals.json", [
        _goal("1", "引っ越しの準備"),
        _goal("2", "達成した目標", status="done"),
    ])
    _write_json(tmp_path, "tasks.json", [
        _task("1", "段ボールを買う", status="done", goal_id="1"),
        _task("2", "住所変更の届け出", status="open", goal_id="1"),
        _task("3", "目標と無関係な用事"),
    ])

    session = Session(Config(data_dir=tmp_path), _EchoModel())
    assert "# 進行中の目標" in session.system_prompt
    assert "- 引っ越しの準備（2件中1件完了）" in session.system_prompt
    # 達成済みの目標は節に出ない
    assert "達成した目標" not in session.system_prompt


def test_goal_without_linked_tasks_has_no_progress(tmp_path: Path):
    _write_json(tmp_path, "goals.json", [_goal("1", "貯金")])

    session = Session(Config(data_dir=tmp_path), _EchoModel())
    assert "# 進行中の目標" in session.system_prompt
    assert "- 貯金" in session.system_prompt
    assert "件中" not in session.system_prompt


def test_no_goals_no_section(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    assert "進行中の目標" not in session.system_prompt

    # 全て達成済みでも節は出ない
    _write_json(tmp_path, "goals.json",
                [_goal("1", "終わった目標", status="done")])
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    assert "進行中の目標" not in session.system_prompt


def test_goals_capped_in_prompt(tmp_path: Path):
    _write_json(tmp_path, "goals.json",
                [_goal(str(i), f"目標{i}") for i in range(1, 8)])

    session = Session(Config(data_dir=tmp_path), _EchoModel())
    # GoalBoard は新しい順に返すため、先頭 5 件＋「ほか 2 件」
    assert "ほか 2 件" in session.system_prompt
    assert "- 目標3" in session.system_prompt
    assert "- 目標2" not in session.system_prompt


def test_goals_in_prompt_wall_mode(tmp_path: Path):
    _write_json(tmp_path, "goals.json", [_goal("1", "引っ越しの準備")])
    _write_json(tmp_path, "tasks.json", [
        _task("1", "段ボールを買う", status="done", goal_id="1")])

    cfg = Config(data_dir=tmp_path, mode="wall")
    session = Session(cfg, _EchoModel(), "work")
    # 目標は利用者ごとの置き場なので、スレッドモードでも載る
    assert "# 進行中の目標" in session.system_prompt
    assert "- 引っ越しの準備（1件中1件完了）" in session.system_prompt
