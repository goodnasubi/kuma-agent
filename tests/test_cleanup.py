"""保持期限と自動クリーンアップのテスト。

保持期限（既定 90 日）を過ぎた部屋の記録ファイルと、完了してから
期限を過ぎた台帳の用事だけが掃除され、長期記憶・人格・進行中の
用事は残ること、保持期限 0（無期限）と dry-run では何も消えない
こと、全テナント×ユーザー置き場が走査されることを確認する。
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kuma_agent import cli
from kuma_agent.config import Config
from kuma_agent.memory import cleanup_expired
from kuma_agent.tasks import TaskBoard

NOW = datetime(2026, 11, 2, 12, 0, 0)
RETENTION = 90


def _days_ago(days: int) -> datetime:
    return NOW - timedelta(days=days)


def _write_session(user_dir: Path, name: str, updated: datetime) -> Path:
    """更新時刻を指定した部屋の記録ファイルを作る。"""
    conv = user_dir / "conversations"
    conv.mkdir(parents=True, exist_ok=True)
    path = conv / f"{name}.jsonl"
    path.write_text('{"role": "user", "content": "x"}\n', encoding="utf-8")
    ts = updated.timestamp()
    os.utime(path, (ts, ts))
    return path


def _write_tasks(user_dir: Path, records: list[dict]) -> Path:
    user_dir.mkdir(parents=True, exist_ok=True)
    path = user_dir / "tasks.json"
    path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return path


def _task(task_id: str, status: str, updated: datetime) -> dict:
    stamp = updated.isoformat(timespec="seconds")
    return {"id": task_id, "title": f"用事{task_id}", "status": status,
            "memo": "", "created_at": stamp, "updated_at": stamp}


# ---------------------------------------------------------------- 設定

def test_config_retention_days_default_and_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("KUMA_RETENTION_DAYS", raising=False)
    assert Config().retention_days == 90
    monkeypatch.setenv("KUMA_RETENTION_DAYS", "30")
    assert Config().retention_days == 30
    monkeypatch.setenv("KUMA_RETENTION_DAYS", "0")
    assert Config().retention_days == 0
    monkeypatch.setenv("KUMA_RETENTION_DAYS", "abc")
    assert Config().retention_days == 90  # 不正な値は既定値に倒す


# ---------------------------------------------------------------- 部屋の記録

def test_removes_only_old_sessions(tmp_path: Path):
    old = _write_session(tmp_path, "old-room", _days_ago(100))
    fresh = _write_session(tmp_path, "fresh-room", _days_ago(10))
    result = cleanup_expired(tmp_path, RETENTION, now=NOW)
    assert result.sessions_removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_keeps_memory_persona_and_other_files(tmp_path: Path):
    old_ts = _days_ago(365).timestamp()
    kept_names = ["MEMORY.md", "SOUL.md", "IDENTITY.md", "USER.md"]
    for name in kept_names:
        path = tmp_path / name
        path.write_text("残す\n", encoding="utf-8")
        os.utime(path, (old_ts, old_ts))
    # conversations 配下でも .jsonl 以外は対象外
    memo = tmp_path / "conversations" / "memo.txt"
    memo.parent.mkdir(parents=True)
    memo.write_text("残す\n", encoding="utf-8")
    os.utime(memo, (old_ts, old_ts))
    result = cleanup_expired(tmp_path, RETENTION, now=NOW)
    assert result.total == 0
    for name in kept_names:
        assert (tmp_path / name).exists()
    assert memo.exists()


# ---------------------------------------------------------------- 台帳

def test_prunes_only_old_done_tasks(tmp_path: Path):
    path = _write_tasks(tmp_path, [
        _task("1", "done", _days_ago(100)),   # 完了＋期限超過 → 除去
        _task("2", "done", _days_ago(10)),    # 完了だが新しい → 残す
        _task("3", "open", _days_ago(100)),   # 進行中は古くても残す
    ])
    result = cleanup_expired(tmp_path, RETENTION, now=NOW)
    assert result.tasks_removed == 1
    assert path.exists()  # 台帳ファイル自体は消さない
    remaining = {t.id for t in TaskBoard(tmp_path).list()}
    assert remaining == {"2", "3"}


# ---------------------------------------------------------------- 無期限と dry-run

def test_zero_retention_removes_nothing(tmp_path: Path):
    session = _write_session(tmp_path, "old-room", _days_ago(1000))
    _write_tasks(tmp_path, [_task("1", "done", _days_ago(1000))])
    result = cleanup_expired(tmp_path, 0, now=NOW)
    assert result.total == 0
    assert session.exists()
    assert len(TaskBoard(tmp_path).list()) == 1


def test_dry_run_counts_without_removing(tmp_path: Path):
    session = _write_session(tmp_path, "old-room", _days_ago(100))
    _write_tasks(tmp_path, [_task("1", "done", _days_ago(100))])
    result = cleanup_expired(tmp_path, RETENTION, now=NOW, dry_run=True)
    assert result.sessions_removed == 1
    assert result.tasks_removed == 1
    assert session.exists()  # 数えるだけで消さない
    assert len(TaskBoard(tmp_path).list()) == 1


# ---------------------------------------------------------------- 走査範囲

def test_scans_all_tenants_and_users(tmp_path: Path):
    # 既定テナント × 既定ユーザー（data_dir 直下）
    _write_session(tmp_path, "a", _days_ago(100))
    # 別テナント × 既定ユーザー
    acme = tmp_path / "tenants" / "acme"
    _write_session(acme, "b", _days_ago(100))
    # 別テナント × 別ユーザー（部屋＋台帳）
    bob = acme / "users" / "bob"
    _write_session(bob, "c", _days_ago(100))
    _write_tasks(bob, [_task("1", "done", _days_ago(100))])
    # 既定テナント × 別ユーザー（新しい部屋は残る）
    fresh = _write_session(tmp_path / "users" / "alice", "d", _days_ago(1))
    result = cleanup_expired(tmp_path, RETENTION, now=NOW)
    assert result.sessions_removed == 3
    assert result.tasks_removed == 1
    assert fresh.exists()


def test_missing_dirs_do_not_raise(tmp_path: Path):
    result = cleanup_expired(tmp_path / "nothing-here", RETENTION, now=NOW)
    assert result.total == 0
    assert cleanup_expired(tmp_path, RETENTION, now=NOW).total == 0


# ---------------------------------------------------------------- CLI

def test_cli_cleanup_and_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KUMA_RETENTION_DAYS", str(RETENTION))
    session = _write_session(tmp_path, "old-room", _days_ago(100))
    # dry-run: 件数だけ出て、ファイルは残る（API キーなしでも動く）
    assert cli.main(["cleanup", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "部屋の記録 1 件" in out
    assert session.exists()
    # 本実行: 消える
    assert cli.main(["cleanup"]) == 0
    assert "削除しました" in capsys.readouterr().out
    assert not session.exists()
