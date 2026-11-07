"""書き出し・控えの分離（歯止めの漏れ点検 L1／L2、0.87.0）のテスト。

既定置き場のときに、ユーザー単位の書き出しとテナント削除前の
控えへ他のテナント／ユーザーのデータ（backups/ の控え ZIP を
含む）が混入しないこと、MCP 設定（mcp.json）が資格情報2ファ
イルと同じく書き出し・控え・復元の対象外であること、非既定の
テナント・ユーザーの書き出しは従来どおり自分の分が全部入る
ことを確認する。
"""
import io
import json
import zipfile
from pathlib import Path

from kuma_agent.export import (
    EXPORT_INFO_NAME,
    build_export_zip,
    build_tenant_backup_zip,
)
from kuma_agent.restore import restore_user_data

_VALID_INFO = {"format": "kuma-agent export",
               "tenant": "default", "user": "default"}


def _names(body: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(body)).namelist()


def _write(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_shared_data_dir(data_dir: Path) -> None:
    """既定組・他ユーザー・他テナント・控え・設定が同居する置き場。

    監査（guardrail-leak-audit-2026-11-07.md の L1）の再現と
    同じ構成: 既定ユーザーの置き場＝データ置き場そのものに、
    他組の控え（backups/）と他テナントの実データ（tenants/）と
    別ユーザーの置き場（users/）が隣り合う。
    """
    _write(data_dir / "MEMORY.md", "既定ユーザーの記憶")
    _write(data_dir / "tasks.json", "[]")
    _write(data_dir / "mcp.json", '{"mcpServers": {}}')
    _write(data_dir / "backups" / "tenant-other-20261107-000000.zip", "z")
    _write(data_dir / "backups" / "user-other-alice-20261107-000000.zip", "z")
    _write(data_dir / "users" / "bob" / "MEMORY.md", "bob の記憶")
    _write(data_dir / "tenants" / "other" / "MEMORY.md", "他テナントの記憶")
    _write(data_dir / "tenants" / "other" / "users" / "alice" / "MEMORY.md",
           "alice の記憶")


def _craft_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(EXPORT_INFO_NAME,
                    json.dumps(_VALID_INFO, ensure_ascii=False))
        for name, text in files.items():
            zf.writestr(name, text)
    return buffer.getvalue()


# ------------------------------------------------------- L1: 書き出し

def test_default_user_export_excludes_other_scopes(tmp_path: Path):
    _seed_shared_data_dir(tmp_path)
    names = _names(build_export_zip(tmp_path, "default", "default"))
    assert "MEMORY.md" in names
    assert "tasks.json" in names
    # 他組の控え・他テナント・他ユーザーの置き場は入らない
    assert not any(n.startswith("backups/") for n in names)
    assert not any(n.startswith("tenants/") for n in names)
    assert not any(n.startswith("users/") for n in names)
    # MCP 設定も入らない（L2）
    assert "mcp.json" not in names


def test_default_tenant_backup_excludes_other_scopes(tmp_path: Path):
    _seed_shared_data_dir(tmp_path)
    names = _names(build_tenant_backup_zip(tmp_path, "default"))
    assert "MEMORY.md" in names
    # 他テナントの実データと他組の控えは入らない
    assert not any(n.startswith("backups/") for n in names)
    assert not any(n.startswith("tenants/") for n in names)
    assert "mcp.json" not in names
    # 直下の users/ は既定テナント自身のユーザーなので含まれる
    assert "users/bob/MEMORY.md" in names


def test_nondefault_user_export_contains_all_own_files(tmp_path: Path):
    _seed_shared_data_dir(tmp_path)
    # 既定テナント×非既定ユーザー: 自分の分は全部入る
    bob = _names(build_export_zip(tmp_path, "default", "bob"))
    assert bob == [EXPORT_INFO_NAME, "MEMORY.md"]
    # 他テナント×非既定ユーザーも同じ
    alice = _names(build_export_zip(tmp_path, "other", "alice"))
    assert alice == [EXPORT_INFO_NAME, "MEMORY.md"]


def test_nondefault_tenant_backup_includes_own_users(tmp_path: Path):
    _seed_shared_data_dir(tmp_path)
    _write(tmp_path / "tenants" / "other" / "mcp.json", "{}")
    names = _names(build_tenant_backup_zip(tmp_path, "other"))
    # そのテナントの全ユーザー分が入る（users/ は除外されない）
    assert "MEMORY.md" in names
    assert "users/alice/MEMORY.md" in names
    # テナント直下の MCP 設定は入らない（L2）
    assert "mcp.json" not in names


# ------------------------------------------------- L2 と資格情報の除外

def test_user_export_excludes_mcp_and_credentials(tmp_path: Path):
    tdir = tmp_path / "tenants" / "other"
    _write(tdir / "MEMORY.md", "他テナント既定ユーザーの記憶")
    _write(tdir / "mcp.json", "{}")
    _write(tdir / "brain" / "credentials.json", '{"provider": "claude"}')
    _write(tdir / "google-calendar" / "credentials.json", "{}")
    names = _names(build_export_zip(tmp_path, "other", "default"))
    assert names == [EXPORT_INFO_NAME, "MEMORY.md"]


def test_tenant_backup_excludes_credentials_everywhere(tmp_path: Path):
    tdir = tmp_path / "tenants" / "other"
    _write(tdir / "MEMORY.md")
    _write(tdir / "brain" / "credentials.json", "{}")
    _write(tdir / "google-calendar" / "credentials.json", "{}")
    _write(tdir / "users" / "alice" / "MEMORY.md")
    _write(tdir / "users" / "alice" / "google-calendar"
           / "credentials.json", "{}")
    names = _names(build_tenant_backup_zip(tmp_path, "other"))
    assert "users/alice/MEMORY.md" in names
    assert not any(n.endswith("credentials.json") for n in names)


# ------------------------------------------------------- 復元との整合

def test_restore_skips_backups_and_mcp_and_preserves_existing(
        tmp_path: Path):
    # 復元先（既定組）には既存の MCP 設定と控えがある
    _write(tmp_path / "mcp.json", "元の設定")
    _write(tmp_path / "backups" / "keep.zip", "元の控え")
    _write(tmp_path / "MEMORY.md", "古い記憶")
    body = _craft_zip({
        "MEMORY.md": "新しい記憶",
        "mcp.json": "ZIP 側の設定",
        "backups/evil.zip": "ZIP 側の控え",
        "brain/credentials.json": "ZIP 側のキー",
    })
    _write(tmp_path / "brain" / "credentials.json", "元のキー")
    result = restore_user_data(tmp_path, "default", "default", body)
    # 通常ファイルだけが戻り、残りは skipped に数えられる
    assert result.restored_files == 1
    assert result.skipped_files == 3
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == "新しい記憶"
    # MCP 設定・キー・控え置き場は既存のまま保全される
    assert (tmp_path / "mcp.json").read_text(encoding="utf-8") == "元の設定"
    assert (tmp_path / "brain" / "credentials.json").read_text(
        encoding="utf-8") == "元のキー"
    assert (tmp_path / "backups" / "keep.zip").read_text(
        encoding="utf-8") == "元の控え"
    assert not (tmp_path / "backups" / "evil.zip").exists()


def test_restore_nondefault_user_unaffected(tmp_path: Path):
    udir = tmp_path / "tenants" / "other" / "users" / "alice"
    _write(udir / "MEMORY.md", "古い記憶")
    info = {"format": "kuma-agent export", "tenant": "other", "user": "alice"}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(EXPORT_INFO_NAME, json.dumps(info, ensure_ascii=False))
        zf.writestr("MEMORY.md", "新しい記憶")
        zf.writestr("notes.txt", "メモ")
    result = restore_user_data(tmp_path, "other", "alice",
                               buffer.getvalue())
    assert result.restored_files == 2
    assert (udir / "notes.txt").read_text(encoding="utf-8") == "メモ"
