"""委任の権限の型（role）のテスト。

調べ役（research）の子に書き込み系の道具が登録されないこと、
分類表に無い道具が調べ役・計画役に出ないこと、計画役（plan）
は道具ゼロで計画文を返すこと、型の省略時は従来どおり（通常役）
であること、不正な型は error になること、委ね直し（AF-2）が
同じ型で行われること、活動の記録に型が残ることを確認する。
"""
from pathlib import Path

from kuma_agent.activity import load_activity_records
from kuma_agent.config import Config
from kuma_agent.delegate import DELEGATE_NOTICE
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.tools import (
    READ_ONLY_TOOLS,
    Tool,
    ToolBox,
    summarize_tool_args,
)

TASK_TEXT = "資料を調べて要点をまとめて"

# 読み取りではないと分類される道具（書き込み・実行・送信系）。
# 分類表（READ_ONLY_TOOLS）と重ならないことの確認に使う
WRITE_TOOLS = {
    "write_file", "run_shell", "create_task", "update_task",
    "set_work_steps", "tidy_memory", "save_skill",
    "create_job", "delete_job", "set_job_enabled",
    "create_watch", "delete_watch", "set_watch_enabled",
    "create_space", "update_space", "delete_space",
    "set_notify_mute", "clear_notify_mute",
    "deliver_file", "generate_image",
}


class _NullModel:
    """直接 Session を作るだけのときに使う、何もしないスタブ。"""

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "応答")


class _RoleFlowModel:
    """親は delegate を（role 指定つきで）呼び、子は報告を返す。

    子が見た道具名・システムプロンプト・依頼文を記録する。
    role=None のときは引数に role を入れない（省略時の確認用）。
    """

    def __init__(self, role: str | None = None,
                 child_reply: str = "子の最終報告") -> None:
        self.role = role
        self.child_reply = child_reply
        self.parent_calls = 0
        self.child_prompts: list[str] = []
        self.child_tool_names: list[list[str]] = []
        self.child_tasks: list[str] = []

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            self.child_prompts.append(system_prompt)
            self.child_tool_names.append([t["name"] for t in tools])
            self.child_tasks.append(messages[-1].content)
            return Message("assistant", self.child_reply)
        self.parent_calls += 1
        if self.parent_calls == 1:
            args: dict[str, str] = {"task": TASK_TEXT}
            if self.role is not None:
                args["role"] = self.role
            return Message("assistant", "", tool_calls=[
                {"id": "d1", "name": "delegate", "args": args}])
        return Message("assistant", f"親の答え: {messages[-1].content}")


def _make_parent(tmp_path: Path, monkeypatch, model,
                 **cfg_kwargs) -> tuple[Session, Config]:
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path / "data", **cfg_kwargs)
    return Session(cfg, model), cfg


def _make_child(tmp_path: Path, monkeypatch, role: str) -> Session:
    monkeypatch.chdir(tmp_path)
    cfg = Config(data_dir=tmp_path / "data")
    return Session(cfg, _NullModel(), session_id="delegate-test-1",
                   delegate_child=True, delegate_role=role)


def _names(session: Session) -> list[str]:
    return [t["name"] for t in session.tools.schemas()]


# ------------------------------------------------------- 調べ役の上限

def test_research_child_has_only_read_only_tools(
        tmp_path: Path, monkeypatch):
    model = _RoleFlowModel(role="research")
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    assert session.say("この仕事を頼む") == "親の答え: 子の最終報告"
    names = model.child_tool_names[0]
    # 調べ役の子の道具は分類表の中だけ（上限が効いている）
    assert names and set(names) <= READ_ONLY_TOOLS
    for expected in ("read_file", "list_tasks", "search_memory",
                     "web_search", "fetch_page", "list_jobs"):
        assert expected in names
    # 書き込み・実行系と、分類で外した道具は出ない
    for banned in WRITE_TOOLS | {"delegate", "ask_choice"}:
        assert banned not in names
    # 子への指示にも調べ役の旨が入る
    assert "読み取り専用の道具だけです" in model.child_prompts[0]


def test_research_child_call_of_write_tool_is_unknown(
        tmp_path: Path, monkeypatch):
    child = _make_child(tmp_path, monkeypatch, "research")
    # 見せないだけでなく、呼ばれても unknown tool として弾く
    assert child.tools.call(
        "write_file", {"path": "a.txt", "content": "x"}
    ) == "error: unknown tool write_file"
    assert child.tools.call(
        "run_shell", {"command": "echo hi"}
    ) == "error: unknown tool run_shell"
    # 読み取りの道具は使える
    assert "unknown tool" not in child.tools.call(
        "list_tasks", {})


def test_unlisted_tool_not_offered_to_capped_children(
        tmp_path: Path, monkeypatch):
    # 分類表に無い道具（将来足される道具や MCP 道具のつもり）は、
    # 上限が設定された子には登録自体されない
    for allowed in (READ_ONLY_TOOLS, frozenset()):
        box = ToolBox(tmp_path, allowed_tools=allowed)
        box.register(Tool("brand_new_tool", "新しい道具",
                          {"type": "object", "properties": {}},
                          lambda: "ok"))
        box.register(Tool("srv__ext_tool", "MCP 道具のつもり",
                          {"type": "object", "properties": {}},
                          lambda: "ok"))
        names = [t["name"] for t in box.schemas()]
        assert "brand_new_tool" not in names
        assert "srv__ext_tool" not in names
        assert box.call("brand_new_tool", {}) == \
            "error: unknown tool brand_new_tool"
    # 調べ役の上限では、分類表にある基本道具だけが残る
    box = ToolBox(tmp_path, allowed_tools=READ_ONLY_TOOLS)
    assert {t["name"] for t in box.schemas()} == {
        "now", "read_file", "web_search", "fetch_page"}
    # 計画役の上限（空集合）では道具ゼロ
    box = ToolBox(tmp_path, allowed_tools=frozenset())
    assert box.schemas() == []
    # 上限なし（従来どおり）では新道具も登録される
    box = ToolBox(tmp_path)
    box.register(Tool("brand_new_tool", "新しい道具",
                      {"type": "object", "properties": {}},
                      lambda: "ok"))
    assert "brand_new_tool" in [t["name"] for t in box.schemas()]


def test_read_only_table_matches_registered_tools(
        tmp_path: Path, monkeypatch):
    # 分類表が、現行の登録道具を漏れなく覆うこと（表にある名前は
    # 実在し、書き込み系とは重ならず、登録道具はどちらかに必ず
    # 分類される）。list_calendar_events はカレンダーのクライ
    # アント設定がある環境でのみ登録されるため、表側の実在確認
    # からだけ外す
    session, cfg = _make_parent(tmp_path, monkeypatch, _NullModel())
    names = set(_names(session))
    assert READ_ONLY_TOOLS - {"list_calendar_events"} <= names
    assert "list_calendar_events" in READ_ONLY_TOOLS
    assert not (READ_ONLY_TOOLS & WRITE_TOOLS)
    assert names - READ_ONLY_TOOLS - {"delegate"} == WRITE_TOOLS


# ------------------------------------------------------- 計画役

def test_plan_child_has_no_tools_and_returns_plan(
        tmp_path: Path, monkeypatch):
    plan_text = "計画: まず資料を集め、次に要点をまとめます"
    model = _RoleFlowModel(role="plan", child_reply=plan_text)
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    # 道具ゼロの子の最終応答（計画文）がそのまま親へ返る
    assert session.say("計画を立てて") == f"親の答え: {plan_text}"
    assert model.child_tool_names[0] == []
    assert "計画だけを担当します" in model.child_prompts[0]


def test_plan_child_toolbox_is_empty(tmp_path: Path, monkeypatch):
    child = _make_child(tmp_path, monkeypatch, "plan")
    assert child.tools.schemas() == []
    assert child.tools.call("read_file", {"path": "a.txt"}) == \
        "error: unknown tool read_file"


# ------------------------------------------------------- 通常役と省略時

def test_default_role_is_general_and_unchanged(
        tmp_path: Path, monkeypatch):
    model = _RoleFlowModel(role=None)  # role 引数自体を渡さない
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    session.say("頼む")
    child_names = set(model.child_tool_names[0])
    parent_names = set(_names(session))
    # 省略時の子は従来どおり道具一式（親から delegate を除いた分）
    assert child_names == parent_names - {"delegate"}
    assert "write_file" in child_names
    assert "run_shell" in child_names


def test_explicit_general_matches_default(tmp_path: Path, monkeypatch):
    child = _make_child(tmp_path, monkeypatch, "general")
    names = set(_names(child))
    assert "write_file" in names and "create_task" in names
    assert "delegate" not in names  # 子には従来どおり登録しない


# ------------------------------------------------------- 不正な型

def test_invalid_role_returns_error(tmp_path: Path, monkeypatch):
    model = _RoleFlowModel()
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    result = session.tools.call(
        "delegate", {"task": "仕事", "role": "bogus"})
    assert result.startswith("error:")
    assert "型の指定が不正です" in result
    # 通常役へ倒して実行してはいない（子は作られていない）
    assert model.child_tool_names == []
    conv = cfg.user_data_dir / "conversations"
    children = list(conv.glob("delegate-*.jsonl")) if conv.is_dir() else []
    assert children == []


# ------------------------------------------------------- 委ね直し

class _FlakyRoleModel(_RoleFlowModel):
    """1 回目の子は例外で落ち、2 回目の子は報告を返すスタブ。

    子の呼び出しごとに道具名を記録し、委ね直しでも型（道具の
    上限）が変わらないことを確かめるために使う。
    """

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            self.child_tool_names.append([t["name"] for t in tools])
            if len(self.child_tool_names) == 1:
                raise RuntimeError("1 回目の子が落ちた")
            return Message("assistant", "立て直し後の報告")
        return super().chat(system_prompt, messages, tools)


def test_retry_keeps_same_role(tmp_path: Path, monkeypatch):
    model = _FlakyRoleModel(role="research")
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    answer = session.say("頼む")
    assert "立て直し後の報告" in answer
    # 子は 2 回作られ、両方とも調べ役の上限のまま（広い型への
    # 自動切り替えはしない）
    assert len(model.child_tool_names) == 2
    for names in model.child_tool_names:
        assert names and set(names) <= READ_ONLY_TOOLS
        assert "write_file" not in names


# ------------------------------------------------------- 活動の記録

def test_activity_summary_records_role(tmp_path: Path, monkeypatch):
    # 要約そのもの: 型と課題が残り、省略時は通常役になる
    assert summarize_tool_args(
        "delegate", {"task": TASK_TEXT, "role": "research"}
    ) == f"型: 調べ役／課題: {TASK_TEXT}"
    assert summarize_tool_args(
        "delegate", {"task": TASK_TEXT}
    ) == f"型: 通常役／課題: {TASK_TEXT}"
    assert summarize_tool_args(
        "delegate", {"task": TASK_TEXT, "role": "plan"}
    ) == f"型: 計画役／課題: {TASK_TEXT}"


def test_activity_record_includes_role(tmp_path: Path, monkeypatch):
    model = _RoleFlowModel(role="research")
    session, cfg = _make_parent(tmp_path, monkeypatch, model)
    session.say("頼む")
    records = [r for r in load_activity_records(cfg.user_data_dir)
               if r.get("tool") == "delegate"]
    assert records, "delegate の活動の記録が残っていない"
    assert any(r["summary"].startswith("型: 調べ役")
               for r in records)
