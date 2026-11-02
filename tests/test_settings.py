"""テナント設定（settings.json）と保持期間の保存のテスト。

能動発信の ON/OFF の読み書きと既定値（未設定なら ON）、壊れた
ファイルでも既定で動くこと、保持期間の上書き保存が他のテナントの
指定を壊さないことを確認する。
"""
import json
from pathlib import Path

from kuma_agent.memory import (load_retention_overrides,
                               save_retention_override)
from kuma_agent.settings import (load_tenant_settings,
                                 proactive_enabled_for,
                                 set_proactive_enabled)


def _write_settings(data_dir: Path, content: object) -> Path:
    path = data_dir / "settings.json"
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, ensure_ascii=False),
                        encoding="utf-8")
    return path


# ---------------------------------------------------------------- 能動発信の設定

def test_proactive_enabled_defaults_to_true(tmp_path: Path):
    assert proactive_enabled_for(tmp_path, "default") is True
    assert proactive_enabled_for(tmp_path, "acme") is True
    assert load_tenant_settings(tmp_path) == {}


def test_set_and_load_proactive_enabled(tmp_path: Path):
    set_proactive_enabled(tmp_path, "acme", False)
    assert proactive_enabled_for(tmp_path, "acme") is False
    # 他のテナントは未設定のまま既定（ON）
    assert proactive_enabled_for(tmp_path, "other") is True
    assert load_tenant_settings(tmp_path) == {
        "acme": {"proactive_enabled": False}}

    set_proactive_enabled(tmp_path, "acme", True)
    assert proactive_enabled_for(tmp_path, "acme") is True
    raw = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert raw == {"tenants": {"acme": {"proactive_enabled": True}}}


def test_set_proactive_enabled_preserves_others(tmp_path: Path):
    _write_settings(tmp_path, {"tenants": {
        "acme": {"proactive_enabled": False, "other_key": 1},
        "beta": {"proactive_enabled": True},
    }, "unrelated": "keep"})
    set_proactive_enabled(tmp_path, "beta", False)
    raw = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    # 他テナント・他キー・同じテナントの他の項目は壊れない
    assert raw["tenants"]["acme"] == {
        "proactive_enabled": False, "other_key": 1}
    assert raw["tenants"]["beta"] == {"proactive_enabled": False}
    assert raw["unrelated"] == "keep"


def test_tenant_name_is_normalized_in_settings(tmp_path: Path):
    set_proactive_enabled(tmp_path, "acme corp", False)
    assert proactive_enabled_for(tmp_path, "acme-corp") is False
    assert proactive_enabled_for(tmp_path, "acme corp") is False


def test_broken_settings_file_falls_back_to_default(tmp_path: Path):
    _write_settings(tmp_path, "{壊れた JSON")
    assert proactive_enabled_for(tmp_path, "acme") is True
    assert load_tenant_settings(tmp_path) == {}

    _write_settings(tmp_path, {"tenants": ["acme"]})  # 構造が違う
    assert proactive_enabled_for(tmp_path, "acme") is True

    _write_settings(tmp_path, {"tenants": {"acme": {"proactive_enabled": "yes"}}})
    assert proactive_enabled_for(tmp_path, "acme") is True  # 真偽値以外は無視

    # 壊れたファイルの上からでも保存できる
    _write_settings(tmp_path, "{壊れた JSON")
    set_proactive_enabled(tmp_path, "acme", False)
    assert proactive_enabled_for(tmp_path, "acme") is False


# ---------------------------------------------------------------- 保持期間の保存

def test_save_retention_override(tmp_path: Path):
    save_retention_override(tmp_path, "acme", 30)
    assert load_retention_overrides(tmp_path) == {"acme": 30}
    raw = json.loads((tmp_path / "retention.json").read_text(encoding="utf-8"))
    assert raw == {"tenants": {"acme": 30}}

    # 0（無期限）も指定として保存できる
    save_retention_override(tmp_path, "acme", 0)
    assert load_retention_overrides(tmp_path) == {"acme": 0}


def test_save_retention_override_preserves_and_removes(tmp_path: Path):
    save_retention_override(tmp_path, "acme", 30)
    save_retention_override(tmp_path, "beta", 10)
    assert load_retention_overrides(tmp_path) == {"acme": 30, "beta": 10}

    # None でそのテナントの指定だけを取り除く
    save_retention_override(tmp_path, "acme", None)
    assert load_retention_overrides(tmp_path) == {"beta": 10}

    # 無いテナントの取り除きは何もしない
    save_retention_override(tmp_path, "missing", None)
    assert load_retention_overrides(tmp_path) == {"beta": 10}
