"""委任（delegate 道具）のテスト。

スタブ頭脳で、親が delegate を呼ぶと子セッションが作られて仕事が
渡り、最終応答が親へ返ること、子の記録が delegate- で始まる別
セッションとして残ること、子には delegate 道具が無いこと、
KUMA_DELEGATE_MODEL の指定が子に効くこと、子の例外・時間切れでも
親に error が返って会話が続くこと、承認が子にも継承されること、
時間切れのときに協調キャンセルで子が止まることを確認する。
"""
import json
import threading
import time
from pathlib import Path

from kuma_agent import delegate as delegate_module
from kuma_agent.config import Config
from kuma_agent.delegate import DELEGATE_NOTICE
from kuma_agent.llm import Message
from kuma_agent.session import CANCELLED_REPLY, Session
from kuma_agent.tools import (
    APPROVAL_DENIED_RESULT,
    CANCELLED_RESULT,
    ToolBox,
    activity_label,
)

TASK_TEXT = "資料を調べて要点をまとめて"


class _FlowModel:
    """親は delegate を呼び、子は最終報告を返すスタブ。

    親と子は（モデル指定が無い場合）同じモデルを共用するため、
    子のシステムプロンプトにだけ入る委任の一文で見分ける。
    子が見た依頼文と道具名も記録する。
    """

    def __init__(self) -> None:
        self.parent_calls = 0
        self.child_task: str | None = None
        self.child_tool_names: list[str] = []

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            self.child_task = messages[-1].content
            self.child_tool_names = [t["name"] for t in tools]
            return Message("assistant", "子の最終報告")
        self.parent_calls += 1
        if self.parent_calls == 1:
            return Message("assistant", "", tool_calls=[
                {"id": "d1", "name": "delegate",
                 "args": {"task": TASK_TEXT}}])
        return Message("assistant", f"親の答え: {messages[-1].content}")


def _make_parent(tmp_path: Path, monkeypatch, model,
                 **cfg_kwargs) -> tuple[Session, Config]:
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path / "data", **cfg_kwargs)
    return Session(cfg, model), cfg


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _child_files(cfg: Config) -> list[Path]:
    conv = cfg.user_data_dir / "conversations"
    return sorted(conv.glob("delegate-*.jsonl")) if conv.is_dir() else []


# ---------------------------------------------------------------- 往復

def test_delegate_round_trip(tmp_path: Path, monkeypatch):
    model = _FlowModel()
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    assert session.say("この仕事を頼む") == "親の答え: 子の最終報告"
    # 子には依頼文がそのまま渡り、最終応答が親へ返った
    assert model.child_task == TASK_TEXT
    # 子の会話は delegate- で始まる別セッションとして残る
    children = _child_files(cfg)
    assert len(children) == 1
    records = _records(children[0])
    assert [(r["role"], r["content"]) for r in records] == [
        ("user", TASK_TEXT), ("assistant", "子の最終報告")]
    # 親の記録には子の発話は混ざらない（親自身の発話だけ）
    main_records = _records(cfg.user_data_dir / "conversations" / "main.jsonl")
    contents = [r["content"] for r in main_records]
    assert TASK_TEXT not in contents
    assert "子の最終報告" not in contents


def test_child_has_no_delegate_tool(tmp_path: Path, monkeypatch):
    model = _FlowModel()
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    session.say("頼む")
    # 子には道具一式はあるが delegate 自体はない（委任は1段まで）
    assert "delegate" not in model.child_tool_names
    assert "read_file" in model.child_tool_names
    assert "list_tasks" in model.child_tool_names  # 相棒モードの構成
    # 親には delegate がある
    parent_names = [t["name"] for t in session.tools.schemas()]
    assert "delegate" in parent_names


def test_delegate_session_ids_are_unique(tmp_path: Path, monkeypatch):
    model = _FlowModel()
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    # 道具を直接2回呼んでも子のセッション ID は重ならない
    assert session.tools.call("delegate", {"task": "仕事1"}) == "子の最終報告"
    assert session.tools.call("delegate", {"task": "仕事2"}) == "子の最終報告"
    assert len(_child_files(cfg)) == 2


def test_child_status_is_relayed_to_parent(tmp_path: Path, monkeypatch):
    model = _FlowModel()
    statuses: list[str] = []
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path / "data")
    session = Session(cfg, model, on_status=statuses.append)
    session.say("頼む")
    assert any(s.startswith("別のエージェントが作業中") for s in statuses)


def test_activity_label_for_delegate():
    assert activity_label("delegate") == "別のエージェントに作業を頼んでいます"


# ---------------------------------------------------------------- モデルの指定

def test_delegate_model_env_is_read(monkeypatch):
    monkeypatch.setenv("KUMA_DELEGATE_MODEL", "env-child-model")
    monkeypatch.setenv("KUMA_DELEGATE_TIMEOUT_SECONDS", "42")
    cfg = Config(data_dir=Path("data"))
    assert cfg.delegate_model == "env-child-model"
    assert cfg.delegate_timeout_seconds == 42


def test_delegate_timeout_nonpositive_falls_back(monkeypatch):
    monkeypatch.setenv("KUMA_DELEGATE_TIMEOUT_SECONDS", "0")
    cfg = Config(data_dir=Path("data"))
    assert cfg.delegate_timeout_seconds == 600


class _OverrideChildModel:
    """モデル上書き時に create_client の代わりに使われる子のスタブ。"""

    def chat(self, system_prompt, messages, tools):
        assert DELEGATE_NOTICE in system_prompt
        return Message("assistant", "上書きモデルの子の報告")


def test_delegate_model_override_is_used(tmp_path: Path, monkeypatch):
    model = _FlowModel()
    session, cfg = _make_parent(
        tmp_path, monkeypatch, model, delegate_model="special-child-model")
    seen: dict[str, object] = {}

    def fake_create_client(provider, api_key="", model=None, **kwargs):
        seen.update(provider=provider, model=model)
        return _OverrideChildModel()

    monkeypatch.setattr(delegate_module, "create_client", fake_create_client)
    # 親は同じプロバイダ・指定モデルで子のクライアントが作られ、
    # 子の応答がそのまま親へ返る（親と子の共用スタブは使われない）
    assert session.say("頼む") == "親の答え: 上書きモデルの子の報告"
    assert seen == {"provider": cfg.provider, "model": "special-child-model"}
    assert model.child_task is None


# ---------------------------------------------------------------- 失敗と時間切れ

class _FailingChildModel(_FlowModel):
    """子として呼ばれると例外を投げるスタブ。"""

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            raise RuntimeError("子の頭脳が落ちた")
        return super().chat(system_prompt, messages, tools)


def test_child_exception_returns_error_and_parent_continues(
        tmp_path: Path, monkeypatch):
    model = _FailingChildModel()
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    answer = session.say("頼む")
    assert answer.startswith("親の答え: error:")
    assert "子の頭脳が落ちた" in answer


class _SlowChildModel(_FlowModel):
    """子として呼ばれると時間切れより長く眠るスタブ。"""

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            time.sleep(3)
            return Message("assistant", "遅い報告")
        return super().chat(system_prompt, messages, tools)


def test_child_timeout_returns_error_and_parent_continues(
        tmp_path: Path, monkeypatch):
    model = _SlowChildModel()
    session, cfg = _make_parent(
        tmp_path, monkeypatch, model, delegate_timeout_seconds=1)
    answer = session.say("頼む")
    assert answer.startswith("親の答え: error:")
    assert "時間内に終わりませんでした" in answer


# ---------------------------------------------------------------- 承認の継承

class _ShellChildModel(_FlowModel):
    """子は run_shell を使い、その結果を報告するスタブ。"""

    def __init__(self) -> None:
        super().__init__()
        self.child_calls = 0

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            self.child_calls += 1
            if self.child_calls == 1:
                return Message("assistant", "", tool_calls=[
                    {"id": "c1", "name": "run_shell",
                     "args": {"command": "echo child-shell-out"}}])
            return Message("assistant", f"子の報告: {messages[-1].content}")
        return super().chat(system_prompt, messages, tools)


def test_child_tools_use_parents_approver(tmp_path: Path, monkeypatch):
    model = _ShellChildModel()
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    seen: list[tuple[str, dict]] = []

    def approver(name: str, args: dict) -> bool:
        seen.append((name, args))
        return True

    session.approver = approver
    # 親の承認者が子の run_shell にも呼ばれ、承認されれば実行される
    assert session.say("頼む") == "親の答え: 子の報告: child-shell-out"
    assert seen == [("run_shell", {"command": "echo child-shell-out"})]


def test_child_tools_denied_without_approver(tmp_path: Path, monkeypatch):
    model = _ShellChildModel()
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    # 承認者が付いていない経路では、子でも安全側に倒して却下する
    assert session.approver is None
    answer = session.say("頼む")
    assert f"子の報告: {APPROVAL_DENIED_RESULT}" in answer


# ---------------------------------------------------------------- 協調キャンセル

def test_normal_session_has_unset_cancel_event(
        tmp_path: Path, monkeypatch):
    # 通常のセッション（委任でない）では印は渡されず、未設定の
    # Event を自分で持つだけで、挙動は従来どおり
    session, cfg = _make_parent(tmp_path, monkeypatch, _FlowModel())
    assert not session.cancel_event.is_set()
    assert session.tools.cancel_event is session.cancel_event


def test_session_with_cancel_event_set_is_cancelled_immediately(
        tmp_path: Path, monkeypatch):
    # 印が最初から立っている Session は、モデルを呼ばずに
    # キャンセルの文言で会話を打ち切る
    model = _FlowModel()
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path / "data")
    event = threading.Event()
    event.set()
    session = Session(cfg, model, cancel_event=event)
    assert session.say("頼む") == CANCELLED_REPLY
    assert model.parent_calls == 0
    # 最終応答は通常の完了と同じく会話記録に残る
    records = _records(cfg.user_data_dir / "conversations" / "main.jsonl")
    assert records[-1]["role"] == "assistant"
    assert records[-1]["content"] == CANCELLED_REPLY


def test_toolbox_call_is_short_circuited_by_cancel_event(
        tmp_path: Path):
    box = ToolBox(tmp_path)
    (tmp_path / "a.txt").write_text("中身", encoding="utf-8")
    # 印が無ければ（None の間は）従来どおり実行される
    assert box.call("read_file", {"path": "a.txt"}) == "中身"
    event = threading.Event()
    box.cancel_event = event
    # 未設定の間も従来どおり
    assert box.call("read_file", {"path": "a.txt"}) == "中身"
    event.set()
    # 立った後は道具を実行せず、キャンセルの結果を返す
    assert box.call("read_file", {"path": "a.txt"}) == CANCELLED_RESULT


class _LoopingChildModel(_FlowModel):
    """子として呼ばれると道具を呼び続けるスタブ。

    1 回の応答ごとに少し眠るため、制限時間（1 秒）の間に道具
    ループが何周も回り、時間切れの後も止まらなければ回り続ける。
    """

    def __init__(self) -> None:
        super().__init__()
        self.child_calls = 0

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            self.child_calls += 1
            time.sleep(0.2)
            return Message("assistant", "", tool_calls=[
                {"id": f"c{self.child_calls}", "name": "list_tasks",
                 "args": {}}])
        return super().chat(system_prompt, messages, tools)


def test_child_stops_at_checkpoint_after_timeout(
        tmp_path: Path, monkeypatch):
    model = _LoopingChildModel()
    session, cfg = _make_parent(
        tmp_path, monkeypatch, model, delegate_timeout_seconds=1)
    answer = session.say("頼む")
    # 親には従来どおり時間切れの error が返り、会話は続く
    assert answer.startswith("親の答え: error:")
    assert "時間内に終わりませんでした" in answer
    # 子は協調キャンセルで止まり、スレッド自体も終わる
    assert delegate_module.wait_child_threads(5)
    stopped_at = model.child_calls
    assert stopped_at >= 1
    time.sleep(0.5)
    assert model.child_calls == stopped_at
    # 子の会話記録の最後は、打ち切りの文言が最終応答として残る
    children = _child_files(cfg)
    assert len(children) == 1
    records = _records(children[0])
    assert records[-1]["role"] == "assistant"
    assert records[-1]["content"] == CANCELLED_REPLY
