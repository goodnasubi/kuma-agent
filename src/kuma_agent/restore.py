"""ユーザーデータの復元（書き出し ZIP の読み込み）。

書き出し（export.py）が作った ZIP を読み、指定したテナント×
ユーザーの置き場をその内容で置き換える。ZIP の先頭にある
export-info.json で、kuma-agent の書き出しであることを確かめて
から進める（テナント削除前の控え ZIP は形式が違うため、復元元
には使えない）。

置き換えの前に、対象の置き場にデータがあれば現在の内容の控え
（ユーザー単位の書き出し ZIP）をデータ置き場ルートの backups/
へ自動で取る。控えが作れなければ復元は中止する。

分離のための除外は書き出しの鏡像にする。既定ユーザーの置き場
（＝テナントの置き場）配下の users/ は他のユーザーの置き場、
データ置き場そのものが置き場になる場合の tenants/ は他の
テナントの置き場であり、復元でも書き込まず、置き換えの際も
消さない。データ置き場そのものが対象の場合は、サーバー自身の
控え置き場である backups/ も消さない（取ったばかりの控えを
置き換えで消してしまわないため）。

資格情報の扱いも書き出しとそろえる。Google カレンダー連携の
資格情報ファイル（google-calendar/credentials.json）と、
テナントの頭脳の API キー（brain/credentials.json）は、ZIP
に含まれていても書き込まず（注入・上書きの防止）、置き換えの
際も既存のファイルを消さずに保全する（パス定義は
google_calendar.py と brain.py がそれぞれ正）。頭脳のキーは
テナントの置き場の直下にあるため、既定ユーザーの復元（置き場
＝テナントの置き場）で同名の項目が現れ得る。MCP の設定
ファイル（mcp.json）も、env に秘密情報を持ち得るため資格
情報と同じ扱いにし、書き戻さず既存の設定を保全する
（0.87.0）。また、データ置き場そのものが対象の場合、書き
出しの分離規則（export._excluded_subdirs）に backups/ が
含まれるため、ZIP 内の backups/ 配下の項目も書き戻さない
（控え置き場は復元の対象外）。

安全のため、ZIP の項目は書き出す前にすべて検証する。絶対パス
や `..` を含む項目、シンボリックリンクの項目が 1 つでもあれば、
何も変更せずに ValueError で中止する（パストラバーサル防止）。
"""
from __future__ import annotations

import io
import json
import shutil
import stat
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from .export import (
    BACKUPS_DIRNAME,
    EXPORT_INFO_NAME,
    MCP_CONFIG_PATH_PARTS,
    _excluded_subdirs,
    build_export_zip,
    iter_export_files,
)
from .brain import BRAIN_CREDENTIALS_PATH_PARTS
from .google_calendar import CREDENTIALS_PATH_PARTS
from .memory import (
    normalize_tenant_name,
    normalize_user_name,
    tenant_dir,
    user_dir,
)

# 復元を受け付ける ZIP のサイズ上限（バイト）。Web では base64 で
# 送られてくるため、メモリに乗り切る範囲に抑える
MAX_IMPORT_BYTES = 100 * 1024 * 1024

_EXPORT_FORMAT = "kuma-agent export"


@dataclass
class RestoreResult:
    """復元の結果。

    tenant / user は復元先（正規化済み）、source_tenant /
    source_user は ZIP の export-info.json に記録されていた
    書き出し元の組み合わせ。backup_path は復元前に取った控えの
    パスで、対象が空だった場合は None。
    """

    tenant: str
    user: str
    source_tenant: str
    source_user: str
    restored_files: int
    skipped_files: int
    backup_path: Path | None


def read_export_info(zip_bytes: bytes) -> dict:
    """ZIP の export-info.json を読み、書き出し情報の辞書を返す。

    ZIP として開けない、export-info.json が無い、その内容が
    kuma-agent の書き出し（format が "kuma-agent export"）で
    なければ ValueError。
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            try:
                raw = zf.read(EXPORT_INFO_NAME)
            except KeyError:
                raise ValueError(
                    "export-info.json が入っていません。"
                    "kuma-agent の書き出し ZIP を指定してください")
    except zipfile.BadZipFile:
        raise ValueError("ZIP ファイルとして読み込めませんでした")
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("export-info.json の内容が不正です")
    if not isinstance(info, dict) or info.get("format") != _EXPORT_FORMAT:
        raise ValueError("kuma-agent の書き出し ZIP ではありません")
    return info


def user_backup_filename(tenant: str, user: str,
                         now: datetime | None = None) -> str:
    """復元前の控え ZIP のファイル名。

    形は user-<テナント>-<ユーザー>-<日時>.zip（日時は
    YYYYMMDD-HHMMSS）。テナント・ユーザーは通常の正規化を通す
    ため、ファイル名に使えない文字は含まれない。
    """
    now = now or datetime.now()
    return (f"user-{normalize_tenant_name(tenant)}"
            f"-{normalize_user_name(user)}-{now:%Y%m%d-%H%M%S}.zip")


def save_user_backup(data_dir: Path, tenant: str, user: str,
                     now: datetime | None = None) -> Path:
    """ユーザーの現在のデータを控え ZIP として backups/ に保存する。

    控えの置き場はデータ置き場ルートの backups/（ユーザーの置き
    場の外）で、置き場を置き換えても控えは残る。中身はユーザー
    単位の書き出し ZIP（build_export_zip）そのもの。保存に失敗
    したら OSError をそのまま伝える（呼び出し側は、控えが作れ
    なければ復元に進まない判断に使う）。
    """
    now = now or datetime.now()
    body = build_export_zip(data_dir, tenant, user, now)
    backups_dir = data_dir / BACKUPS_DIRNAME
    backups_dir.mkdir(parents=True, exist_ok=True)
    path = backups_dir / user_backup_filename(tenant, user, now)
    path.write_bytes(body)
    return path


def _excluded_top_names(data_dir: Path, tenant_data_dir: Path,
                        user_data_dir: Path) -> set[str]:
    """復元で触らない、対象置き場直下の名前（他の置き場の入口）。

    書き出しの除外（export._excluded_subdirs）と同じ考え方で、
    除外対象のサブディレクトリの名前だけを取り出す。
    """
    return {path.name for path
            in _excluded_subdirs(data_dir, tenant_data_dir, user_data_dir)}


def _validated_entries(
        zf: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, tuple[str, ...]]]:
    """ZIP の項目をすべて検証し、書き出し対象の一覧を返す。

    ディレクトリ項目と、ルートの export-info.json（書き出し情報
    自体はデータではない）は飛ばす。絶対パス・`..` の要素を含む
    項目やシンボリックリンクの項目があれば ValueError（呼び出し
    側は何も変更していない段階で中止できる）。
    """
    entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if name == EXPORT_INFO_NAME:
            continue
        path = PurePosixPath(name)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError(
                f"不正なパスの項目が含まれています: {name}")
        if stat.S_ISLNK(info.external_attr >> 16):
            raise ValueError(
                f"シンボリックリンクの項目は復元できません: {name}")
        entries.append((info, path.parts))
    return entries


def _clear_tree(directory: Path, prefix: tuple[str, ...],
                preserved_files: set[tuple[str, ...]]) -> None:
    """ディレクトリの中身を消す（保全対象のファイルは残す）。

    prefix はユーザー置き場から見た directory の相対パスの要素。
    preserved_files は置き場からの相対パスの要素の組で、一致
    するファイルは中身ごと残す。保全のために残ったファイルを
    含むディレクトリは消さず、空になったディレクトリは消す。
    """
    for child in list(directory.iterdir()):
        parts = prefix + (child.name,)
        if child.is_dir() and not child.is_symlink():
            _clear_tree(child, parts, preserved_files)
            if not any(child.iterdir()):
                child.rmdir()
        elif parts in preserved_files:
            continue
        else:
            child.unlink()


def _clear_user_dir(user_data_dir: Path, preserved: set[str],
                    preserved_files: set[tuple[str, ...]] | None = None
                    ) -> None:
    """ユーザー置き場の中身を空にする（残す名前の配下は触らない）。

    ファイルは削除し、ディレクトリは中身ごと削除する。
    preserved_files（置き場からの相対パス）にあたるファイルは、
    そのファイルだけを残して周りを消す（資格情報の保全用）。
    置き場自体が無ければ何もしない。
    """
    if not user_data_dir.is_dir():
        return
    keep = preserved_files or set()
    for child in user_data_dir.iterdir():
        if child.name in preserved and child.is_dir() \
                and not child.is_symlink():
            continue
        if child.is_dir() and not child.is_symlink():
            _clear_tree(child, (child.name,), keep)
            if not any(child.iterdir()):
                child.rmdir()
        elif (child.name,) in keep:
            continue
        else:
            child.unlink()


def restore_user_data(data_dir: Path, tenant: str, user: str,
                      zip_bytes: bytes,
                      now: datetime | None = None) -> RestoreResult:
    """書き出し ZIP の内容で、テナント×ユーザーの置き場を置き換える。

    手順は、検証（サイズ・書き出し情報・全項目のパス）→ 対象に
    データがあれば控え → 置き場を空にする → 項目の書き出し。
    検証で落ちる場合は何も変更しない。除外対象（他のユーザー／
    テナントの置き場）と資格情報ファイル・MCP 設定にあたる項目
    は書き込まず、skipped_files に数える。既存の資格情報ファイル
    と MCP 設定は置き換えでも消さずに保全する。
    """
    if len(zip_bytes) > MAX_IMPORT_BYTES:
        raise ValueError(
            f"ZIP が大きすぎます（上限は {MAX_IMPORT_BYTES} バイトです）")
    info = read_export_info(zip_bytes)
    tenant_name = normalize_tenant_name(tenant)
    user_name = normalize_user_name(user)
    source_tenant = info.get("tenant")
    source_user = info.get("user")
    result = RestoreResult(
        tenant=tenant_name,
        user=user_name,
        source_tenant=(normalize_tenant_name(source_tenant)
                       if isinstance(source_tenant, str) else tenant_name),
        source_user=(normalize_user_name(source_user)
                     if isinstance(source_user, str) else user_name),
        restored_files=0,
        skipped_files=0,
        backup_path=None,
    )
    tdir = tenant_dir(data_dir, tenant_name)
    udir = user_dir(tdir, user_name)
    excluded = _excluded_top_names(data_dir, tdir, udir)
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("ZIP ファイルとして読み込めませんでした")
    with zf:
        entries = _validated_entries(zf)
        accepted: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for entry in entries:
            if entry[1][0] in excluded:
                result.skipped_files += 1
            elif entry[1] in (CREDENTIALS_PATH_PARTS,
                              BRAIN_CREDENTIALS_PATH_PARTS,
                              MCP_CONFIG_PATH_PARTS):
                # 資格情報と MCP 設定は ZIP からは書き戻さない
                result.skipped_files += 1
            else:
                accepted.append(entry)
        # 控えの要否: 除外配下を除いたファイルが 1 つでもあれば取る。
        # データ置き場そのものが対象のときは、サーバーの控え置き場
        # （backups/）の中身はユーザーのデータとして数えない
        existing = iter_export_files(data_dir, tenant_name, user_name)
        if udir == data_dir:
            backups_dir = data_dir / BACKUPS_DIRNAME
            existing = [p for p in existing
                        if not p.is_relative_to(backups_dir)]
        if existing:
            result.backup_path = save_user_backup(
                data_dir, tenant_name, user_name, now)
        preserved = set(excluded)
        if udir == data_dir:
            preserved.add(BACKUPS_DIRNAME)
        _clear_user_dir(udir, preserved,
                        preserved_files={CREDENTIALS_PATH_PARTS,
                                         BRAIN_CREDENTIALS_PATH_PARTS,
                                         MCP_CONFIG_PATH_PARTS})
        udir.mkdir(parents=True, exist_ok=True)
        for zip_info, parts in accepted:
            dest = udir.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(zip_info) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            result.restored_files += 1
    return result
