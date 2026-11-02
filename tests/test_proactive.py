"""能動発信（proactive）のテスト。

進行中の用事があるときだけ 1 日 1 回 main の会話に投稿すること、
テナント設定が OFF のとき・用事が無いとき・スレッドモードでは
投稿しないこと、7 日以上手つかずの用事が同じ発言に含まれることを
確認する。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.memory import Memory
from kuma_agent.proactive import check_and_post, run_sweep
from kuma_agent.session import Session
from kuma_agent.settings import set_proactive_enabled

NOW = datetime(2026, 11, 2, 9, 0, 0)


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


def _write_tasks(user_dir: Path, tasks: list[dict]) -> Path:
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / "tasks.json"
    path.write_text(json.dumps(tasks, ensure_ascii=False), encoding="utf-8")
    return path


def _task(task_id: str, title: str, created: datetime,
          status: str = "open") -> dict:
    stamp = created.isoformat(timespec="seconds")
    return {"id": task_id, "title": title, "status": status, "memo": "",
            "created_at": stamp, "updated_at": stamp}


def _records(user_dir: Path) -> list[dict]:
    path = user_dir / "conversations" / "main.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- 条件

def test_no_open_tasks_no_post(tmp_path: Path):
    cfg = Config(data_dir=tmp_path)
    assert check_and_post(cfg, now=NOW) is None
    assert _records(tmp_path) == []
    assert not (tmp_path / "proactive_state.json").exists()

    # 完了した用事だけでも投稿しない
    _write_tasks(tmp_path, [_task("1", "終わった用事", NOW, status="done")])
    assert check_and_post(cfg, now=NOW) is None
    assert _records(tmp_path) == []


def test_posts_open_tasks_once_a_day(tmp_path: Path):
    _write_tasks(tmp_path, [
        _task("1", "買い物", NOW),
        _task("2", "申込書を出す", NOW - timedelta(days=1)),
    ])
    cfg = Config(data_dir=tmp_path)
    text = check_and_post(cfg, now=NOW)
    assert text is not None
    assert "買い物" in text and "申込書を出す" in text
    assert "2 件" in text

    records = _records(tmp_path)
    assert len(records) == 1
    assert records[0]["role"] == "assistant"
    assert records[0]["content"] == text
    assert records[0]["proactive"] is True

    state = json.loads(
        (tmp_path / "proactive_state.json").read_text(encoding="utf-8"))
    assert state == {"last_posted_on": "2026-11-02"}

    # 同じ日にもう一度チェックしても投稿しない
    assert check_and_post(cfg, now=NOW + timedelta(hours=3)) is None
    assert len(_records(tmp_path)) == 1

    # 翌日になればまた投稿する
    text2 = check_and_post(cfg, now=NOW + timedelta(days=1))
    assert text2 is not None
    assert len(_records(tmp_path)) == 2


def test_posted_message_restores_as_assistant_turn(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = Config(data_dir=tmp_path)
    text = check_and_post(cfg, now=NOW)
    # 印（proactive: true）があっても復元は通常の assistant 発話
    session = Session(cfg, _EchoModel())
    assert [m.content for m in session.history] == [text]
    assert session.history[0].role == "assistant"


def test_tenant_disabled_no_post(tmp_path: Path):
    _write_tasks(tmp_path / "tenants" / "acme",
                 [_task("1", "acmeの用事", NOW)])
    set_proactive_enabled(tmp_path, "acme", False)
    cfg = Config(data_dir=tmp_path, tenant="acme")
    assert check_and_post(cfg, now=NOW) is None
    assert _records(tmp_path / "tenants" / "acme") == []

    # 既定テナント（未設定＝ON）は投稿する
    _write_tasks(tmp_path, [_task("1", "既定の用事", NOW)])
    assert check_and_post(Config(data_dir=tmp_path), now=NOW) is not None


def test_wall_mode_no_post(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = Config(data_dir=tmp_path, mode="wall")
    assert check_and_post(cfg, now=NOW) is None
    assert _records(tmp_path) == []


def test_stale_task_is_called_out_in_same_message(tmp_path: Path):
    _write_tasks(tmp_path, [
        _task("1", "古い用事", NOW - timedelta(days=8)),
        _task("2", "新しい用事", NOW),
    ])
    text = check_and_post(Config(data_dir=tmp_path), now=NOW)
    assert text is not None
    assert "古い用事" in text and "新しい用事" in text
    # 7 日以上の注意喚起は同じ発言の中に含まれる（別発言は増えない）
    assert "7 日以上そのまま" in text
    assert len(_records(tmp_path)) == 1
    stale_line = [line for line in text.splitlines() if "7 日以上" in line]
    assert "古い用事" in stale_line[0]
    assert "新しい用事" not in stale_line[0]


def test_message_lists_at_most_five_titles(tmp_path: Path):
    tasks = [_task(str(i), f"用事{i}", NOW) for i in range(1, 8)]
    _write_tasks(tmp_path, tasks)
    text = check_and_post(Config(data_dir=tmp_path), now=NOW)
    assert text is not None
    for i in range(1, 6):
        assert f"用事{i}" in text
    assert "用事6" not in text and "用事7" not in text
    assert "ほか 2 件" in text


def test_uses_given_session_when_provided(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    text = check_and_post(cfg, now=NOW, session_provider=lambda: session)
    assert text is not None
    # 保持中の Session の履歴にも反映され、記録は 1 回だけ書かれる
    assert session.history[-1].role == "assistant"
    assert session.history[-1].content == text
    assert len(_records(tmp_path)) == 1


# ---------------------------------------------------------------- 巡回

def test_run_sweep_covers_tenants_and_users(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "既定の用事", NOW)])
    _write_tasks(tmp_path / "tenants" / "acme",
                 [_task("1", "acme既定ユーザーの用事", NOW)])
    _write_tasks(tmp_path / "tenants" / "acme" / "users" / "bob",
                 [_task("1", "bobの用事", NOW)])
    cfg = Config(data_dir=tmp_path)
    posted = run_sweep(cfg, now=NOW)
    assert {(t, u) for t, u, _text in posted} == {
        ("default", "default"), ("acme", "default"), ("acme", "bob")}
    # 2 回目の巡回では誰にも投稿しない（1 日 1 回）
    assert run_sweep(cfg, now=NOW) == []


def test_run_sweep_scoped_by_tenant_and_user(tmp_path: Path):
    _write_tasks(tmp_path, [_task("1", "既定の用事", NOW)])
    _write_tasks(tmp_path / "tenants" / "acme" / "users" / "bob",
                 [_task("1", "bobの用事", NOW)])
    cfg = Config(data_dir=tmp_path)
    posted = run_sweep(cfg, tenant="acme", user="bob", now=NOW)
    assert [(t, u) for t, u, _text in posted] == [("acme", "bob")]
    assert _records(tmp_path) == []  # 既定テナントには投稿されていない


# ---------------------------------------------------------------- CLI

def test_cli_proactive(monkeypatch, tmp_path: Path, capsys):
    _write_tasks(tmp_path, [_task("1", "買い物", NOW)])
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KUMA_TENANT", raising=False)
    monkeypatch.delenv("KUMA_USER", raising=False)
    monkeypatch.delenv("KUMA_MODE", raising=False)
    # API キーが無くても動く経路であること
    monkeypatch.delenv("META_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)

    assert cli.main(["proactive"]) == 0
    out = capsys.readouterr().out
    assert "投稿しました" in out and "買い物" in out
    assert len(_records(tmp_path)) == 1

    # 2 回目は投稿しない
    assert cli.main(["proactive"]) == 0
    out = capsys.readouterr().out
    assert "投稿する内容はありませんでした" in out
    assert len(_records(tmp_path)) == 1
    # 記録が Memory 経由で読める（形式が壊れていない）
    assert Memory(tmp_path).load_turns()[0]["content"] != ""
