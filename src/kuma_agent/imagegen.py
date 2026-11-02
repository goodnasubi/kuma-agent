"""画像生成（generate_image 道具の実処理）。

生成そのものは Gemini API の generateContent（画像生成対応
モデル）に任せる。頭脳が Gemini でなくても、生成だけは Gemini
を使う: 画像生成の口は手元の API では Gemini にしか無く、頭脳の
選択と切り離しておくことで、どの頭脳（CLI 系を含む）からでも
同じ道具として使える。そのためキーは頭脳のキーではなく、Gemini
のキー（GEMINI_API_KEY / GOOGLE_API_KEY）を環境変数から引く。

通信の作りは既存の Gemini クライアント（llm.GeminiClient）と
そろえる: 同じベース URL・同じキー用ヘッダー（x-goog-api-key）・
httpx で、transport はテスト用に差し替えられる。失敗は
ImageGenerationError で表し、道具側で error: の結果に変える
（会話は壊さない）。
"""
from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from .llm import GeminiClient, _http

# GeminiClient と同じ宛先・流儀で送る（新しい通信経路は作らない）
BASE_URL = GeminiClient.BASE_URL
DEFAULT_IMAGE_MODEL = "gemini-3.8-flash-image"
IMAGE_MODEL_ENV = "KUMA_IMAGE_MODEL"
API_KEY_ENVS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
# エラー文に添えるモデル応答の最大文字数
MAX_MODEL_TEXT_CHARS = 200


class ImageGenerationError(Exception):
    """画像生成の失敗。道具の結果（error:）としてモデルに返す。"""


@dataclass
class GeneratedImage:
    """生成された画像。text はモデルが添えたテキスト応答（あれば）。"""
    data: bytes
    mime_type: str
    text: str = ""


def resolve_api_key() -> str:
    """Gemini のキーを環境変数から引く（GEMINI_API_KEY 優先）。"""
    for name in API_KEY_ENVS:
        if os.environ.get(name):
            return os.environ[name]
    return ""


def resolve_model() -> str:
    """画像生成に使うモデル名（KUMA_IMAGE_MODEL で差し替え可能）。"""
    return os.environ.get(IMAGE_MODEL_ENV) or DEFAULT_IMAGE_MODEL


def generate_image(prompt: str, *, api_key: str | None = None,
                   model: str | None = None, transport=None) -> GeneratedImage:
    """プロンプトから画像を 1 枚生成して返す。

    api_key / model を省略すると環境変数から引く。キーが無い・
    通信に失敗した・応答に画像が含まれない場合は
    ImageGenerationError を投げる。
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ImageGenerationError("作りたい画像の説明が空です")
    key = resolve_api_key() if api_key is None else api_key
    if not key:
        raise ImageGenerationError(
            "画像生成に必要な設定がありません"
            "（GEMINI_API_KEY が未設定です）")
    use_model = model or resolve_model()
    client = _http(BASE_URL, {"x-goog-api-key": key}, transport)
    try:
        resp = client.post(
            f"/v1beta/models/{use_model}:generateContent",
            json={"contents": [{"parts": [{"text": prompt}]}]})
        resp.raise_for_status()
    except Exception as e:
        raise ImageGenerationError(
            f"画像の生成に失敗しました（{e}）") from e
    finally:
        client.close()
    try:
        body = resp.json()
    except Exception as e:
        raise ImageGenerationError(
            "画像生成の応答を読み取れませんでした") from e
    return _parse_response(body)


def _parse_response(body: object) -> GeneratedImage:
    """generateContent の応答から画像を取り出す。

    応答の candidates[0].content.parts に、テキスト（text）と
    画像（inlineData の base64）が混ざって返る。画像が無ければ、
    モデルのテキスト応答があれば短く添えてエラーにする。
    """
    parts: list = []
    if isinstance(body, dict):
        candidates = body.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            content = candidates[0].get("content") or {}
            if isinstance(content, dict):
                parts = content.get("parts") or []
    texts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        inline = part.get("inlineData") or part.get("inline_data") or {}
        if not isinstance(inline, dict):
            inline = {}
        data_text = inline.get("data")
        if isinstance(data_text, str) and data_text:
            try:
                data = base64.b64decode(data_text)
            except (binascii.Error, ValueError):
                data = b""
            if data:
                mime = (inline.get("mimeType") or inline.get("mime_type")
                        or "image/png")
                return GeneratedImage(
                    data=data, mime_type=str(mime),
                    text="".join(texts).strip())
        if part.get("text"):
            texts.append(str(part["text"]))
    detail = "".join(texts).strip()
    if detail:
        if len(detail) > MAX_MODEL_TEXT_CHARS:
            detail = detail[:MAX_MODEL_TEXT_CHARS] + "…"
        raise ImageGenerationError(
            f"画像が返ってきませんでした（モデルの応答: {detail}）")
    raise ImageGenerationError("画像が返ってきませんでした")
