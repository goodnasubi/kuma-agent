"""手順書（スキル）の走査・解析・保存。

手順書は、特定の仕事の進め方を Markdown で書いた再利用可能な
指示書。データ置き場ルートの skills/ に置き、全テナント共通で
使う（運用者が置く前提。人格や長期記憶と違い、テナントや
ユーザーでは分けない）。

置き方は 2 通り:
- フォルダ形式: skills/<名前>/SKILL.md
- 単一ファイル: skills/<名前>.md

SKILL.md の先頭には簡易ヘッダー（`---` で囲んだ `name:` と
`description:`）を置ける。YAML ライブラリは使わず、行単位の
単純な解析で読む。ヘッダーが無い・欠けている場合は、名前は
ファイル/フォルダ名から、説明は本文の最初の見出しまたは先頭行
から補う。読めないファイルがあっても走査全体は落とさない。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

SKILLS_DIRNAME = "skills"
SKILL_FILENAME = "SKILL.md"

# 派生させる説明文の上限（目次が長くならないように）
_DERIVED_DESCRIPTION_LIMIT = 120


@dataclass
class Skill:
    """手順書 1 件。body はヘッダーを除いた本文。"""
    name: str
    description: str
    body: str
    path: Path  # 読み込み元（フォルダ形式は SKILL.md、単一ファイルはそのファイル）


def skills_root(data_dir: Path) -> Path:
    """手順書の置き場（データ置き場ルート直下の skills/）。"""
    return data_dir / SKILLS_DIRNAME


def is_valid_skill_name(name: str) -> bool:
    """手順書の名前がパスに使える安全な形かどうか。

    使えるのは英数（日本語の文字を含む）と "-" "_" だけ。
    パス区切り・ドット（".." を含む）・空白などは不可。
    """
    if not name or name != name.strip():
        return False
    return all(ch.isalnum() or ch in "-_" for ch in name)


def _unquote(value: str) -> str:
    """前後を同じ引用符で囲まれていたら外す（簡易ヘッダーの値用）。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_header(text: str) -> tuple[dict[str, str], str] | None:
    """先頭の `---` ヘッダーを (項目辞書, 残りの本文) に分ける。

    ヘッダーが無い、または閉じられていない場合は None（本文全体を
    ヘッダー無しとして扱う）。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    meta: dict[str, str] = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:])
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip().lower()] = _unquote(value.strip())
    return None


def _derive_description(body: str) -> str:
    """本文から説明を補う（最初の見出し、無ければ最初の行）。

    見出しらしい行（# で始まる）が本文の早い段階にあればそれを
    優先し、無ければ最初の空でない行を使う。長すぎる場合は
    目次向けに切り詰める。
    """
    first_line = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            return _trim_description(line.lstrip("#").strip())
        if not first_line:
            first_line = line
    return _trim_description(first_line)


def _trim_description(text: str) -> str:
    if len(text) > _DERIVED_DESCRIPTION_LIMIT:
        return text[:_DERIVED_DESCRIPTION_LIMIT] + "…"
    return text


def parse_skill_text(text: str,
                     fallback_name: str) -> tuple[str, str, str]:
    """手順書の本文テキストを (名前, 説明, 本文) に分ける。

    ヘッダーに name / description があればそれを使い、欠けた分は
    フォールバック名と本文から補う。本文は前後の空白行を除いた
    ものを返す。
    """
    parsed = _parse_header(text)
    if parsed is not None:
        meta, body = parsed
        name = meta.get("name") or fallback_name
        description = meta.get("description") or _derive_description(body)
        return name, description, body.strip()
    return fallback_name, _derive_description(text), text.strip()


def _read_skill_file(path: Path, fallback_name: str) -> Skill | None:
    """手順書ファイルを 1 件読む。読めなければ None（読み飛ばす）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    name, description, body = parse_skill_text(text, fallback_name)
    return Skill(name=name, description=description, body=body, path=path)


def load_skills(data_dir: Path) -> list[Skill]:
    """データ置き場ルートの skills/ を走査して手順書の一覧を返す。

    フォルダ形式（<名前>/SKILL.md）と単一ファイル形式（<名前>.md）
    の両方を読む。走査順は名前順で決定的にし、同じ名前が重なった
    ら先勝ちにする。置き場が無い・読めない場合は空の一覧を返す。
    """
    root = skills_root(data_dir)
    if not root.is_dir():
        return []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    skills: list[Skill] = []
    seen: set[str] = set()
    for entry in entries:
        skill: Skill | None = None
        if entry.is_dir():
            target = entry / SKILL_FILENAME
            if target.is_file():
                skill = _read_skill_file(target, entry.name)
        elif entry.is_file() and entry.suffix == ".md":
            skill = _read_skill_file(entry, entry.stem)
        if skill is None or skill.name in seen:
            continue
        seen.add(skill.name)
        skills.append(skill)
    return skills


def load_skill(data_dir: Path, name: str) -> Skill | None:
    """名前で手順書を 1 件引く。無ければ None。"""
    for skill in load_skills(data_dir):
        if skill.name == name:
            return skill
    return None


def save_skill(data_dir: Path, name: str, description: str,
               body: str) -> Path:
    """手順書をフォルダ形式（skills/<名前>/SKILL.md）で保存する。

    既存の同名手順書があれば上書きする。名前が安全な形でなければ
    ValueError。説明はヘッダーに 1 行で書くため空白を潰す。
    保存したファイルのパスを返す。
    """
    name = name.strip()
    if not is_valid_skill_name(name):
        raise ValueError(f"手順書の名前が不正です: {name!r}")
    desc = " ".join(str(description).split())
    folder = skills_root(data_dir) / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SKILL_FILENAME
    text = (f"---\nname: {name}\ndescription: {desc}\n---\n\n"
            f"{body.strip()}\n")
    path.write_text(text, encoding="utf-8")
    return path
