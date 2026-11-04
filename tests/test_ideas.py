"""アイデア（クマからの提案カード）のテスト。

保存と読み出し（状態の変更を含む）、生成（委任の差し替えで構造化
出力のパース、壊れた出力・「提案なし」・失敗の扱い、件数の上限）、
「やってみる」で用事が 1 つだけ登録されること、「やめておく」で
一覧から消えること、API 経由の一連の流れを確認する。生成は
ボタンで頼んだときだけ（オンデマンド）で、能動発信の巡回では
生まれないことも確かめる。
"""
import json
import threading
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from kuma_agent.config import Config
from kuma_agent.delegate import DELEGATE_NOTICE
from kuma_agent.ideas import (
    IDEAS_FILE,
    STATUS_ACCEPTED,
    STATUS_DISMISSED,
    STATUS_NEW,
    IdeaBoard,
    accept_idea,
    dismiss_idea,
    generate_ideas,
    parse_idea_proposals,
)
from kuma_agent.llm import Message
from kuma_agent.proactive import check_and_post
from kuma_agent.session import Session
from kuma_agent.tasks import TaskBoard
from kuma_agent.web import build_server, render_page

T1 = datetime(2026, 11, 4, 9, 0, 0)


# ------------------------------------------------------------ 保存と読み出し

def test_add_and_list_newest_first(tmp_path: Path):
    board = IdeaBoard(tmp_path)
    board.add("一つ目", "内容1", now=T1)
    board.add("二つ目", "内容2", reason="きっかけ2", now=T1)

    ideas = IdeaBoard(tmp_path).list()
    assert [i.title for i in ideas] == ["二つ目", "一つ目"]
    assert ideas[0].reason == "きっかけ2"
    assert ideas[0].status == STATUS_NEW
    assert ideas[0].created_at == "2026-11-04T09:00:00"
    assert ideas[1].reason == ""


def test_status_change_is_saved(tmp_path: Path):
    board = IdeaBoard(tmp_path)
    idea = board.add("提案", "内容", now=T1)
    board.set_status(idea.id, STATUS_DISMISSED)

    reloaded = IdeaBoard(tmp_path)
    assert reloaded.get(idea.id).status == STATUS_DISMISSED
    # 状態で絞れる
    assert reloaded.list(statuses=(STATUS_NEW,)) == []
    assert len(reloaded.list(statuses=(STATUS_DISMISSED,))) == 1


def test_load_skips_broken_data(tmp_path: Path):
    path = tmp_path / IDEAS_FILE
    path.write_text(json.dumps([
        {"id": "1", "title": "有効", "body": "内容", "status": "new"},
        {"id": "2", "title": "", "body": "題名が空"},
        {"id": "3", "body": "題名が無い"},
        "辞書でない",
        {"id": "4", "title": "状態が変", "body": "内容", "status": "???"},
    ], ensure_ascii=False), encoding="utf-8")
    ideas = IdeaBoard(tmp_path).list()
    assert [i.title for i in ideas] == ["状態が変", "有効"]
    # 変な状態は new として読み直す
    assert ideas[0].status == STATUS_NEW
    # ファイル自体が壊れていれば空として始める
    path.write_text("{壊れた JSON", encoding="utf-8")
    assert IdeaBoard(tmp_path).list() == []


# ------------------------------------------------------------ 操作

def test_accept_registers_exactly_one_task(tmp_path: Path):
    board = IdeaBoard(tmp_path)
    idea = board.add("散歩の習慣", "毎朝10分歩く", reason="用事が減った",
                     now=T1)

    accepted, task = accept_idea(tmp_path, idea.id)
    assert accepted.status == STATUS_ACCEPTED
    assert task is not None
    assert task.title == "散歩の習慣"
    assert "毎朝10分歩く" in task.memo
    assert "用事が減った" in task.memo
    assert accepted.task_id == task.id

    # 二度目でも用事は増えない（登録済みの用事がそのまま返る）
    again, same_task = accept_idea(tmp_path, idea.id)
    assert again.status == STATUS_ACCEPTED
    assert same_task is not None and same_task.id == task.id
    assert len(TaskBoard(tmp_path).list()) == 1
    # 保存にも残る
    assert IdeaBoard(tmp_path).get(idea.id).status == STATUS_ACCEPTED


def test_accept_unknown_and_dismissed(tmp_path: Path):
    assert accept_idea(tmp_path, "ない") is None
    board = IdeaBoard(tmp_path)
    idea = board.add("提案", "内容", now=T1)
    dismiss_idea(tmp_path, idea.id)
    # やめておいたカードは「やってみる」にできない
    assert accept_idea(tmp_path, idea.id) is None
    assert TaskBoard(tmp_path).list() == []


def test_dismiss_hides_but_keeps_record(tmp_path: Path):
    board = IdeaBoard(tmp_path)
    idea = board.add("提案", "内容", now=T1)

    dismissed = dismiss_idea(tmp_path, idea.id)
    assert dismissed is not None
    assert dismissed.status == STATUS_DISMISSED

    # 一覧（表示対象）からは消えるが、記録自体は残る
    assert IdeaBoard(tmp_path).list(
        statuses=(STATUS_NEW, STATUS_ACCEPTED)) == []
    records = json.loads((tmp_path / IDEAS_FILE).read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["status"] == "dismissed"
    # 対象が無い・用事にしたカードはやめられない
    assert dismiss_idea(tmp_path, "ない") is None
    other = board.add("別の提案", "内容", now=T1)
    accept_idea(tmp_path, other.id)
    assert dismiss_idea(tmp_path, other.id) is None


# ------------------------------------------------------------ 生成のパース

def test_parse_proposals():
    result = json.dumps({"ideas": [
        {"title": "題名", "body": "本文", "reason": "理由"},
        {"title": "題名2", "body": "本文2"},
    ]}, ensure_ascii=False)
    assert parse_idea_proposals(result) == [
        ("題名", "本文", "理由"), ("題名2", "本文2", "")]


def test_parse_proposals_tolerates_wrapping():
    # コードフェンスや前後の文章が混ざっても JSON 部分を読む
    result = "はい、考えました。\n```json\n" + json.dumps(
        {"ideas": [{"title": "題名", "body": "本文"}]},
        ensure_ascii=False) + "\n```\n以上です。"
    assert parse_idea_proposals(result) == [("題名", "本文", "")]


def test_parse_proposals_broken():
    assert parse_idea_proposals("提案はありません") is None
    assert parse_idea_proposals("") is None
    assert parse_idea_proposals('{"ideas": "並びでない"}') is None
    assert parse_idea_proposals('{"別のキー": []}') is None


def test_parse_proposals_skips_invalid_and_caps():
    result = json.dumps({"ideas": [
        {"title": "", "body": "題名が空"},
        {"body": "題名が無い"},
        {"title": "有効1", "body": "本文"},
        {"title": "有効2", "body": "本文"},
        {"title": "有効3", "body": "本文"},
        {"title": "有効4", "body": "本文"},
    ]}, ensure_ascii=False)
    proposals = parse_idea_proposals(result)
    # 形の合わない要素は読み飛ばし、上限は 3 件
    assert [p[0] for p in proposals] == ["有効1", "有効2", "有効3"]


# ------------------------------------------------------------ 生成

class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


def _session(tmp_path: Path) -> Session:
    return Session(Config(data_dir=tmp_path), _EchoModel())


def _executor_returning(result):
    def executor(parent, prompt):
        return result
    return executor


def test_generate_creates_cards(tmp_path: Path):
    TaskBoard(tmp_path).add("買い物", memo="牛乳")
    result = json.dumps({"ideas": [
        {"title": "まとめ買い", "body": "週1回にまとめる",
         "reason": "買い物が続いている"},
    ]}, ensure_ascii=False)
    prompts: list[str] = []

    def executor(parent, prompt):
        prompts.append(prompt)
        return result

    generation = generate_ideas(_session(tmp_path), tmp_path,
                                executor=executor, now=T1)
    assert generation.outcome == "created"
    assert len(generation.ideas) == 1
    assert "1 件" in generation.message
    # 材料に進行中の用事が入っている
    assert "買い物" in prompts[0]
    ideas = IdeaBoard(tmp_path).list()
    assert [i.title for i in ideas] == ["まとめ買い"]
    assert ideas[0].reason == "買い物が続いている"
    assert ideas[0].status == STATUS_NEW


def test_generate_none(tmp_path: Path):
    generation = generate_ideas(
        _session(tmp_path), tmp_path,
        executor=_executor_returning('{"ideas": []}'), now=T1)
    assert generation.outcome == "none"
    assert generation.ideas == []
    assert IdeaBoard(tmp_path).list() == []


def test_generate_failures_keep_existing_cards(tmp_path: Path):
    board = IdeaBoard(tmp_path)
    board.add("既存の提案", "内容", now=T1)
    session = _session(tmp_path)

    for bad in ("error: 子が失敗しました", "JSON でない応答", ""):
        generation = generate_ideas(
            session, tmp_path, executor=_executor_returning(bad), now=T1)
        assert generation.outcome == "failed"
        assert generation.ideas == []

    def raising(parent, prompt):
        raise RuntimeError("例外")

    generation = generate_ideas(session, tmp_path, executor=raising, now=T1)
    assert generation.outcome == "failed"
    # 既存のカードは無事なまま
    assert [i.title for i in IdeaBoard(tmp_path).list()] == ["既存の提案"]


def test_proactive_sweep_does_not_generate_ideas(tmp_path: Path):
    # 能動発信の巡回では生まない（オンデマンドだけが生成経路）
    TaskBoard(tmp_path).add("買い物")
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    check_and_post(Config(data_dir=tmp_path), now=datetime(2026, 11, 4, 10, 0),
                   session_provider=lambda: session)
    assert IdeaBoard(tmp_path).list() == []
    assert not (tmp_path / IDEAS_FILE).exists()


# ------------------------------------------------------------ Web API と画面

class _IdeaModel:
    """委任の子では提案 JSON を返し、それ以外では通常の返答をする。"""

    def chat(self, system_prompt, messages, tools):
        if DELEGATE_NOTICE in system_prompt:
            return Message("assistant", json.dumps({"ideas": [
                {"title": "朝の散歩", "body": "毎朝10分歩く",
                 "reason": "天気が良い日が続いている"},
                {"title": "買い物リストの共有", "body": "メモをまとめる",
                 "reason": ""},
            ]}, ensure_ascii=False))
        return Message("assistant", "通常の返答です")


@contextmanager
def _running(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _IdeaModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(base: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    if payload is None:
        with urllib.request.urlopen(base + path) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_ideas_over_http(tmp_path: Path):
    with _running(tmp_path) as base:
        # 最初は空
        status, data = _request(base, "/api/ideas")
        assert status == 200
        assert data["tenant"] == "default"
        assert data["user"] == "default"
        assert data["ideas"] == []

        # 「提案をもらう」で生成される
        status, data = _request(base, "/api/ideas/generate", {})
        assert status == 200
        assert data["generation"]["outcome"] == "created"
        assert data["generation"]["created"] == 2
        assert [i["title"] for i in data["ideas"]] == [
            "買い物リストの共有", "朝の散歩"]  # 新しい順（同日時は新しい方）
        target = data["ideas"][1]
        assert target["status"] == "new"
        assert target["reason"] == "天気が良い日が続いている"

        # やってみる → 用事に登録され、状態が accepted になる
        status, data = _request(
            base, "/api/ideas/act", {"id": target["id"], "action": "accept"})
        assert status == 200
        accepted = [i for i in data["ideas"] if i["id"] == target["id"]]
        assert accepted[0]["status"] == "accepted"
        tasks = TaskBoard(tmp_path).list()
        assert [t.title for t in tasks] == ["朝の散歩"]

        # 二度押しでも二重登録しない
        status, _ = _request(
            base, "/api/ideas/act", {"id": target["id"], "action": "accept"})
        assert status == 200
        assert len(TaskBoard(tmp_path).list()) == 1

        # やめておく → 一覧から消える
        other = [i for i in data["ideas"] if i["id"] != target["id"]][0]
        status, data = _request(
            base, "/api/ideas/act", {"id": other["id"], "action": "dismiss"})
        assert status == 200
        assert [i["id"] for i in data["ideas"]] == [target["id"]]

        # 不正な操作
        status, _ = _request(
            base, "/api/ideas/act", {"id": target["id"], "action": "??? "})
        assert status == 400
        status, _ = _request(
            base, "/api/ideas/act", {"id": "ない", "action": "accept"})
        assert status == 404


def test_render_page_has_ideas_ui():
    page = render_page("muse")
    assert 'id="ideasPanel"' in page
    assert 'id="ideaRows"' in page
    assert 'id="ideaGenerate"' in page
    assert "提案をもらう" in page
    assert "やってみる" in page
    assert "やめておく" in page
    assert "/api/ideas" in page
    assert "loadIdeas" in page
    # アイデアの「準備中」表示は無くなっている（目標は残る）
    assert "準備中です。これから、クマからの提案をカードで並べます" not in page
    assert 'id="goalsPanel"' in page
