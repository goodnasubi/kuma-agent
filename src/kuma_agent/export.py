"""ユーザーデータの書き出し（バックアップ）。

指定したテナント×ユーザーの置き場にある全データを 1 つの ZIP に
まとめる。ZIP の先頭には export-info.json（書き出し日時・テナント・
ユーザー・kuma-agent の版・このファイルが書き出しである旨）を入れ、
残りはユーザー置き場のファイルを相対パスのまま収める。置き場が
無い・空でも、export-info.json だけの ZIP ができる。

分離のための除外: 既定ユーザーの置き場はテナントの置き場そのもの
であり、その配下の users/ は他のユーザーの置き場である。同様に、
既定テナント×既定ユーザーの置き場はデータ置き場そのもので、
配下の tenants/ は他のテナントの置き場であり、backups/ は
全テナント・全ユーザーの控え ZIP の置き場である（他組のデータ
そのもの）。これらは対象ユーザーのデータではないため、書き出し
には含めない。テナント削除前の控えでも、既定テナントについては
同じ考え方で直下の tenants/ と backups/ を除外する（直下の
users/ はそのテナント自身のユーザーの置き場なので含める。
歯止めの漏れ点検 L1、0.87.0）。

資格情報の除外: Google カレンダー連携の資格情報ファイル
（google-calendar/credentials.json）と、テナントの頭脳の
API キー（brain/credentials.json）は、データではあっても
資格情報そのものであるため、書き出し・控えのどちらにも含め
ない（パス定義は google_calendar.py と brain.py がそれぞれ
正。復元側の扱いは restore.py）。頭脳のキーはテナントの置き
場の直下にあるため、既定ユーザーの書き出し（置き場＝テナント
の置き場）にも混ざり得る点に注意する。MCP の設定ファイル
（mcp.json）も、サーバーごとの env に秘密情報を持ち得るため、
資格情報と同じ扱いにして書き出し・控えに含めない（置き場
ルートとテナント置き場の分。ファイル名の定義は mcp_client.py
が正。設定の移行は手作業になる）。

書き出しは読み取り専用で、サーバー側にファイルは残さない（Web は
メモリ上で組み立てて返す）。読み込み（復元）は restore.py が担う
（DESIGN.md の「データの書き出し」「データの復元」参照）。

テナント削除前の控え（バックアップ）もここで担う。こちらは
ユーザー単位の書き出しと違い、テナントの置き場全体（全ユーザー
分）を ZIP にし、データ置き場ルートの backups/ にファイルとして
残す（テナントの置き場を消しても控えが残るようにするため）。
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path

from .brain import BRAIN_CREDENTIALS_PATH_PARTS
from .google_calendar import CREDENTIALS_PATH_PARTS
from .mcp_client import MCP_FILENAME
from .memory import (
    TENANTS_DIRNAME,
    USERS_DIRNAME,
    list_user_names,
    normalize_tenant_name,
    normalize_user_name,
    tenant_dir,
    user_dir,
)

EXPORT_INFO_NAME = "export-info.json"

# テナント削除前の控え（バックアップ）の置き場（データ置き場ルート
# の配下）。控えはテナントの置き場の外に作るため、置き場を丸ごと
# 消しても控えは残る
BACKUPS_DIRNAME = "backups"

# MCP の設定ファイル（mcp.json）の、置き場からの相対パスの要素。
# 置き場ルートとテナントの置き場にだけ置かれる設定ファイルで、
# 資格情報と同じく書き出し・控え・復元の対象から外す
MCP_CONFIG_PATH_PARTS = (MCP_FILENAME,)


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
    tenants/ は他のテナントの置き場、backups/ は全組の控えの
    置き場（他組のデータそのもの）である。
    """
    excluded: set[Path] = set()
    if user_data_dir == tenant_data_dir:
        excluded.add(user_data_dir / USERS_DIRNAME)
    if user_data_dir == data_dir:
        excluded.add(user_data_dir / TENANTS_DIRNAME)
        excluded.add(user_data_dir / BACKUPS_DIRNAME)
    return excluded


def _tenant_backup_excluded_subdirs(data_dir: Path,
                                    tenant_data_dir: Path) -> set[Path]:
    """テナント控えから外す、他の置き場の入口（既定テナント用）。

    既定テナントの置き場はデータ置き場そのものなので、直下の
    tenants/ は他のテナントの置き場、backups/ は全組の控えの
    置き場であり、対象テナントのデータではない（ユーザー単位
    の書き出しの分離規則（_excluded_subdirs）と同じ考え方。
    L1、0.87.0）。直下の users/ はそのテナント自身のユーザー
    の置き場なので、控えには含める（ユーザー単位の書き出しで
    users/ を外すのは、対象ユーザー以外の置き場だからであり、
    対象がテナントの場合は意味が変わる）。それ以外のテナント
    では、置き場の中身はすべてそのテナントのデータなので
    除外は要らない。
    """
    if tenant_data_dir != data_dir:
        return set()
    return {
        tenant_data_dir / TENANTS_DIRNAME,
        tenant_data_dir / BACKUPS_DIRNAME,
    }


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
            if not path.is_file():
                continue
            if path.relative_to(udir).parts in (
                    CREDENTIALS_PATH_PARTS, BRAIN_CREDENTIALS_PATH_PARTS,
                    MCP_CONFIG_PATH_PARTS):
                continue  # 資格情報と MCP 設定は書き出しに含めない
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


# ---------------------------------------------------------- テナントの控え

def tenant_backup_filename(tenant: str,
                           now: datetime | None = None) -> str:
    """テナントの控え ZIP のファイル名。

    形は tenant-<テナント>-<日時>.zip（日時は
    YYYYMMDD-HHMMSS）。テナント名は通常の正規化を通すため、
    ファイル名に使えない文字は含まれない。
    """
    now = now or datetime.now()
    return (f"tenant-{normalize_tenant_name(tenant)}"
            f"-{now:%Y%m%d-%H%M%S}.zip")


def build_tenant_backup_zip(data_dir: Path, tenant: str,
                            now: datetime | None = None) -> bytes:
    """テナントの置き場全体の控え ZIP をメモリ上で組み立てて返す。

    テナント削除の前に取る控えで、ユーザー単位の書き出しと違い、
    そのテナントの全ユーザー分を置き場からの相対パスのまま収める。
    対象が既定テナントの場合は置き場＝データ置き場そのものに
    なるため、ユーザー単位の書き出しと同じ考え方で、直下の
    tenants/（他のテナント）と backups/（全組の控え）は含め
    ない（L1、0.87.0。直下の users/ はそのテナント自身の
    ユーザーの置き場なので含める。それ以外のテナントでは、
    置き場の中身はすべてそのテナントのデータなので、この
    除外は要らない）。
    資格情報ファイル（google-calendar/credentials.json と
    brain/credentials.json）は、各ユーザーの分とテナント直下の
    分を含め、控えにも含めない。テナント直下の MCP 設定
    （mcp.json）も資格情報と同じ扱いで含めない。
    先頭には export-info.json の形で、控えである旨・テナント名・
    ユーザー一覧を入れる。置き場が無い・空でも、情報ファイルだけ
    の ZIP ができる。読み込み中に消えた・読めないファイルは飛ばす。
    """
    tenant_name = normalize_tenant_name(tenant)
    tdir = tenant_dir(data_dir, tenant_name)
    now = now or datetime.now()
    info = {
        "format": "kuma-agent tenant backup",
        "description": "このファイルは kuma-agent のテナント削除前に"
                       "自動で取られた控え（バックアップ）です",
        "backed_up_at": now.isoformat(timespec="seconds"),
        "tenant": tenant_name,
        "users": list_user_names(tdir),
        "kuma_agent_version": app_version(),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(EXPORT_INFO_NAME,
                    json.dumps(info, ensure_ascii=False, indent=2) + "\n")
        if tdir.is_dir():
            excluded = _tenant_backup_excluded_subdirs(data_dir, tdir)
            for root, dirs, names in os.walk(tdir):
                root_path = Path(root)
                dirs[:] = sorted(
                    d for d in dirs if root_path / d not in excluded)
                for name in sorted(names):
                    path = root_path / name
                    if not path.is_file():
                        continue
                    relative = path.relative_to(tdir)
                    arcname = relative.as_posix()
                    if arcname == EXPORT_INFO_NAME:
                        continue
                    if relative.parts == MCP_CONFIG_PATH_PARTS:
                        continue  # MCP 設定は控えに含めない
                    if relative.parts[-2:] in (
                            CREDENTIALS_PATH_PARTS,
                            BRAIN_CREDENTIALS_PATH_PARTS):
                        continue  # 資格情報は控えに含めない
                    try:
                        zf.write(path, arcname)
                    except OSError:
                        continue  # 消えた・読めないファイルは飛ばす
    return buffer.getvalue()


def save_tenant_backup(data_dir: Path, tenant: str,
                       now: datetime | None = None) -> Path:
    """テナントの控え ZIP を backups/ に保存し、そのパスを返す。

    控えの置き場はデータ置き場ルートの backups/（テナントの置き場
    の外）で、テナントの置き場を消しても控えは残る。保存に失敗
    したら OSError をそのまま伝える（呼び出し側は、控えが作れ
    なければ削除に進まない判断に使う）。
    """
    now = now or datetime.now()
    body = build_tenant_backup_zip(data_dir, tenant, now)
    backups_dir = data_dir / BACKUPS_DIRNAME
    backups_dir.mkdir(parents=True, exist_ok=True)
    path = backups_dir / tenant_backup_filename(tenant, now)
    path.write_bytes(body)
    return path
