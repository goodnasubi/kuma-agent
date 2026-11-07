"""選択肢での質問（ask_choice、AF-4、0.73.0）。

確認すること:
- (a) 質問が出て、選んだ答えがモデルへ返り、会話が続く。答えは
  会話記録にも利用者の発話として残る
- (b) 文章での回答（allow_text）が受けられる
- (c) 時間切れでは「答えが無かった」がモデルへ返り、文章での
  質問に切り替えられる。ChoiceBroker 単体でも時間切れは None
- (d) 受け皿の無い経路（応答者なし・委任の子）では道具が
  スキーマに出ない。直接呼んでも「使えない」が返る
- (e) 承認フローと混ざらない: ask_choice は承認対象にならず、
  呼び出しで承認者が呼ばれない
- ChoiceBroker の単体（ラベル・文章の可否、テナント分離）
- Web API の流れ（保留の取得と回答で会話が完了する）
- CLI / TUI の応答者の読み替え
- 活動の記録に質問と答えが残る
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from kuma_agent.cli import _cli_responder
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import (
    CHOICE_NO_ANSWER_RESULT, CHOICE_UNAVAILABLE_RESULT, Session)
from kuma_agent.tools import requires_approval
from kuma_agent.tui import TuiResponder, choice_prompt_text
from kuma_agent.web import ChoiceBroker, build_server


class ChoiceStubModel:
    """1 回目は ask_choice を呼び、2 回目は結果をそのまま返す頭脳。"""

    model = "stub-choice"

    def __init__(self, allow_text: bool = True):
        self.calls = 0
        self.allow_text = allow_text
        self.last_tool_names: list[str] = []

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        self.last_tool_names = [t["name"] for t in tools]
        if self.calls == 1:
            args = {"question": "どちらで作りますか？",
                    "options": [{"label": "表", "note": "一覧で見やすい"},
                                {"label": "文章"}]}
            if self.allow_text:
                args["allow_text"] = True
            return Message("assistant", "", tool_calls=[
                {"id": "c1", "name": "ask_choice", "args": args}])
        return Message("assistant", f"結果: {messages[-1].content}")


def make_session(tmp_path, model=None, **kwargs) -> Session:
    cfg = Config(data_dir=tmp_path / "data")
    return Session(cfg, model or ChoiceStubModel(), **kwargs)


def _option_labels(session: Session) -> list[str]:
    return [t["name"] for t in session.tools.schemas()]


# ------------------------------------------------ (a) 選択した答え

def test_choice_answer_reaches_model_and_record(tmp_path):
    """質問が出て、選んだ答えがモデルへ返り、会話が続く。"""
    seen = {}

    def responder(question, options, allow_text):
        seen["question"] = question
        seen["options"] = options
        seen["allow_text"] = allow_text
        return "表"

    model = ChoiceStubModel()
    session = make_session(tmp_path, model, responder=responder)
    reply = session.say("作って")

    # 応答者へは質問・選択肢・allow_text が正規化されて渡る
    assert seen["question"] == "どちらで作りますか？"
    assert seen["options"] == [
        {"label": "表", "note": "一覧で見やすい"}, {"label": "文章"}]
    assert seen["allow_text"] is True
    # 答えが道具の結果としてモデルへ返り、最終返事に反映される
    assert reply == "結果: 利用者の答え: 表"
    assert "ask_choice" in model.last_tool_names
    # 会話記録に、利用者の発話として質問と答えが残る
    turns = session.memory.load_turns()
    answers = [t for t in turns if t["role"] == "user"
               and "への答え" in t["content"]]
    assert len(answers) == 1
    assert "どちらで作りますか？" in answers[0]["content"]
    assert "表" in answers[0]["content"]


def test_choice_records_activity_with_question_and_answer(tmp_path):
    """活動の記録（activity.jsonl）に質問と答えの要約が残る。"""
    session = make_session(tmp_path, responder=lambda q, o, a: "文章")
    session.say("作って")
    activity = (tmp_path / "data" / "activity.jsonl").read_text(
        encoding="utf-8")
    entries = [json.loads(line) for line in activity.splitlines()]
    calls = [e for e in entries if e.get("kind") == "tool_call"
             and e.get("tool") == "ask_choice"]
    assert len(calls) == 1
    assert "どちらで作りますか？" in calls[0]["summary"]
    assert "利用者の答え: 文章" in calls[0]["result"]


# ------------------------------------------------ (b) 文章での回答

def test_choice_free_text_answer(tmp_path):
    """allow_text のとき、文章の答えもそのままモデルへ返る。"""
    session = make_session(
        tmp_path, responder=lambda q, o, a: "表で、色つきで")
    assert session.say("作って") == "結果: 利用者の答え: 表で、色つきで"


def test_choice_broker_text_answer_only_when_allowed(tmp_path):
    """ChoiceBroker: 文章の答えは allow_text の質問だけ受け付ける。"""
    broker = ChoiceBroker()
    result: dict = {}

    def ask(allow_text):
        result["answer"] = broker.request(
            "t", "u", "質問", [{"label": "はい"}, {"label": "いいえ"}],
            allow_text, timeout=5)

    thread = threading.Thread(target=ask, args=(False,))
    thread.start()
    deadline = time.time() + 5
    while not broker.list_pending("t", "u") and time.time() < deadline:
        time.sleep(0.01)
    pending = broker.list_pending("t", "u")
    assert len(pending) == 1
    # allow_text でない質問へ文章の答えは受け付けない
    assert broker.answer("t", "u", pending[0]["id"], "どちらでも") is False
    # ラベルの答えは受け付ける
    assert broker.answer("t", "u", pending[0]["id"], "はい") is True
    thread.join(timeout=5)
    assert result["answer"] == "はい"

    thread2 = threading.Thread(target=ask, args=(True,))
    thread2.start()
    deadline = time.time() + 5
    while not broker.list_pending("t", "u") and time.time() < deadline:
        time.sleep(0.01)
    pending = broker.list_pending("t", "u")
    assert len(pending) == 1
    assert broker.answer("t", "u", pending[0]["id"], "PDFで") is True
    thread2.join(timeout=5)
    assert result["answer"] == "PDFで"


def test_choice_broker_isolated_by_tenant_and_user():
    """別テナント・別ユーザーの保留は見えず、答えられない。"""
    broker = ChoiceBroker()
    thread = threading.Thread(target=lambda: broker.request(
        "t1", "u1", "質問", [{"label": "A"}, {"label": "B"}],
        False, timeout=5))
    thread.start()
    deadline = time.time() + 5
    while not broker.list_pending("t1", "u1") and time.time() < deadline:
        time.sleep(0.01)
    pending = broker.list_pending("t1", "u1")
    assert len(pending) == 1
    assert broker.list_pending("t2", "u1") == []
    assert broker.list_pending("t1", "u2") == []
    assert broker.answer("t2", "u1", pending[0]["id"], "A") is False
    assert broker.answer("t1", "u2", pending[0]["id"], "A") is False
    assert broker.answer("t1", "u1", "no-such-id", "A") is False
    assert broker.answer("t1", "u1", pending[0]["id"], "A") is True
    thread.join(timeout=5)


# ------------------------------------------------ (c) 時間切れ

def test_choice_timeout_returns_no_answer_result(tmp_path):
    """答えが無いまま時間切れになると、文章で聞き直す結果が返る。"""
    session = make_session(tmp_path, responder=lambda q, o, a: None)
    assert session.say("作って") == f"結果: {CHOICE_NO_ANSWER_RESULT}"


def test_choice_broker_timeout_returns_none():
    broker = ChoiceBroker()
    started = time.time()
    answer = broker.request("t", "u", "質問",
                            [{"label": "A"}, {"label": "B"}],
                            False, timeout=0.2)
    assert answer is None
    assert time.time() - started < 2
    assert broker.list_pending("t", "u") == []


# ------------------------------------------------ (d) 受け皿のない経路

def test_no_responder_no_tool(tmp_path):
    """応答者がいない経路では ask_choice が道具に出ない。"""
    session = make_session(tmp_path)
    assert "ask_choice" not in _option_labels(session)
    assert session._ask_choice("質問", ["a", "b"]) == CHOICE_UNAVAILABLE_RESULT


def test_delegate_child_has_no_choice_tool(tmp_path):
    """委任の子は、応答者を渡しても道具が出ない（対面していない）。"""
    child = make_session(
        tmp_path, session_id="delegate-x", delegate_child=True,
        responder=lambda q, o, a: "a")
    assert "ask_choice" not in _option_labels(child)
    assert child._ask_choice("質問", ["a", "b"]) == CHOICE_UNAVAILABLE_RESULT


def test_choice_validation_errors(tmp_path):
    """選択肢の形が違う呼び出しは error の結果になる。"""
    session = make_session(tmp_path, responder=lambda q, o, a: "a")
    assert "ask_choice" in _option_labels(session)
    assert session._ask_choice("質問", ["a"]).startswith("error:")
    assert session._ask_choice(
        "質問", ["a", "b", "c", "d", "e"]).startswith("error:")
    assert session._ask_choice("質問", [{"label": ""}, {"label": "b"}]) \
        .startswith("error:")
    assert session._ask_choice(" ", ["a", "b"]).startswith("error:")


# ------------------------------------------------ (e) 承認と混ざらない

def test_ask_choice_is_not_an_approval_target(tmp_path):
    """ask_choice は承認対象でなく、呼び出しで承認者が呼ばれない。"""
    assert requires_approval("ask_choice") is False
    approvals: list = []

    def approver(name, args):
        approvals.append(name)
        return False  # テナント側で承認が有効でも、対象外なら効かない

    cfg = Config(data_dir=tmp_path / "data")
    session = Session(cfg, ChoiceStubModel(), approver=approver,
                      responder=lambda q, o, a: "表")
    reply = session.say("作って")
    assert reply == "結果: 利用者の答え: 表"
    assert approvals == []


# ------------------------------------------------ CLI / TUI の応答者

def test_cli_responder_reads_number_and_text(monkeypatch):
    options = [{"label": "表", "note": "一覧"}, {"label": "文章"}]
    answers = iter(["2"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert _cli_responder("質問", options, False) == "文章"
    # 文章での回答を許したときは、番号以外の入力を文章として返す
    answers = iter(["PDFで"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert _cli_responder("質問", options, True) == "PDFで"
    # 許していないときの文章・範囲外・空入力は答えなし
    for raw in ("PDFで", "9", ""):
        answers = iter([raw])
        monkeypatch.setattr(
            "builtins.input", lambda prompt="", _a=answers: next(_a))
        assert _cli_responder("質問", options, False) is None


def test_tui_responder_answer_and_timeout():
    responder = TuiResponder(timeout=5)
    result: dict = {}
    thread = threading.Thread(
        target=lambda: result.update(
            answer=responder(
                "どちら？", [{"label": "A"}, {"label": "B"}], False)))
    thread.start()
    deadline = time.time() + 5
    while responder.pending() is None and time.time() < deadline:
        time.sleep(0.01)
    pending = responder.pending()
    assert pending is not None
    assert "1. A" in choice_prompt_text(pending)
    # 文章での回答を許していない質問への文章は受け付けない
    assert responder.answer("どちらでも") is False
    assert responder.pending() is not None
    assert responder.answer("2") is True
    thread.join(timeout=5)
    assert result["answer"] == "B"

    slow = TuiResponder(timeout=0.2)
    assert slow("質問", [{"label": "A"}, {"label": "B"}], False) is None


# ------------------------------------------------ Web の流れ

def _request(port: int, method: str, path: str, payload: dict | None = None):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    data = response.read().decode("utf-8")
    conn.close()
    return response.status, json.loads(data) if data else {}


@pytest.fixture
def choice_server(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    session = Session(cfg, ChoiceStubModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_web_choice_flow(choice_server):
    """会話が質問で止まり、保留が出て、回答で会話が完了する。"""
    port = choice_server
    result: dict = {}

    def chat():
        result["response"] = _request(port, "POST", "/api/chat", {
            "message": "作って", "tenant": "default", "user": "default",
            "session": "main"})

    thread = threading.Thread(target=chat)
    thread.start()
    pending = []
    deadline = time.time() + 10
    while time.time() < deadline:
        _, body = _request(port, "GET",
                           "/api/choices?tenant=default&user=default")
        if body.get("choices"):
            pending = body["choices"]
            break
        time.sleep(0.05)
    assert len(pending) == 1
    item = pending[0]
    assert item["question"] == "どちらで作りますか？"
    assert [o["label"] for o in item["options"]] == ["表", "文章"]
    assert item["allow_text"] is True
    # 不正な答え（選択肢にも文章にも当たらない空）は 404 にならず
    # 400（本文の検証）。選択肢に無いラベルは、文章が許されている
    # ため文章の答えとして通るので、ここでは id 不一致で確認する
    status, _ = _request(port, "POST", "/api/choices/answer", {
        "id": "no-such", "answer": "表",
        "tenant": "default", "user": "default"})
    assert status == 404
    status, body = _request(port, "POST", "/api/choices/answer", {
        "id": item["id"], "answer": "文章",
        "tenant": "default", "user": "default"})
    assert status == 200 and body["answered"] is True
    thread.join(timeout=10)
    status, chat_body = result["response"]
    assert status == 200
    assert chat_body["reply"] == "結果: 利用者の答え: 文章"
    # 回答後は保留が消える
    _, body = _request(port, "GET",
                       "/api/choices?tenant=default&user=default")
    assert body["choices"] == []


def test_web_choice_rejects_unknown_label_without_text(choice_server,
                                                       tmp_path):
    """allow_text でない質問の保留は ChoiceBroker 経由でしか作れないため、
    ここでは不正な本文（id なし）が 400 になることだけ確かめる。"""
    port = choice_server
    status, _ = _request(port, "POST", "/api/choices/answer",
                         {"answer": "表"})
    assert status == 400
    status, _ = _request(port, "POST", "/api/choices/answer",
                         {"id": "x", "answer": "  "})
    assert status == 400


def test_web_page_has_choice_panel_and_api(choice_server):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", choice_server, timeout=10)
    conn.request("GET", "/")
    html = conn.getresponse().read().decode("utf-8")
    conn.close()
    assert 'id="choicePanel"' in html
    assert "/api/choices" in html
