"""音声の文字起こし（transcribe_audio）。

頭脳が Gemini でない場合に、音声添付の内容を文章として頭脳へ
渡せるよう、Gemini API で書き起こす。画像生成（imagegen）と
同じ流儀で、文字起こしそのものは頭脳の選択と切り離して Gemini
が担う: キーは頭脳のキーではなく、Gemini のキー
（GEMINI_API_KEY / GOOGLE_API_KEY）を環境変数から引く。

通信の作りも Gemini クライアント（llm.GeminiClient）とそろえる:
同じベース URL・同じキー用ヘッダー（x-goog-api-key）・httpx で、
transport はテスト用に差し替えられる。失敗は
TranscriptionError で表し、呼び出し側（Session）は従来の扱い
（注記・ファイルだけ）に静かに戻す（会話は壊さない）。
"""
from __future__ import annotations

import os

from .imagegen import resolve_api_key
from .llm import Attachment, GeminiClient, _http

# GeminiClient と同じ宛先・流儀で送る（新しい通信経路は作らない）
BASE_URL = GeminiClient.BASE_URL
TRANSCRIBE_MODEL_ENV = "KUMA_TRANSCRIBE_MODEL"
TRANSCRIBE_ENV = "KUMA_TRANSCRIBE"
# モデルへ渡す書き起こしの指示
INSTRUCTION = "この音声の内容をそのまま書き起こしてください"


class TranscriptionError(Exception):
    """文字起こしの失敗。呼び出し側は従来の扱いに戻す。"""


def transcription_enabled() -> bool:
    """文字起こしが有効かどうか（KUMA_TRANSCRIBE=0 で無効化）。"""
    return os.environ.get(TRANSCRIBE_ENV, "") != "0"


def resolve_model() -> str:
    """文字起こしに使うモデル名（KUMA_TRANSCRIBE_MODEL で差し替え可能）。

    既定は Gemini の会話用既定モデル（画像生成用の
    KUMA_IMAGE_MODEL とは別）。
    """
    return os.environ.get(TRANSCRIBE_MODEL_ENV) or GeminiClient.DEFAULT_MODEL


def available() -> bool:
    """文字起こしを試みられる状態かどうか。

    無効化されていないことと、Gemini のキーが引けることの両方が
    要る。キーが無い場合は呼び出し自体を試みず、従来の扱いに
    なる（Session が合流点で確認する）。
    """
    return transcription_enabled() and bool(resolve_api_key())


def transcribe_audio(attachment: Attachment, *, api_key: str | None = None,
                     model: str | None = None, transport=None) -> str:
    """音声添付を Gemini で書き起こし、文章を返す。

    api_key / model を省略すると環境変数から引く。キーが無い・
    通信に失敗した・応答に文章が含まれない場合は
    TranscriptionError を投げる。
    """
    key = resolve_api_key() if api_key is None else api_key
    if not key:
        raise TranscriptionError(
            "文字起こしに必要な設定がありません"
            "（GEMINI_API_KEY が未設定です）")
    use_model = model or resolve_model()
    client = _http(BASE_URL, {"x-goog-api-key": key}, transport)
    try:
        resp = client.post(
            f"/v1beta/models/{use_model}:generateContent",
            json={"contents": [{"parts": [
                {"text": INSTRUCTION},
                {"inlineData": {"mimeType": attachment.media_type,
                                "data": attachment.to_base64()}},
            ]}]})
        resp.raise_for_status()
    except Exception as e:
        raise TranscriptionError(
            f"文字起こしに失敗しました（{e}）") from e
    finally:
        client.close()
    try:
        body = resp.json()
    except Exception as e:
        raise TranscriptionError(
            "文字起こしの応答を読み取れませんでした") from e
    return _parse_response(body)


def _parse_response(body: object) -> str:
    """generateContent の応答から書き起こしの文章を取り出す。

    応答の candidates[0].content.parts の text をつなげる。
    文章が無ければエラーにする。
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
        if isinstance(part, dict) and part.get("text"):
            texts.append(str(part["text"]))
    text = "".join(texts).strip()
    if not text:
        raise TranscriptionError("文字起こしが返ってきませんでした")
    return text
