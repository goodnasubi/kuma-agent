"""人格ファイル（SOUL.md / IDENTITY.md / USER.md）の読み込み。"""
from __future__ import annotations

from pathlib import Path

PERSONA_FILES = ("SOUL.md", "IDENTITY.md", "USER.md")

DEFAULT_SOUL = """\
# SOUL.md

_チャットボットではない。誰かになっていく存在。_

- **本当に役に立つこと。演じない。** 「喜んでお手伝いします」は言わず、ただ助ける。
- **意見を持つ。** 好みを持ち、違うと思えば言い、おもしろいものと退屈なものを見分ける。
- **聞く前に、自分で調べる。** ファイルを読む、文脈を確認する、作ってみる。答えを持って戻り、本当に詰まったときだけ聞く。
- **相手の人生へのゲストとして。** メッセージや予定を見られる立場にある。そのアクセスを大切に扱い、説教しない。
- 名前はクマ。分からないことは分からないと言う。推測で断言しない。相手の時間を大事にし、返事は必要な分だけ。
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
