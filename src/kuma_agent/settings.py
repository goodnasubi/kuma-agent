"""テナントごとの設定（settings.json）。

データ置き場ルートの settings.json に
{"tenants": {"<テナント名>": {"proactive_enabled": true/false,
                              "approvals_enabled": true/false}}}
と書くと、テナントごとに振る舞いを切り替えられる。保持期間の
テナント別指定が retention.json に分かれているのと同じ考え方で、
こちらは振る舞いの設定を持つ。

- proactive_enabled: 能動発信（エージェントから先に発言する仕組み）
  の ON/OFF
- approvals_enabled: 危険な道具（run_shell と MCP 道具）の実行前に
  利用者の承認を挟むかどうか
- brain: テナントごとの頭脳（プロバイダ／モデル）の上書き
  （{"provider": ..., "model": ...}。どちらも省略可）。読み書きは
  brain_setting_for / set_brain_setting が担い、値の検証（未知の
  プロバイダを落とす等）は解決側（brain.py）の責務とする。秘密
  （API キー）はこのファイルには絶対に入れない

ファイルが無い・壊れている場合や、そのテナントの指定が無い場合は
既定値で動く（どちらも ON）。値が真偽値でない項目は無視する。
テナント名は通常の正規化を通して照合する。
"""
from __future__ import annotations

import json
from pathlib import Path

from .memory import normalize_tenant_name

SETTINGS_FILENAME = "settings.json"

# 能動発信の既定値（settings.json に指定が無いテナントはこれで動く）
DEFAULT_PROACTIVE_ENABLED = True

# 承認の既定値（指定が無いテナントは承認を挟む側＝安全側で動く）
DEFAULT_APPROVALS_ENABLED = True

# 真偽値として読む設定項目（キー → 既定値）。ファイルに真偽値で
# 書かれている項目だけを読み取り、指定が無い項目は含めない
_BOOL_KEYS = ("proactive_enabled", "approvals_enabled")


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

    戻り値は {正規化したテナント名: {項目名: 真偽値}} で、真偽値と
    して読める項目だけを含む。指定が無いテナント・項目は含まれ
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
        parsed = {key: entry[key] for key in _BOOL_KEYS
                  if isinstance(entry.get(key), bool)}
        if parsed:
            settings[normalize_tenant_name(name)] = parsed
    return settings


def _bool_setting_for(data_dir: Path, tenant: str | None, key: str,
                      default: bool) -> bool:
    """テナントの真偽値設定を読む（未設定なら既定値）。"""
    entry = load_tenant_settings(data_dir).get(normalize_tenant_name(tenant))
    if entry is None:
        return default
    return entry.get(key, default)


def proactive_enabled_for(data_dir: Path, tenant: str | None) -> bool:
    """テナントの能動発信が有効かどうかを返す（未設定なら既定で有効）。"""
    return _bool_setting_for(data_dir, tenant, "proactive_enabled",
                             DEFAULT_PROACTIVE_ENABLED)


def approvals_enabled_for(data_dir: Path, tenant: str | None) -> bool:
    """テナントの承認（危険な道具の実行前確認）が有効かどうかを返す。

    未設定なら既定で有効（承認を挟む側＝安全側）。
    """
    return _bool_setting_for(data_dir, tenant, "approvals_enabled",
                             DEFAULT_APPROVALS_ENABLED)


def _merged_tenants(raw: dict) -> dict[str, object]:
    """raw の tenants を、キーを正規化した辞書として返す（保存用）。

    既存のキーも正規化して引き継ぐ（照合の流儀にそろえる）。
    tenants が辞書でなければ空の辞書。
    """
    tenants = raw.get("tenants")
    if not isinstance(tenants, dict):
        return {}
    return {normalize_tenant_name(key): value
            for key, value in tenants.items()}


def _write_raw(data_dir: Path, raw: dict) -> None:
    """settings.json を書き出す（保存の共通処理）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(raw, ensure_ascii=False, indent=2)
    (data_dir / SETTINGS_FILENAME).write_text(text + "\n", encoding="utf-8")


def _set_bool_setting(data_dir: Path, tenant: str | None, key: str,
                      enabled: bool) -> None:
    """テナントの真偽値設定を settings.json に保存する（共通処理）。

    他のテナントの指定や、同じテナントの他の設定項目はそのまま
    残す。既存ファイルが壊れている場合は、読めた範囲を引き継げ
    ないため、この指定だけを持つ内容として書き直す。
    """
    name = normalize_tenant_name(tenant)
    raw = _load_raw(data_dir)
    merged = _merged_tenants(raw)
    entry = merged.get(name)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry[key] = bool(enabled)
    merged[name] = entry
    raw["tenants"] = merged
    _write_raw(data_dir, raw)


def remove_tenant_settings(data_dir: Path, tenant: str | None) -> None:
    """テナントの設定項目を settings.json から取り除く（テナント削除用）。

    そのテナントの指定が無ければ何もしない（ファイルも作らない）。
    他のテナントの指定はそのまま残す。既存ファイルが壊れている
    場合は、引き継げる指定が無いため何もしない。
    """
    name = normalize_tenant_name(tenant)
    raw = _load_raw(data_dir)
    if not isinstance(raw.get("tenants"), dict):
        return
    # 既存のキーも正規化して照合する（読み書きの流儀にそろえる）
    merged = _merged_tenants(raw)
    if name not in merged:
        return
    del merged[name]
    raw["tenants"] = merged
    _write_raw(data_dir, raw)


def set_proactive_enabled(data_dir: Path, tenant: str | None,
                          enabled: bool) -> None:
    """テナントの能動発信 ON/OFF を settings.json に保存する。"""
    _set_bool_setting(data_dir, tenant, "proactive_enabled", enabled)


def set_approvals_enabled(data_dir: Path, tenant: str | None,
                          enabled: bool) -> None:
    """テナントの承認 ON/OFF を settings.json に保存する。"""
    _set_bool_setting(data_dir, tenant, "approvals_enabled", enabled)


# ---------------------------------------------------------------- 頭脳

# 頭脳の指定として読む項目（文字列の値だけを拾う）
_BRAIN_KEYS = ("provider", "model")


def brain_setting_for(data_dir: Path, tenant: str | None) -> dict[str, str]:
    """テナントの頭脳の指定（provider / model）を読む。

    settings.json の brain 項目から、文字列で書かれている項目だけ
    を返す。指定が無い・壊れている場合は空の辞書。プロバイダ名の
    正しさ（未知の名前や、その段で許可されていない範囲）などの
    検証はここでは行わない（解決側＝brain.py の責務）。秘密
    （API キー）はこのファイルには置かないため、ここにも現れない。
    """
    entry = _merged_tenants(_load_raw(data_dir)).get(
        normalize_tenant_name(tenant))
    if not isinstance(entry, dict):
        return {}
    brain = entry.get("brain")
    if not isinstance(brain, dict):
        return {}
    return {key: brain[key] for key in _BRAIN_KEYS
            if isinstance(brain.get(key), str)}


def all_brain_settings(data_dir: Path) -> dict[str, dict[str, str]]:
    """全テナントの頭脳の指定を {テナント名: 指定} で返す。

    brain_setting_for と同じ読み方（文字列の項目だけ）を、settings
    .json に書かれている全テナント分について行う。指定が無い
    テナントは含めない。起動時の注意で「どのテナントがどの頭脳を
    指定しているか」を走査するために使う。
    """
    result: dict[str, dict[str, str]] = {}
    for name, entry in _merged_tenants(_load_raw(data_dir)).items():
        if not isinstance(entry, dict):
            continue
        brain = entry.get("brain")
        if not isinstance(brain, dict):
            continue
        parsed = {key: brain[key] for key in _BRAIN_KEYS
                  if isinstance(brain.get(key), str)}
        if parsed:
            result[name] = parsed
    return result


def set_brain_setting(data_dir: Path, tenant: str | None,
                      provider: str | None, model: str | None) -> None:
    """テナントの頭脳の指定を settings.json に保存する。

    provider / model は、文字列を渡すとその項目を指定し、None を
    渡すとその項目を取り除く。両方とも取り除かれたら brain の
    指定ごと消え、そのテナントは全体の既定に戻る。他のテナント
    の指定や、同じテナントの他の設定項目はそのまま残す。
    """
    name = normalize_tenant_name(tenant)
    raw = _load_raw(data_dir)
    merged = _merged_tenants(raw)
    entry = merged.get(name)
    entry = dict(entry) if isinstance(entry, dict) else {}
    brain = entry.get("brain")
    brain = dict(brain) if isinstance(brain, dict) else {}
    for key, value in (("provider", provider), ("model", model)):
        if value is None:
            brain.pop(key, None)
        else:
            brain[key] = value
    if brain:
        entry["brain"] = brain
    else:
        entry.pop("brain", None)
    merged[name] = entry
    raw["tenants"] = merged
    _write_raw(data_dir, raw)
