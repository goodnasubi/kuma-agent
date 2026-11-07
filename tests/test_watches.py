"""見張り（条件の変化を知らせる仕組み）のテスト。

部品の検証、保存と読み出し（削除・ON/OFF・名前での指定）、
期限判定、対象の確認（ファイル・ページ・検索。取得は差し替え）、
巡回での検出（最初の確認は基準だけ、変化で投稿、失敗は記録
して次回へ）、道具の登録と実行、check_and_post への組み込みを
確認する。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import kuma_agent.watches as watches_mod
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.proactive import _UnusedModel, check_and_post, run_due_watches
from kuma_agent.session import Session
from kuma_agent.tools import activity_label
from kuma_agent.watches import (
    WatchBoard,
    check_watch,
    is_due,
    validate_watch,
)

NOW = datetime(2026, 11, 7, 12, 0, 0)


def _pin_created_at(data_dir: Path, when: datetime) -> None:
    """保存済みの見張りの作成時刻を固定する。

    WatchBoard.add は実時計で作成時刻を刻むため、そのままだと
    固定した NOW との期限判定（作成時刻＋間隔）が実時計の進み
    方に依存してしまう。種を作った直後に作成時刻を NOW 基準
    へ寄せ、判定を実時計から切り離す（定期ジョブのテストが
    jobs.json に作成時刻を明示して用意する流儀と同じ）。
    """
    path = data_dir / "watches.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    for record in records:
        record["created_at"] = when.isoformat(timespec="seconds")
    path.write_text(json.dumps(records, ensure_ascii=False),
                    encoding="utf-8")


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


class _FakeSession:
    """run_due_watches が使う範囲だけを持つ Session の代わり。"""

    def __init__(self, model=None):
        self.model = model if model is not None else object()
        self.posted: list[str] = []

    def post_proactive_message(self, text: str) -> None:
        self.posted.append(text)


# ------------------------------------------------------------ 検証

def test_validate_watch():
    assert validate_watch("web", "https://example.com", None) == \
        ("web", "https://example.com", 1)
    assert validate_watch(" search ", " 猫 ", 3) == ("search", "猫", 3)
    assert validate_watch("file", "a/b.txt", 2.0) == ("file", "a/b.txt", 2)
    for kind, target, hours in [
            ("page", "https://example.com", None),   # 不正な種別
            ("web", "example.com", None),            # URL でない
            ("search", " ", None),                   # 空の対象
            ("file", "a.txt", 0),                    # 間隔が 0
            ("file", "a.txt", "x"),                  # 間隔が数でない
    ]:
        with pytest.raises(ValueError):
            validate_watch(kind, target, hours)


# ------------------------------------------------------------ 保存と操作

def test_board_add_list_and_reload(tmp_path: Path):
    board = WatchBoard(tmp_path)
    board.add("価格", "web", "https://example.com", interval_hours=2,
              note="下がったら")
    loaded = WatchBoard(tmp_path).list()
    assert len(loaded) == 1
    w = loaded[0]
    assert (w.title, w.kind, w.target, w.interval_hours, w.note) == \
        ("価格", "web", "https://example.com", 2, "下がったら")
    assert w.enabled and w.last_hash == "" and w.last_checked_at == ""


def test_board_broken_file_and_records(tmp_path: Path):
    path = tmp_path / "watches.json"
    path.write_text("{壊れた", encoding="utf-8")
    assert WatchBoard(tmp_path).list() == []
    path.write_text(json.dumps([
        {"id": "1", "title": "有効", "kind": "search", "target": "猫"},
        {"id": "2", "title": "種別が不正", "kind": "page", "target": "x"},
        {"title": "ID なし", "kind": "search", "target": "犬"},
    ]), encoding="utf-8")
    assert [w.title for w in WatchBoard(tmp_path).list()] == ["有効"]


def test_board_find_delete_and_enabled(tmp_path: Path):
    board = WatchBoard(tmp_path)
    board.add("天気ページ", "web", "https://example.com")
    board.add("天気検索", "search", "天気")
    assert board.find("1").title == "天気ページ"
    assert board.find("天気検索").id == "2"
    assert board.find("天気").title == "天気ページ"  # 前方一致
    assert board.find("なし") is None

    updated = board.set_enabled("天気検索", False)
    assert updated is not None and not updated.enabled
    assert board.set_enabled("なし", True) is None

    removed = board.delete("天気ページ")
    assert removed is not None and removed.title == "天気ページ"
    assert [w.title for w in WatchBoard(tmp_path).list()] == ["天気検索"]
    assert board.delete("天気ページ") is None


def test_is_due(tmp_path: Path):
    board = WatchBoard(tmp_path)
    watch = board.add("対象", "search", "猫", interval_hours=2)
    created = datetime.fromisoformat(watch.created_at)
    # 未確認の間は作成時刻＋間隔で判定する
    assert not is_due(watch, created + timedelta(hours=1))
    assert is_due(watch, created + timedelta(hours=2))
    board.mark_checked(watch.id, NOW, hash="h", summary="s")
    checked = WatchBoard(tmp_path).list()[0]
    assert not is_due(checked, NOW + timedelta(hours=1))
    assert is_due(checked, NOW + timedelta(hours=2))
    board.set_enabled(watch.id, False)
    assert not is_due(WatchBoard(tmp_path).list()[0],
                      NOW + timedelta(days=30))


# ------------------------------------------------------------ 対象の確認

def test_check_file_states(tmp_path: Path):
    board = WatchBoard(tmp_path)
    watch = board.add("ファイル", "file", "sub/note.txt")
    # 無い状態も 1 つの状態
    h1, s1 = check_watch(watch, tmp_path)
    assert "ありません" in s1
    target = tmp_path / "sub" / "note.txt"
    target.parent.mkdir(parents=True)
    target.write_text("中身", encoding="utf-8")
    h2, s2 = check_watch(watch, tmp_path)
    assert h2 != h1 and "バイト" in s2
    target.write_text("中身", encoding="utf-8")
    h3, _ = check_watch(watch, tmp_path)
    assert h3 == h2  # 同じ内容なら同じハッシュ
    target.write_text("別の内容", encoding="utf-8")
    h4, _ = check_watch(watch, tmp_path)
    assert h4 != h2


def test_check_file_rejects_escape(tmp_path: Path):
    board = WatchBoard(tmp_path)
    watch = board.add("外", "file", "../secret.txt")
    with pytest.raises(ValueError):
        check_watch(watch, tmp_path)


def test_check_web_and_search(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(watches_mod, "fetch_page",
                        lambda url: "ページの本文です")
    monkeypatch.setattr(watches_mod, "web_search",
                        lambda q, limit=5: "検索結果の一覧")
    board = WatchBoard(tmp_path)
    web = board.add("ページ", "web", "https://example.com")
    h, s = check_watch(web, tmp_path)
    assert h and "ページの本文" in s
    search = board.add("検索", "search", "猫")
    h2, s2 = check_watch(search, tmp_path)
    assert h2 != h and "検索結果" in s2

    monkeypatch.setattr(watches_mod, "fetch_page",
                        lambda url: "error: 取得できません")
    with pytest.raises(ValueError):
        check_watch(web, tmp_path)


# ------------------------------------------------------------ 巡回での検出

def _seed_watch(tmp_path: Path, monkeypatch, text: str) -> WatchBoard:
    """本文が差し替えられる web の見張りを用意し、基準を記録する。"""
    state = {"text": text}
    monkeypatch.setattr(watches_mod, "fetch_page", lambda url: state["text"])
    board = WatchBoard(tmp_path)
    board.add("ページ", "web", "https://example.com", interval_hours=1)
    # NOW の時点で最初の確認が来ているよう、作成時刻を固定する
    _pin_created_at(tmp_path, NOW - timedelta(hours=2))
    return board


def test_first_check_is_baseline_only(tmp_path: Path, monkeypatch):
    _seed_watch(tmp_path, monkeypatch, "初回の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    posted = run_due_watches(cfg, NOW, lambda: session,
                             watch_executor=lambda s, p: "知らせ文")
    assert posted == [] and session.posted == []
    watch = WatchBoard(tmp_path).list()[0]
    assert watch.last_hash and watch.last_checked_at != ""
    assert watch.last_changed_at == ""


def test_change_posts_notice(tmp_path: Path, monkeypatch):
    board = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    run_due_watches(cfg, NOW, lambda: session,
                    watch_executor=lambda s, p: "知らせ文")
    # 本文を変えて、間隔を過ぎてから確認する
    watches_mod.fetch_page = lambda url: "新しい本文です"  # 差し替え済み関数を上書き
    prompts: list[str] = []

    def executor(sess, prompt):
        prompts.append(prompt)
        return "値段が下がりました"

    later = NOW + timedelta(hours=2)
    posted = run_due_watches(cfg, later, lambda: session,
                             watch_executor=executor)
    assert len(posted) == 1
    assert posted[0].startswith("見張り「ページ」で変化がありました。")
    assert "値段が下がりました" in posted[0]
    assert session.posted == posted
    # 子への指示には前回と今回の要約が入る
    assert "前の本文" in prompts[0] and "新しい本文です" in prompts[0]
    watch = WatchBoard(tmp_path).list()[0]
    assert watch.last_changed_at == later.isoformat(timespec="seconds")
    # 同じ内容ではもう知らせない
    assert run_due_watches(cfg, later + timedelta(hours=2),
                           lambda: session, watch_executor=executor) == []


def test_check_failure_records_error(tmp_path: Path, monkeypatch):
    def broken(url):
        raise RuntimeError("つながりません")
    monkeypatch.setattr(watches_mod, "fetch_page", broken)
    board = WatchBoard(tmp_path)
    board.add("ページ", "web", "https://example.com")
    _pin_created_at(tmp_path, NOW - timedelta(hours=2))
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    assert run_due_watches(cfg, NOW, lambda: session) == []
    watch = WatchBoard(tmp_path).list()[0]
    assert "つながりません" in watch.last_error
    assert watch.last_hash == ""  # 基準は記録されない


def test_change_waits_for_real_model(tmp_path: Path, monkeypatch):
    board = _seed_watch(tmp_path, monkeypatch, "前の本文")
    cfg = Config(data_dir=tmp_path)
    session = _FakeSession()
    run_due_watches(cfg, NOW, lambda: session,
                    watch_executor=lambda s, p: "知らせ文")
    monkeypatch.setattr(watches_mod, "fetch_page",
                        lambda url: "変わった本文")
    unused = _FakeSession(model=_UnusedModel())
    later = NOW + timedelta(hours=2)
    assert run_due_watches(cfg, later, lambda: unused,
                           watch_executor=lambda s, p: "知らせ文") == []
    # 変化は消費されていない（基準のまま）
    watch = WatchBoard(tmp_path).list()[0]
    assert watch.last_changed_at == ""
    # 実モデルの経路では知らせる
    real = _FakeSession()
    posted = run_due_watches(cfg, later, lambda: real,
                             watch_executor=lambda s, p: "知らせ文")
    assert len(posted) == 1


def test_check_and_post_includes_watches(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.txt").write_text("一つ目", encoding="utf-8")
    cfg = Config(data_dir=tmp_path)
    board = WatchBoard(tmp_path)
    board.add("ファイル", "file", "note.txt", interval_hours=1)
    _pin_created_at(tmp_path, NOW - timedelta(hours=2))
    session = Session(cfg, _EchoModel())
    # 最初の巡回は基準の記録だけ
    assert check_and_post(cfg, now=NOW, session_provider=lambda: session,
                          watch_executor=lambda s, p: "知らせ文") is None
    (tmp_path / "note.txt").write_text("二つ目", encoding="utf-8")
    text = check_and_post(cfg, now=NOW + timedelta(hours=2),
                          session_provider=lambda: session,
                          watch_executor=lambda s, p: "中身が変わりました")
    assert text is not None
    assert "見張り「ファイル」で変化がありました。" in text
    assert "中身が変わりました" in text


# ------------------------------------------------------------ 道具

def test_tools_in_both_modes(tmp_path: Path):
    for mode, name in (("ledger", "main"), ("wall", "work")):
        session = Session(Config(data_dir=tmp_path / mode, mode=mode),
                          _EchoModel(), name)
        names = {s["name"] for s in session.tools.schemas()}
        assert {"create_watch", "list_watches", "delete_watch",
                "set_watch_enabled"} <= names


def test_tools_roundtrip(tmp_path: Path):
    session = Session(Config(data_dir=tmp_path), _EchoModel())
    result = session.tools.call("create_watch", {
        "title": "価格", "kind": "web", "target": "https://example.com",
        "interval_hours": 3, "note": "下がったら金額も"})
    assert result.startswith("登録しました: #1 価格")
    assert session.tools.call("create_watch", {
        "title": "x", "kind": "web", "target": "not-a-url"}).startswith("error:")

    listing = session.tools.call("list_watches", {})
    assert "価格" in listing and "3 時間ごと" in listing

    assert "更新しました" in session.tools.call(
        "set_watch_enabled", {"watch": "価格", "enabled": False})
    assert session.tools.call(
        "set_watch_enabled", {"watch": "なし", "enabled": True}
    ).startswith("error:")
    assert "削除しました" in session.tools.call(
        "delete_watch", {"watch": "価格"})
    assert session.tools.call("list_watches", {}) == "（見張りはありません）"


def test_activity_labels():
    assert activity_label("create_watch") == "見張りを記録中"
    assert activity_label("list_watches") == "見張りを確認中"
