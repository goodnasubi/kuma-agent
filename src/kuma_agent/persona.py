"""人格ファイル（SOUL.md / IDENTITY.md / USER.md）の読み込み。"""
from __future__ import annotations

from pathlib import Path

PERSONA_FILES = ("SOUL.md", "IDENTITY.md", "USER.md")

DEFAULT_SOUL = """\
# SOUL.md

- **名前はクマ**。親しみやすく、率直で、少し遊び心のある相棒。
- 分からないことは分からないと言う。推測で断言しない。
- 相手の時間を大事にし、返事は必要な分だけ。
"""


def load_persona(data_dir: Path) -> str:
    """人格ファイルを結合してシステムプロンプトの一部にする。
    ファイルがなければ既定の人格を使う。"""
    parts: list[str] = []
    for name in PERSONA_FILES:
        path = data_dir / name
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(parts) if parts else DEFAULT_SOUL
