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
  無視する
- 第2段（0.82.0）では、テナントごとの API キーを持てる。キー
  は settings.json には入れず、テナントの置き場の専用ファイル
  （brain/credentials.json）に provider と併記して置く（Google
  カレンダーの資格情報と同じ保護・除外の流儀）。解決では、
  実効プロバイダと一致するテナントのキーが全体のキーより
  優先する

設計の正は docs/DESIGN.md の「テナントごとの頭脳」の節。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from .agent_cli import CLI_PROVIDERS
from .config import Config
from .llm import API_PROVIDERS, create_client, default_model_name
from .memory import normalize_tenant_name, tenant_dir
from .settings import brain_setting_for

# 頭脳の候補一覧の正（API 系＋CLI 系）。llm.PROVIDERS は循環
# import を避けた写しで、候補の組み立てはこちらを正とする
ALL_PROVIDERS = API_PROVIDERS + CLI_PROVIDERS

# テナント指定として受け付けるプロバイダの範囲。第1段は API 系
# のみ（CLI 系のテナント別は第3段で運用者スイッチ付きで解禁する）
TENANT_BRAIN_PROVIDERS = API_PROVIDERS

# モデル名の長さ上限（管理タブからの保存時の検証に使う）
MAX_BRAIN_MODEL_CHARS = 200

# テナントの API キーの置き場（テナントの置き場からの相対パス）。
# export.py / restore.py の除外もこの定義を正とする（Google
# カレンダーの資格情報の CREDENTIALS_PATH_PARTS と同じ分担）
BRAIN_CREDENTIALS_DIRNAME = "brain"
BRAIN_CREDENTIALS_FILENAME = "credentials.json"
BRAIN_CREDENTIALS_PATH_PARTS = (BRAIN_CREDENTIALS_DIRNAME,
                                BRAIN_CREDENTIALS_FILENAME)

# API キーの長さ上限（管理タブからの保存時の検証に使う）
MAX_BRAIN_API_KEY_CHARS = 1000


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


# ------------------------------------------------------- テナントのキー

class BrainCredentialsError(Exception):
    """テナントのキー（資格情報ファイル）の読み込み失敗。

    kind は "invalid"（ファイルはあるが壊れている）だけ。文面に
    キーの値は含めない。
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class BrainCredentials:
    """テナントの資格情報（brain/credentials.json の中身）。

    provider を併記するのは、頭脳の指定の provider が変わった
    ときに古いキーを別プロバイダへ誤って送らないため。解決時に
    実効プロバイダと一致するときだけ api_key を使う。
    """

    provider: str
    api_key: str


def brain_credentials_dir(tenant_data_dir: Path) -> Path:
    """テナントの置き場の中の、頭脳用ディレクトリのパス。"""
    return tenant_data_dir / BRAIN_CREDENTIALS_DIRNAME


def brain_credentials_path(tenant_data_dir: Path) -> Path:
    """テナントの資格情報ファイルのパス。"""
    return brain_credentials_dir(tenant_data_dir) \
        / BRAIN_CREDENTIALS_FILENAME


def _chmod_quiet(path: Path, mode: int) -> None:
    """権限を設定する。失敗しても処理は止めない（置き場の
    ファイルシステムによっては権限の概念が無いため）。"""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def save_brain_credentials(tenant_data_dir: Path, provider: str,
                           api_key: str) -> Path:
    """テナントのキーを保存し、そのパスを返す。

    ディレクトリは 0700、ファイルは 0600 を目安にし、一時
    ファイルへ書いてから os.replace で置き換える（途中で止まって
    も壊れた資格情報を残さない。Google カレンダーの資格情報と
    同じ作り）。
    """
    directory = brain_credentials_dir(tenant_data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _chmod_quiet(directory, 0o700)
    path = brain_credentials_path(tenant_data_dir)
    payload = {"provider": provider, "api_key": api_key}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    tmp = directory / (BRAIN_CREDENTIALS_FILENAME + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    _chmod_quiet(path, 0o600)
    return path


def load_brain_credentials(tenant_data_dir: Path) \
        -> BrainCredentials | None:
    """テナントのキーを読み込む。ファイルが無ければ None。

    ファイルはあるが読めない・形式が壊れている場合は
    BrainCredentialsError（kind=invalid）。読み込めた場合は、
    緩くなっているファイル権限を 0600 へ直す。
    """
    path = brain_credentials_path(tenant_data_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise BrainCredentialsError(
            "invalid", "テナントのキーが読み込めませんでした")
    if not isinstance(data, dict):
        raise BrainCredentialsError(
            "invalid", "テナントのキーが読み込めませんでした")
    provider = data.get("provider")
    key_value = data.get("api_key")
    if not isinstance(provider, str) or not provider \
            or not isinstance(key_value, str) or not key_value:
        raise BrainCredentialsError(
            "invalid", "テナントのキーが読み込めませんでした")
    _chmod_quiet(path, 0o600)
    return BrainCredentials(provider, key_value)


def delete_brain_credentials(tenant_data_dir: Path) -> bool:
    """テナントのキーのファイルを削除する。元々無ければ False。"""
    try:
        brain_credentials_path(tenant_data_dir).unlink()
        return True
    except FileNotFoundError:
        return False


def tenant_api_key(data_dir: Path, tenant: str | None,
                   provider: str) -> str | None:
    """そのテナントに効く API キーを返す（無ければ None）。

    資格情報ファイルの provider が、実効プロバイダと一致するとき
    だけキーを使う（別プロバイダへキーを送らないため）。ファイル
    が壊れている場合は「キー無し」に倒す（設定ミスでそのテナント
    が全滅しないための流儀。壊れていること自体は brain_key_status
    で画面に伝わる）。
    """
    tdir = tenant_dir(data_dir, normalize_tenant_name(tenant))
    try:
        creds = load_brain_credentials(tdir)
    except BrainCredentialsError:
        return None
    if creds is None or creds.provider != provider:
        return None
    return creds.api_key


def brain_key_status(data_dir: Path, tenant: str | None) -> dict:
    """テナントのキーの状態を返す（キーの値は含めない）。

    state は "unset"（未設定）／"set"（設定済み）／"invalid"
    （ファイルが壊れている）のいずれかで、provider は資格情報に
    併記された対象プロバイダ（"set" のときだけ）。
    """
    tdir = tenant_dir(data_dir, normalize_tenant_name(tenant))
    try:
        creds = load_brain_credentials(tdir)
    except BrainCredentialsError:
        return {"state": "invalid", "provider": None}
    if creds is None:
        return {"state": "unset", "provider": None}
    return {"state": "set", "provider": creds.provider}


def has_tenant_brain_override(data_dir: Path, tenant: str | None,
                              base_provider: str) -> bool:
    """そのテナントに頭脳の上書き（指定、または効くキー）があるか。

    base_provider は土台のプロバイダ（全体の既定、またはプロセス
    のテナントで解決済みの Config のプロバイダ）。頭脳の指定が
    無ければ実効プロバイダは土台のものになるため、キーの一致
    判定はそのプロバイダで行う。
    """
    if tenant_brain_setting(data_dir, tenant):
        return True
    return tenant_api_key(data_dir, tenant, base_provider) is not None


def scoped_config(base_cfg: Config, tenant: str | None) -> Config:
    """テナントの指定を反映した Config を作る（実体は作らない）。

    テナント名・プロバイダ・モデルを差し替え、API 系では API
    キーも解決して model_api_key へ書き戻す。キーの解決順序は、
    実効プロバイダと一致するテナントのキー（brain/credentials
    .json）が最優先で、無ければ全体のキー（環境変数）。プロバイダ
    が全体の既定と違う場合は、全体用に解決済みのキーを流用しない
    よう model_api_key を空にしてから引き直す（別プロバイダへ
    他人のキーを送る事故の防止）。
    """
    name = normalize_tenant_name(tenant)
    setting = tenant_brain_setting(base_cfg.data_dir, name)
    cfg = replace(base_cfg, tenant=name)
    if "provider" in setting:
        cfg = replace(cfg, provider=setting["provider"])
    if "model" in setting:
        cfg = replace(cfg, model=setting["model"])
    if not cfg.is_cli_provider:
        key = tenant_api_key(base_cfg.data_dir, name, cfg.provider)
        if key is not None:
            cfg = replace(cfg, model_api_key=key)
        else:
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

    テナントの上書き（頭脳の指定、または効くキー）があれば、
    その指定でクライアントを作る。上書きが無ければ
    fallback_model（呼び出し側が全体の既定で作った実体）をその
    まま使い、無ければ全体の既定のクライアントを作る。
    生成に失敗した場合は例外を送出する（呼び出し側で、その
    テナント分だけの失敗として扱う。失敗はキャッシュしない
    前提のため、この関数自体は状態を持たない）。
    """
    cfg = scoped_config(base_cfg, tenant)
    if fallback_model is not None and not has_tenant_brain_override(
            base_cfg.data_dir, tenant, base_cfg.provider):
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
    の上書き（指定または効くキー）がある場合は、そのテナント
    専用の実体になっている可能性があるため、そのままでは
    使い回さない。

    - 行き先に上書きがあれば、その内容で作る（土台は、行き先が
      プロセスのテナントと同じなら cfg、違うなら全体の既定）
    - 行き先に上書きが無く、プロセスのテナントにも無ければ、
      fallback_model をそのまま使う
    - 行き先に上書きが無いがプロセスのテナントにはある場合は、
      全体の既定で作り直す
    """
    name = normalize_tenant_name(tenant)
    same = name == normalize_tenant_name(cfg.tenant)
    base = cfg if same else global_brain_base(cfg)
    if has_tenant_brain_override(cfg.data_dir, name, base.provider):
        return resolve_brain(base, name)
    if has_tenant_brain_override(cfg.data_dir, cfg.tenant, cfg.provider):
        return resolve_brain(global_brain_base(cfg), name)
    return scoped_config(cfg, name), fallback_model
