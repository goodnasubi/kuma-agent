"""テナントごとの設定（settings.json）。

データ置き場ルートの settings.json に
{"tenants": {"<テナント名>": {"proactive_enabled": true/false}}}
と書くと、テナントごとに能動発信（エージェントから先に発言する
仕組み）の ON/OFF を切り替えられる。保持期間のテナント別指定が
retention.json に分かれているのと同じ考え方で、こちらは振る舞いの
設定を持つ。

ファイルが無い・壊れている場合や、そのテナントの指定が無い場合は
既定値で動く（能動発信は ON）。値が真偽値でない項目は無視する。
テナント名は通常の正規化を通して照合する。
"""
from __future__ import annotations

import json
from pathlib import Path

from .memory import normalize_tenant_name

SETTINGS_FILENAME = "settings.json"

# 能動発信の既定値（settings.json に指定が無いテナントはこれで動く）
DEFAULT_PROACTIVE_ENABLED = True


def _load_raw(data_dir: Path) -> dict:
    """settings.json を辞書として読む。無い・壊れていたら空の辞書。"""
    try:
        raw = json.loads(
            (data_dir / SETTINGS_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_tenant_settings(data_dir: Path) -> dict[str, dict[str, bool]]:
    """テナントごとの設定を読む。

    戻り値は {正規化したテナント名: {"proactive_enabled": 真偽値}} で、
    真偽値として読める項目だけを含む。指定が無いテナントは含まれ
    ない（呼び出し側で既定値に倒す）。
    """
    raw = _load_raw(data_dir)
    tenants = raw.get("tenants")
    if not isinstance(tenants, dict):
        return {}
    settings: dict[str, dict[str, bool]] = {}
    for name, entry in tenants.items():
        if not isinstance(entry, dict):
            continue
        enabled = entry.get("proactive_enabled")
        if isinstance(enabled, bool):
            settings[normalize_tenant_name(name)] = {
                "proactive_enabled": enabled}
    return settings


def proactive_enabled_for(data_dir: Path, tenant: str | None) -> bool:
    """テナントの能動発信が有効かどうかを返す（未設定なら既定で有効）。"""
    entry = load_tenant_settings(data_dir).get(normalize_tenant_name(tenant))
    if entry is None:
        return DEFAULT_PROACTIVE_ENABLED
    return entry["proactive_enabled"]


def set_proactive_enabled(data_dir: Path, tenant: str | None,
                          enabled: bool) -> None:
    """テナントの能動発信 ON/OFF を settings.json に保存する。

    他のテナントの指定や、同じテナントの他の設定項目はそのまま
    残す。既存ファイルが壊れている場合は、読めた範囲を引き継げ
    ないため、この指定だけを持つ内容として書き直す。
    """
    name = normalize_tenant_name(tenant)
    raw = _load_raw(data_dir)
    tenants = raw.get("tenants")
    if not isinstance(tenants, dict):
        tenants = {}
    # 既存のキーも正規化して引き継ぐ（照合の流儀にそろえる）
    merged: dict[str, object] = {
        normalize_tenant_name(key): value for key, value in tenants.items()}
    entry = merged.get(name)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry["proactive_enabled"] = bool(enabled)
    merged[name] = entry
    raw["tenants"] = merged
    data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(raw, ensure_ascii=False, indent=2)
    (data_dir / SETTINGS_FILENAME).write_text(text + "\n", encoding="utf-8")
