"""保持期限と自動クリーンアップのテスト。

保持期限（既定 90 日）を過ぎた部屋の記録ファイルと、完了してから
期限を過ぎた用事だけが掃除され、長期記憶・人格・進行中の
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
from kuma_agent.memory import (cleanup_expired, load_retention_overrides,
                               retention_days_for)
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


# ---------------------------------------------------------------- 用事の一覧

def test_prunes_only_old_done_tasks(tmp_path: Path):
    path = _write_tasks(tmp_path, [
        _task("1", "done", _days_ago(100)),   # 完了＋期限超過 → 除去
        _task("2", "done", _days_ago(10)),    # 完了だが新しい → 残す
        _task("3", "open", _days_ago(100)),   # 進行中は古くても残す
    ])
    result = cleanup_expired(tmp_path, RETENTION, now=NOW)
    assert result.tasks_removed == 1
    assert path.exists()  # 用事ファイル自体は消さない
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
    # 別テナント × 別ユーザー（部屋＋用事）
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


# ---------------------------------------------------------------- テナント単位

def test_cleanup_scoped_to_one_tenant(tmp_path: Path):
    # 対象テナント（部屋＋別ユーザーの用事）と他テナントに期限切れを用意
    acme = tmp_path / "tenants" / "acme"
    acme_old = _write_session(acme, "acme-old", _days_ago(100))
    acme_fresh = _write_session(acme, "acme-fresh", _days_ago(10))
    bob = acme / "users" / "bob"
    _write_tasks(bob, [_task("1", "done", _days_ago(100))])
    other = tmp_path / "tenants" / "other"
    other_old = _write_session(other, "other-old", _days_ago(100))
    _write_tasks(other, [_task("1", "done", _days_ago(100))])
    default_old = _write_session(tmp_path, "default-old", _days_ago(100))
    result = cleanup_expired(tmp_path, RETENTION, now=NOW, tenant="acme")
    # acme 配下だけが掃除され、他テナントと既定テナントは無傷
    assert result.sessions_removed == 1
    assert result.tasks_removed == 1
    assert not acme_old.exists()
    assert acme_fresh.exists()
    assert TaskBoard(bob).list() == []
    assert other_old.exists()
    assert len(TaskBoard(other).list()) == 1
    assert default_old.exists()


def test_cleanup_unknown_tenant_removes_nothing(tmp_path: Path):
    session = _write_session(tmp_path, "old-room", _days_ago(100))
    result = cleanup_expired(tmp_path, RETENTION, now=NOW, tenant="no-such")
    assert result.total == 0  # 例外にせず 0 件
    assert session.exists()


def test_cleanup_default_tenant_scope(tmp_path: Path):
    own = _write_session(tmp_path, "own-old", _days_ago(100))
    other = _write_session(tmp_path / "tenants" / "acme", "acme-old",
                           _days_ago(100))
    result = cleanup_expired(tmp_path, RETENTION, now=NOW, tenant="default")
    assert result.sessions_removed == 1
    assert not own.exists()
    assert other.exists()


# ---------------------------------------------------------------- テナントごとの保持期間

def _write_retention(data_dir: Path, content: object) -> Path:
    path = data_dir / "retention.json"
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, ensure_ascii=False),
                        encoding="utf-8")
    return path


def test_load_retention_overrides(tmp_path: Path):
    _write_retention(tmp_path, {"tenants": {"acme": 30, "default": 0}})
    assert load_retention_overrides(tmp_path) == {"acme": 30, "default": 0}
    # テナント名は正規化を通して照合される
    _write_retention(tmp_path, {"tenants": {"acme corp": 45}})
    assert load_retention_overrides(tmp_path) == {"acme-corp": 45}


def test_load_retention_overrides_ignores_bad_values(tmp_path: Path):
    _write_retention(tmp_path, {"tenants": {
        "ok": 30,
        "negative": -5,
        "text": "30",
        "float": 30.5,
        "bool": True,
        "null": None,
    }})
    assert load_retention_overrides(tmp_path) == {"ok": 30}


def test_load_retention_overrides_missing_or_broken(tmp_path: Path):
    assert load_retention_overrides(tmp_path) == {}  # ファイル無し
    _write_retention(tmp_path, "{壊れた JSON")
    assert load_retention_overrides(tmp_path) == {}
    _write_retention(tmp_path, {"tenants": ["acme"]})  # 構造が違う
    assert load_retention_overrides(tmp_path) == {}
    _write_retention(tmp_path, [1, 2, 3])
    assert load_retention_overrides(tmp_path) == {}


def test_retention_days_for(tmp_path: Path):
    _write_retention(tmp_path, {"tenants": {"acme": 30}})
    assert retention_days_for(tmp_path, "acme", RETENTION) == 30
    assert retention_days_for(tmp_path, "other", RETENTION) == RETENTION
    assert retention_days_for(tmp_path, None, RETENTION) == RETENTION


def test_cleanup_uses_per_tenant_retention(tmp_path: Path):
    _write_retention(tmp_path, {"tenants": {"acme": 30}})
    acme = tmp_path / "tenants" / "acme"
    acme_old = _write_session(acme, "acme-50d", _days_ago(50))   # 30 日で消える
    acme_fresh = _write_session(acme, "acme-20d", _days_ago(20))  # 残る
    _write_tasks(acme, [_task("1", "done", _days_ago(50)),
                        _task("2", "done", _days_ago(20))])
    own_mid = _write_session(tmp_path, "own-50d", _days_ago(50))  # 既定 90 日で残る
    own_old = _write_session(tmp_path, "own-100d", _days_ago(100))  # 消える
    _write_tasks(tmp_path, [_task("1", "done", _days_ago(50))])
    result = cleanup_expired(tmp_path, RETENTION, now=NOW)
    assert result.sessions_removed == 2
    assert result.tasks_removed == 1
    assert not acme_old.exists()
    assert acme_fresh.exists()
    assert own_mid.exists()
    assert not own_old.exists()
    assert {t.id for t in TaskBoard(acme).list()} == {"2"}
    assert len(TaskBoard(tmp_path).list()) == 1


def test_cleanup_per_tenant_scoped_run_also_resolves(tmp_path: Path):
    # tenant 指定の実行でも、そのテナントの retention.json が効く
    _write_retention(tmp_path, {"tenants": {"acme": 30}})
    target = _write_session(tmp_path / "tenants" / "acme", "acme-50d",
                            _days_ago(50))
    result = cleanup_expired(tmp_path, RETENTION, now=NOW, tenant="acme")
    assert result.sessions_removed == 1
    assert not target.exists()


def test_cleanup_tenant_override_zero_is_unlimited(tmp_path: Path):
    _write_retention(tmp_path, {"tenants": {"acme": 0}})
    acme_old = _write_session(tmp_path / "tenants" / "acme", "acme-ancient",
                              _days_ago(1000))
    own_old = _write_session(tmp_path, "own-100d", _days_ago(100))
    result = cleanup_expired(tmp_path, RETENTION, now=NOW)
    assert result.sessions_removed == 1  # 既定テナント分だけ
    assert acme_old.exists()  # acme は無期限なので残る
    assert not own_old.exists()


def test_cleanup_broken_retention_json_falls_back(tmp_path: Path):
    _write_retention(tmp_path, "{壊れた JSON")
    old = _write_session(tmp_path, "old-room", _days_ago(100))
    fresh = _write_session(tmp_path, "fresh-room", _days_ago(50))
    result = cleanup_expired(tmp_path, RETENTION, now=NOW)
    assert result.sessions_removed == 1  # 例外にならず全体既定で判定
    assert not old.exists()
    assert fresh.exists()


def test_override_days_beats_retention_json(tmp_path: Path):
    _write_retention(tmp_path, {"tenants": {"acme": 0}})  # acme は無期限のはず
    target = _write_session(tmp_path / "tenants" / "acme", "acme-50d",
                            _days_ago(50))
    result = cleanup_expired(tmp_path, RETENTION, now=NOW, override_days=10)
    assert result.sessions_removed == 1  # --days 相当の上書きが優先される
    assert not target.exists()


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


def test_cli_cleanup_tenant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                            capsys: pytest.CaptureFixture):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KUMA_RETENTION_DAYS", str(RETENTION))
    acme = _write_session(tmp_path / "tenants" / "acme", "acme-old",
                          _days_ago(100))
    other = _write_session(tmp_path / "tenants" / "other", "other-old",
                           _days_ago(100))
    # --tenant と --dry-run の併用: 対象テナント名が出て、どちらも残る
    assert cli.main(["cleanup", "--tenant", "acme", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "対象テナント: acme" in out
    assert "部屋の記録 1 件" in out
    assert acme.exists()
    assert other.exists()
    # 本実行: acme だけ消え、other は残る
    assert cli.main(["cleanup", "--tenant", "acme"]) == 0
    out = capsys.readouterr().out
    assert "対象テナント: acme" in out
    assert not acme.exists()
    assert other.exists()
    # 存在しないテナント: 例外にならず 0 件
    assert cli.main(["cleanup", "--tenant", "no-such"]) == 0
    assert "部屋の記録 0 件" in capsys.readouterr().out


def test_cli_cleanup_days_overrides_retention_json(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KUMA_RETENTION_DAYS", str(RETENTION))
    _write_retention(tmp_path, {"tenants": {"acme": 180}})
    target = _write_session(tmp_path / "tenants" / "acme", "acme-100d",
                            _days_ago(100))
    # テナント指定だけでは retention.json の 180 日が表示され、残る
    assert cli.main(["cleanup", "--tenant", "acme"]) == 0
    out = capsys.readouterr().out
    assert "保持期限: 180 日" in out
    assert target.exists()
    # --days は retention.json より優先され、その場限りで適用される
    assert cli.main(["cleanup", "--tenant", "acme", "--days", "30"]) == 0
    out = capsys.readouterr().out
    assert "保持期限: 30 日" in out
    assert not target.exists()


def test_cli_cleanup_days_zero_is_unlimited(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture):
    monkeypatch.setenv("KUMA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KUMA_RETENTION_DAYS", str(RETENTION))
    session = _write_session(tmp_path, "old-room", _days_ago(100))
    assert cli.main(["cleanup", "--days", "0"]) == 0
    out = capsys.readouterr().out
    assert "無期限" in out
    assert session.exists()
