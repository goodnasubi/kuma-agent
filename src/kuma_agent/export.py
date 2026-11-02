"""ユーザーデータの書き出し（バックアップ）。

指定したテナント×ユーザーの置き場にある全データを 1 つの ZIP に
まとめる。ZIP の先頭には export-info.json（書き出し日時・テナント・
ユーザー・kuma-agent の版・このファイルが書き出しである旨）を入れ、
残りはユーザー置き場のファイルを相対パスのまま収める。置き場が
無い・空でも、export-info.json だけの ZIP ができる。

分離のための除外: 既定ユーザーの置き場はテナントの置き場そのもの
であり、その配下の users/ は他のユーザーの置き場である。同様に、
既定テナント×既定ユーザーの置き場はデータ置き場そのもので、
配下の tenants/ は他のテナントの置き場である。これらは対象
ユーザーのデータではないため、書き出しには含めない。

書き出しは読み取り専用で、サーバー側にファイルは残さない（Web は
メモリ上で組み立てて返す）。読み込み（復元）は未対応で、将来の
課題とする（DESIGN.md の「データの書き出し」参照）。
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

from .memory import (
    TENANTS_DIRNAME,
    USERS_DIRNAME,
    normalize_tenant_name,
    normalize_user_name,
    tenant_dir,
    user_dir,
)

EXPORT_INFO_NAME = "export-info.json"


def app_version() -> str:
    """書き出し情報に載せる kuma-agent の版数。取れなければ "dev"。"""
    try:
        from importlib.metadata import version

        return version("kuma-agent")
    except Exception:
        return "dev"


def export_info(tenant: str, user: str,
                now: datetime | None = None) -> dict:
    """export-info.json の中身（書き出しの出どころを示す情報）。"""
    now = now or datetime.now()
    return {
        "format": "kuma-agent export",
        "description": "このファイルは kuma-agent のユーザーデータ"
                       "書き出し（バックアップ）です",
        "exported_at": now.isoformat(timespec="seconds"),
        "tenant": normalize_tenant_name(tenant),
        "user": normalize_user_name(user),
        "kuma_agent_version": app_version(),
    }


def default_export_filename(tenant: str, user: str,
                            now: datetime | None = None) -> str:
    """書き出し ZIP の既定のファイル名。

    形は kuma-export-<テナント>-<ユーザー>-<日付>.zip。テナント・
    ユーザーは通常の正規化を通すため、ファイル名に使えない文字は
    含まれない。
    """
    now = now or datetime.now()
    return (f"kuma-export-{normalize_tenant_name(tenant)}"
            f"-{normalize_user_name(user)}-{now:%Y-%m-%d}.zip")


def _excluded_subdirs(data_dir: Path, tenant_data_dir: Path,
                      user_data_dir: Path) -> set[Path]:
    """書き出しから外す、他のテナント／ユーザーの置き場。

    既定ユーザーの置き場（＝テナントの置き場）配下の users/ は他の
    ユーザーの置き場、データ置き場そのものが置き場になる場合の
    tenants/ は他のテナントの置き場である。
    """
    excluded: set[Path] = set()
    if user_data_dir == tenant_data_dir:
        excluded.add(user_data_dir / USERS_DIRNAME)
    if user_data_dir == data_dir:
        excluded.add(user_data_dir / TENANTS_DIRNAME)
    return excluded


def iter_export_files(data_dir: Path, tenant: str,
                      user: str) -> list[Path]:
    """書き出し対象のファイルを、ユーザー置き場からの相対順で返す。

    置き場が無い場合は空。並びは決定的（ディレクトリ・ファイル
    とも名前順）にする。他のテナント／ユーザーの置き場（除外対象）
    には降りない。
    """
    tdir = tenant_dir(data_dir, tenant)
    udir = user_dir(tdir, user)
    if not udir.is_dir():
        return []
    excluded = _excluded_subdirs(data_dir, tdir, udir)
    files: list[Path] = []
    for root, dirs, names in os.walk(udir):
        root_path = Path(root)
        dirs[:] = sorted(d for d in dirs if root_path / d not in excluded)
        for name in sorted(names):
            path = root_path / name
            if path.is_file():
                files.append(path)
    return files


def build_export_zip(data_dir: Path, tenant: str, user: str,
                     now: datetime | None = None) -> bytes:
    """ユーザーデータの書き出し ZIP をメモリ上で組み立てて返す。

    先頭に export-info.json を入れ、続けてユーザー置き場のファイル
    を相対パスのまま収める。置き場直下に export-info.json という
    名前のファイルがあっても、書き出し情報の方を正として収めない。
    読み込み中に消えた・読めないファイルは飛ばす（書き出し全体は
    落とさない）。
    """
    tenant_name = normalize_tenant_name(tenant)
    user_name = normalize_user_name(user)
    udir = user_dir(tenant_dir(data_dir, tenant_name), user_name)
    info = export_info(tenant_name, user_name, now)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(EXPORT_INFO_NAME,
                    json.dumps(info, ensure_ascii=False, indent=2) + "\n")
        for path in iter_export_files(data_dir, tenant_name, user_name):
            arcname = path.relative_to(udir).as_posix()
            if arcname == EXPORT_INFO_NAME:
                continue
            try:
                zf.write(path, arcname)
            except OSError:
                continue  # 消えた・読めないファイルは飛ばす
    return buffer.getvalue()
