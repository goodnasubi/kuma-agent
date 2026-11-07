"""テナントごとの頭脳（LLM プロバイダ／モデル）の解決。

頭脳は全体共通（Config、環境変数）が既定で、テナントごとに
settings.json の brain 項目で上書きできる。解決はこのモジュール
に集約し、セッション生成の入口（Web の TenantStore、CLI の
main、Telegram / LINE の factory）から使う。

- 解決結果は「テナント用に差し替えた Config（scoped Config）＋
  それから作ったモデル実体」の組で返す。Session の下流は
  cfg.provider（使用量の記録・振る舞いの分岐・委任の子の生成）
  と実体の両方を見るため、組で渡すことで下流を無変更に保てる
- API キーは解決して scoped Config の model_api_key へ書き戻す
  （resolve_api_key が model_api_key を最優先で見る既存の仕組み
  を使い、委任の子も同じキーで追随できるようにする）
- 第1段（0.81.0）では、テナント指定できるのは API 系のプロバ
  イダだけとする。CLI 系のテナント別は第3段（運用者スイッチ
  付き）の範囲で、設定に CLI 系が書かれていても読み取りで
  無視する。API キー自体は全体共通（環境変数）のまま

設計の正は docs/DESIGN.md の「テナントごとの頭脳」の節。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .agent_cli import CLI_PROVIDERS
from .config import Config
from .llm import API_PROVIDERS, create_client, default_model_name
from .memory import normalize_tenant_name
from .settings import brain_setting_for

# 頭脳の候補一覧の正（API 系＋CLI 系）。llm.PROVIDERS は循環
# import を避けた写しで、候補の組み立てはこちらを正とする
ALL_PROVIDERS = API_PROVIDERS + CLI_PROVIDERS

# テナント指定として受け付けるプロバイダの範囲。第1段は API 系
# のみ（CLI 系のテナント別は第3段で運用者スイッチ付きで解禁する）
TENANT_BRAIN_PROVIDERS = API_PROVIDERS

# モデル名の長さ上限（管理タブからの保存時の検証に使う）
MAX_BRAIN_MODEL_CHARS = 200


def tenant_brain_setting(data_dir: Path,
                         tenant: str | None) -> dict[str, str]:
    """検証済みのテナント頭脳の指定を返す（provider / model）。

    settings.json の指定（settings.brain_setting_for）から、その
    段で許可されていないプロバイダ（未知の名前・CLI 系）はその
    項目だけ落とし、空のモデル名も未指定として落とす。壊れた
    指定があっても全体の既定に倒れる（そのテナントが設定ミスで
    全滅しないための流儀）。
    """
    raw = brain_setting_for(data_dir, tenant)
    setting: dict[str, str] = {}
    provider = raw.get("provider")
    if provider in TENANT_BRAIN_PROVIDERS:
        setting["provider"] = provider
    model = raw.get("model")
    if model:
        setting["model"] = model
    return setting


def scoped_config(base_cfg: Config, tenant: str | None) -> Config:
    """テナントの指定を反映した Config を作る（実体は作らない）。

    テナント名・プロバイダ・モデルを差し替え、API 系では API
    キーも解決して model_api_key へ書き戻す。プロバイダが全体の
    既定と違う場合は、全体用に解決済みのキーを流用しないよう
    model_api_key を空にしてから引き直す（別プロバイダへ他人の
    キーを送る事故の防止。第2段のテナント別キーでも同じ考え方）。
    """
    name = normalize_tenant_name(tenant)
    setting = tenant_brain_setting(base_cfg.data_dir, name)
    cfg = replace(base_cfg, tenant=name)
    if "provider" in setting:
        cfg = replace(cfg, provider=setting["provider"])
    if "model" in setting:
        cfg = replace(cfg, model=setting["model"])
    if not cfg.is_cli_provider:
        probe = cfg if cfg.provider == base_cfg.provider else replace(
            cfg, model_api_key="")
        cfg = replace(cfg, model_api_key=probe.resolve_api_key())
    return cfg


def effective_brain(base_cfg: Config,
                    tenant: str | None) -> tuple[str, str, bool]:
    """テナントの実効頭脳を (プロバイダ, モデル名, CLI 系か) で返す。

    クライアントは作らない（表示・API の応答用）。モデル名は
    指定値 → 全体の既定 → プロバイダの既定モデル名の順に解決し、
    どれも無ければ空文字列。
    """
    provider, model, _ = _effective_parts(base_cfg, tenant)
    if not model:
        model = default_model_name(provider) or ""
    return provider, model, provider in CLI_PROVIDERS


def _effective_parts(base_cfg: Config,
                     tenant: str | None) -> tuple[str, str | None, bool]:
    setting = tenant_brain_setting(base_cfg.data_dir, tenant)
    provider = setting.get("provider", base_cfg.provider)
    model = setting.get("model", base_cfg.model)
    return provider, model, provider in CLI_PROVIDERS


def resolve_brain(base_cfg: Config, tenant: str | None,
                  fallback_model=None):
    """テナントの (scoped Config, モデル実体) の組を返す。

    テナント指定があれば、その指定でクライアントを作る。指定が
    無ければ fallback_model（呼び出し側が全体の既定で作った実体）
    をそのまま使い、無ければ全体の既定のクライアントを作る。
    生成に失敗した場合は例外を送出する（呼び出し側で、その
    テナント分だけの失敗として扱う。失敗はキャッシュしない
    前提のため、この関数自体は状態を持たない）。
    """
    setting = tenant_brain_setting(base_cfg.data_dir, tenant)
    cfg = scoped_config(base_cfg, tenant)
    if not setting and fallback_model is not None:
        return cfg, fallback_model
    model = create_client(cfg.provider, cfg.resolve_api_key(),
                          cfg.model, **cfg.cli_client_kwargs())
    return cfg, model


def global_brain_base(cfg: Config) -> Config:
    """全体の既定（環境変数由来）だけを持つ Config を返す。

    Telegram / LINE の factory が、プロセスのテナントとは別の
    テナントの頭脳を解決するときの土台。data_dir だけは呼び出し
    側の Config から引き継ぐ（データ置き場は環境変数とは別に
    差し替えられている場合があるため）。
    """
    return replace(Config(), data_dir=cfg.data_dir)


def resolve_peer_brain(cfg: Config, tenant: str | None, fallback_model):
    """行き先がプロセスのテナントと違い得る経路の頭脳を解決する。

    Telegram / LINE の factory 用。fallback_model は「全体の既定
    で作られた実体」として渡されるが、プロセスのテナントに頭脳
    の指定がある場合は、そのテナント専用の実体になっている
    可能性があるため、そのままでは使い回さない。

    - 行き先に指定があれば、その指定で作る（土台は、行き先が
      プロセスのテナントと同じなら cfg、違うなら全体の既定）
    - 行き先に指定が無く、プロセスのテナントにも指定が無ければ、
      fallback_model をそのまま使う
    - 行き先に指定が無いがプロセスのテナントには指定がある
      場合は、全体の既定で作り直す
    """
    name = normalize_tenant_name(tenant)
    if tenant_brain_setting(cfg.data_dir, name):
        base = cfg if name == normalize_tenant_name(cfg.tenant) \
            else global_brain_base(cfg)
        return resolve_brain(base, name)
    if tenant_brain_setting(cfg.data_dir, cfg.tenant):
        return resolve_brain(global_brain_base(cfg), name)
    return scoped_config(cfg, name), fallback_model
