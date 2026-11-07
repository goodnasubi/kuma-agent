"""LLM プロバイダのクライアント群。

内部の Message 表現は共通で、プロバイダごとに API 形式へ変換する。

- muse:   Meta Model API（OpenAI 互換 Chat Completions、developer ロール）
- claude: Anthropic Messages API（system 分離、tool_use / tool_result）
- gemini: Google Gemini API（generateContent、functionCall / functionResponse）
"""
from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

MAX_TOKENS = 8192

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 添付 1 件の上限（目安 10MB）
MAX_QUOTE_CHARS = 200  # モデルへ渡す引用文の最大文字数

IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
PDF_MEDIA_TYPE = "application/pdf"
# 音声のメディア種別。対応形式は mp3 / wav / m4a / ogg / webm で、
# ブラウザや OS によって揺れる表記（audio/x-wav 等）も含める。
# video/webm はブラウザの録音（MediaRecorder）が音声だけでも
# この種別で出すことがあるため、音声として扱う。
AUDIO_MEDIA_TYPES = frozenset({
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/wave",
    "audio/mp4", "audio/x-m4a", "audio/aac", "audio/ogg", "audio/webm",
    "audio/flac", "audio/aiff", "video/webm",
})
_TEXT_MEDIA_TYPES = frozenset({"application/json"})
_EXTENSION_MEDIA_TYPES = {".md": "text/markdown"}
# mimetypes の推定より優先する拡張子ごとの種別。音声の webm は
# video/webm と推定されてしまうため、音声（audio/webm）に寄せる。
# wav / m4a も頭脳（Gemini）が受け付ける表記にそろえる。
_EXTENSION_OVERRIDES = {
    ".md": "text/markdown",
    ".webm": "audio/webm",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}


def guess_media_type(name: str) -> str:
    """ファイル名の拡張子からメディア種別を推定する。"""
    override = _EXTENSION_OVERRIDES.get(Path(name).suffix.lower())
    if override:
        return override
    media_type, _ = mimetypes.guess_type(name)
    if media_type:
        return media_type
    return _EXTENSION_MEDIA_TYPES.get(Path(name).suffix.lower(), "application/octet-stream")


@dataclass
class Attachment:
    """発話に添えるファイル（画像・文書・音声）。バイト列そのものを持つ。"""

    name: str
    media_type: str
    data: bytes
    # 音声の文字起こし（transcribe で Gemini が書き起こした文章）。
    # 音声をそのまま聞けない頭脳へ内容を渡すために Session が
    # 発話の合流点で用意する。未実施・失敗時は None
    transcript: str | None = None

    @property
    def is_image(self) -> bool:
        return self.media_type in IMAGE_MEDIA_TYPES

    @property
    def is_pdf(self) -> bool:
        return self.media_type == PDF_MEDIA_TYPE

    @property
    def is_audio(self) -> bool:
        """音声ファイル（mp3 / wav / m4a / ogg / webm 等）かどうか。"""
        return (self.media_type.startswith("audio/")
                or self.media_type in AUDIO_MEDIA_TYPES)

    @property
    def is_text(self) -> bool:
        """テキストとして本文をそのまま渡せる文書かどうか。"""
        return self.media_type.startswith("text/") or self.media_type in _TEXT_MEDIA_TYPES

    def text_content(self) -> str:
        return self.data.decode("utf-8", errors="replace")

    def to_base64(self) -> str:
        return base64.b64encode(self.data).decode("ascii")

    def placeholder(self) -> str:
        """添付をそのまま渡せない場合にモデルへ存在を知らせる文。

        音声は利用者にも記録で分かりやすいよう「音声」と明示する。
        """
        if self.is_audio:
            return f"[添付: 音声 {self.name}（{self.media_type}）]"
        return f"[添付: {self.name}（{self.media_type}）]"

    def embedded_text(self) -> str:
        """テキスト文書を本文として埋め込むときの表現。"""
        return f"[添付文書: {self.name}]\n{self.text_content()}"

    def model_note(self) -> str:
        """添付をそのまま渡せない場合にモデルへ渡す文。

        プレースホルダに、音声の文字起こしがあればその文章を
        添える（モデルが音声の内容として読める形）。会話ログの
        要約にもこの形を使い、文字起こしが記録に残るようにする。
        """
        note = self.placeholder()
        if self.is_audio and self.transcript:
            return f"{note}\n（文字起こし）{self.transcript}"
        return note


def fallback_text(attachment: Attachment) -> str:
    """プロバイダに直接渡せない添付の共通の落とし先。

    テキスト文書は本文を埋め込み、それ以外はプレースホルダ文に
    する（音声は文字起こしがあれば文章も添える）。
    """
    if attachment.is_text:
        return attachment.embedded_text()
    return attachment.model_note()


@dataclass
class Usage:
    """1 回のモデル呼び出しの使用量（トークン数）を正規化した形。

    頭脳ごとに API の応答に含まれる使用量の形式が違うため、各
    クライアントがこの形にそろえて Message.usage に載せる。応答に
    無い・読み取れない項目は None のままにする（会話記録とは別に、
    usage.py が使用量の記録へ書き出すための情報）。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


def _usage_int(value: object) -> int | None:
    """使用量の値を整数として防御的に読む。数値でなければ None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def normalize_usage(raw: object, input_key: str, output_key: str,
                    total_key: str) -> Usage | None:
    """頭脳の応答に含まれる使用量の辞書を Usage に正規化する。

    raw が辞書でない・項目が 1 つも読み取れない場合は None（使用量
    不明）を返す。合計だけが無く入力と出力がそろっている場合は、
    合計をその和で補う（Claude の応答は合計を返さないため）。
    想定外の形式でも例外は出さない（会話を壊さないことが優先）。
    """
    if not isinstance(raw, dict):
        return None
    usage = Usage(
        input_tokens=_usage_int(raw.get(input_key)),
        output_tokens=_usage_int(raw.get(output_key)),
        total_tokens=_usage_int(raw.get(total_key)),
    )
    if (usage.total_tokens is None and usage.input_tokens is not None
            and usage.output_tokens is not None):
        usage.total_tokens = usage.input_tokens + usage.output_tokens
    if (usage.input_tokens is None and usage.output_tokens is None
            and usage.total_tokens is None):
        return None
    return usage


@dataclass
class Message:
    role: str  # "user" | "assistant" | "tool"
    content: str
    tool_calls: list[dict] = field(default_factory=list)  # {"id", "name", "args"}
    tool_call_id: str | None = None  # role == "tool" のとき対応する呼び出しの id
    tool_name: str | None = None  # role == "tool" のとき道具名（Gemini は名前で対応付ける）
    attachments: list[Attachment] = field(default_factory=list)  # 利用者の添付ファイル
    quote: str | None = None  # この発話が返信する過去の発話の本文
    usage: Usage | None = None  # 応答した頭脳が返した使用量（assistant のみ、無ければ None）
    record_index: int | None = None  # 会話記録（JSONL）での位置。記録された発話だけに付く（リアクションの宛先）


def truncate_quote(quote: str, limit: int = MAX_QUOTE_CHARS) -> str:
    """引用文をモデルへ渡しやすい長さに整える。

    改行は空白に畳み、limit を超えたら末尾を省略する。履歴や記録に
    残す引用そのものは切り詰めず、モデル入力を作るときだけ使う。
    """
    text = " ".join(quote.split())
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 1)] + "…"


def quote_block(quote: str) -> str:
    """引用元の発話を示す短い見出し。"""
    return f"（「{truncate_quote(quote)}」への返信）"


def quoted_content(msg: Message) -> str:
    """発話本文の前に引用ブロックを付けた、モデル入力用の本文。"""
    if not msg.quote:
        return msg.content
    block = quote_block(msg.quote)
    return f"{block}\n{msg.content}" if msg.content else block


class ChatModel(Protocol):
    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        ...


def _http(base_url: str, headers: dict, transport, timeout: float = 300.0) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers=headers, timeout=timeout, transport=transport)


# ---------------------------------------------------------------- Muse

class MetaModelClient:
    """Meta Model API（Muse）。OpenAI 互換。"""

    BASE_URL = "https://api.meta.ai/v1"
    DEFAULT_MODEL = "muse-wonder-1.0"

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None, transport=None) -> None:
        self.model = model or self.DEFAULT_MODEL
        self._client = _http(base_url or self.BASE_URL,
                             {"Authorization": f"Bearer {api_key}"}, transport)

    @staticmethod
    def _content(msg: Message) -> str | list[dict]:
        """添付があれば content をパート配列にする。

        画像は image_url の data URI で渡せる。PDF と音声は渡せ
        ないのでプレースホルダ文に落とし、テキスト文書は本文を
        埋め込む。
        """
        if not msg.attachments:
            return quoted_content(msg)
        parts: list[dict] = []
        text = quoted_content(msg)
        if text:
            parts.append({"type": "text", "text": text})
        for att in msg.attachments:
            if att.is_image:
                parts.append({"type": "image_url", "image_url": {
                    "url": f"data:{att.media_type};base64,{att.to_base64()}"}})
            else:
                parts.append({"type": "text", "text": fallback_text(att)})
        return parts

    @staticmethod
    def _to_api(msg: Message) -> dict:
        if msg.role == "tool":
            return {"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.content}
        if msg.role == "assistant" and msg.tool_calls:
            return {
                "role": "assistant",
                "content": quoted_content(msg) or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"],
                                  "arguments": json.dumps(c.get("args", {}))}}
                    for c in msg.tool_calls
                ],
            }
        return {"role": msg.role, "content": MetaModelClient._content(msg)}

    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "developer", "content": system_prompt}]
            + [self._to_api(m) for m in messages],
        }
        if tools:
            payload["tools"] = [
                {"type": "function", "function": {
                    "name": t["name"], "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}})}}
                for t in tools
            ]
        resp = self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        body = resp.json()
        data = body["choices"][0]["message"]
        calls = []
        for c in data.get("tool_calls") or []:
            fn = c.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            calls.append({"id": c.get("id", ""), "name": fn.get("name", ""), "args": args})
        usage = normalize_usage(body.get("usage"), "prompt_tokens",
                                "completion_tokens", "total_tokens")
        return Message("assistant", data.get("content") or "",
                       tool_calls=calls, usage=usage)


# ---------------------------------------------------------------- Claude

class AnthropicClient:
    """Anthropic Messages API（Claude）。"""

    BASE_URL = "https://api.anthropic.com"
    DEFAULT_MODEL = "claude-sonnet-5-5"
    VERSION = "2023-06-01"

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None, transport=None) -> None:
        self.model = model or self.DEFAULT_MODEL
        self._client = _http(base_url or self.BASE_URL,
                             {"x-api-key": api_key, "anthropic-version": self.VERSION},
                             transport)

    @staticmethod
    def _content(msg: Message) -> str | list[dict]:
        """添付があれば content をブロック配列にする。

        画像は image ブロック、PDF は document ブロック（いずれも
        base64）で渡せる。音声は直接渡せないのでプレースホルダ文
        に落とし、テキスト文書は本文を埋め込み、それ以外も
        プレースホルダ文に落とす。
        """
        if not msg.attachments:
            return quoted_content(msg)
        blocks: list[dict] = []
        text = quoted_content(msg)
        if text:
            blocks.append({"type": "text", "text": text})
        for att in msg.attachments:
            if att.is_image:
                blocks.append({"type": "image", "source": {
                    "type": "base64", "media_type": att.media_type,
                    "data": att.to_base64()}})
            elif att.is_pdf:
                blocks.append({"type": "document", "source": {
                    "type": "base64", "media_type": PDF_MEDIA_TYPE,
                    "data": att.to_base64()}})
            else:
                blocks.append({"type": "text", "text": fallback_text(att)})
        return blocks

    @staticmethod
    def _to_api(messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        pending_tools: list[dict] = []  # 連続する道具結果は 1 つの user 発話にまとめる

        def flush_tools() -> None:
            if pending_tools:
                out.append({"role": "user", "content": list(pending_tools)})
                pending_tools.clear()

        for msg in messages:
            if msg.role == "tool":
                pending_tools.append({
                    "type": "tool_result", "tool_use_id": msg.tool_call_id,
                    "content": msg.content,
                })
                continue
            flush_tools()
            if msg.role == "assistant" and msg.tool_calls:
                blocks: list[dict] = []
                text = quoted_content(msg)
                if text:
                    blocks.append({"type": "text", "text": text})
                blocks += [
                    {"type": "tool_use", "id": c["id"], "name": c["name"],
                     "input": c.get("args", {})}
                    for c in msg.tool_calls
                ]
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": msg.role, "content": AnthropicClient._content(msg)})
        flush_tools()
        return out

    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        payload: dict = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system_prompt,
            "messages": self._to_api(messages),
        }
        if tools:
            payload["tools"] = [
                {"name": t["name"], "description": t.get("description", ""),
                 "input_schema": t.get("parameters", {"type": "object", "properties": {}})}
                for t in tools
            ]
        resp = self._client.post("/v1/messages", json=payload)
        resp.raise_for_status()
        body = resp.json()
        text, calls = "", []
        for block in body.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
            elif block.get("type") == "tool_use":
                calls.append({"id": block.get("id", ""), "name": block.get("name", ""),
                              "args": block.get("input", {})})
        usage = normalize_usage(body.get("usage"), "input_tokens",
                                "output_tokens", "total_tokens")
        return Message("assistant", text, tool_calls=calls, usage=usage)


# ---------------------------------------------------------------- Gemini

class GeminiClient:
    """Google Gemini API（generateContent）。道具は名前で対応付ける。"""

    BASE_URL = "https://generativelanguage.googleapis.com"
    DEFAULT_MODEL = "gemini-3.8-flash"

    def __init__(self, api_key: str, model: str | None = None,
                 base_url: str | None = None, transport=None) -> None:
        self.model = model or self.DEFAULT_MODEL
        self._client = _http(base_url or self.BASE_URL,
                             {"x-goog-api-key": api_key}, transport)

    @staticmethod
    def _to_api(messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        pending: list[dict] = []  # 連続する道具応答は 1 つの user 発話にまとめる

        def flush() -> None:
            if pending:
                out.append({"role": "user", "parts": list(pending)})
                pending.clear()

        for msg in messages:
            if msg.role == "tool":
                pending.append({"functionResponse": {
                    "name": msg.tool_name or "",
                    "response": {"result": msg.content},
                }})
                continue
            flush()
            if msg.role == "assistant":
                parts: list[dict] = []
                text = quoted_content(msg)
                if text:
                    parts.append({"text": text})
                parts += [{"functionCall": {"name": c["name"], "args": c.get("args", {})}}
                          for c in msg.tool_calls]
                out.append({"role": "model", "parts": parts})
            else:
                parts = []
                text = quoted_content(msg)
                if text:
                    parts.append({"text": text})
                for att in msg.attachments:
                    # 画像・PDF・音声は inlineData で渡せる。テキスト
                    # 文書は本文を埋め込み、それ以外はプレースホルダ
                    # 文に落とす
                    if att.is_image or att.is_pdf or att.is_audio:
                        parts.append({"inlineData": {
                            "mimeType": att.media_type, "data": att.to_base64()}})
                    else:
                        parts.append({"text": fallback_text(att)})
                out.append({"role": "user", "parts": parts})
        flush()
        return out

    def chat(self, system_prompt: str, messages: list[Message], tools: list[dict]) -> Message:
        payload: dict = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": self._to_api(messages),
        }
        if tools:
            payload["tools"] = [{"functionDeclarations": [
                {"name": t["name"], "description": t.get("description", ""),
                 "parameters": t.get("parameters", {"type": "object", "properties": {}})}
                for t in tools
            ]}]
        resp = self._client.post(f"/v1beta/models/{self.model}:generateContent", json=payload)
        resp.raise_for_status()
        body = resp.json()
        text, calls = "", []
        parts = body["candidates"][0]["content"].get("parts", [])
        for i, part in enumerate(parts):
            if "text" in part:
                text += part["text"]
            elif "functionCall" in part:
                fc = part["functionCall"]
                calls.append({"id": f"gemini-{i}", "name": fc.get("name", ""),
                              "args": fc.get("args", {})})
        usage = normalize_usage(body.get("usageMetadata"), "promptTokenCount",
                                "candidatesTokenCount", "totalTokenCount")
        return Message("assistant", text, tool_calls=calls, usage=usage)


# ---------------------------------------------------------------- factory

API_PROVIDERS = ("muse", "claude", "gemini")

_API_CLIENTS = {
    "muse": MetaModelClient,
    "claude": AnthropicClient,
    "gemini": GeminiClient,
}


def create_client(provider: str, api_key: str = "", model: str | None = None, **kwargs) -> ChatModel:
    """provider 名からバックエンドを作る。

    API 系（muse / claude / gemini）は api_key を使い、CLI 系
    （claude-code / gemini-cli）は外部 CLI を実行する（キー不要）。
    """
    from .agent_cli import CLI_PROVIDERS, create_cli_client

    if provider in CLI_PROVIDERS:
        return create_cli_client(provider, model=model, **kwargs)
    cls = _API_CLIENTS.get(provider)
    if cls is None:
        raise ValueError(f"未知のプロバイダ: {provider}")
    return cls(api_key, model=model, **kwargs)


# 頭脳の候補一覧（API 系＋CLI 系）。CLI 系の正は agent_cli.py の
# CLI_PROVIDERS で、こちらは循環 import を避けるため写しを持つ
# （brain.py の ALL_PROVIDERS は両モジュールから組み立てる正の
# 定義）。以前は codex が漏れていたため、定義し直した
PROVIDERS = API_PROVIDERS + ("claude-code", "gemini-cli", "codex")


def default_model_name(provider: str) -> str | None:
    """プロバイダの既定モデル名を返す（API 系のみ）。

    モデル名が未指定のときに各クライアントが使う DEFAULT_MODEL。
    CLI 系はクライアント側に既定モデル名を持たないため None。
    """
    cls = _API_CLIENTS.get(provider)
    return cls.DEFAULT_MODEL if cls is not None else None


def model_name_of(model: ChatModel) -> str:
    """クライアントから実効モデル名を引く（使用量の記録用）。

    クライアントは実効モデル名を model 属性に持ち、未指定の場合は
    クラス既定（DEFAULT_MODEL）を使う作りなので、その順に引く。
    どちらも無い頭脳（テスト用のスタブ等）は空文字列にする。
    Session の使用量記録（session._model_name）と同じ引き方を、
    Session を経由しない裏側の呼び出し（自動抽出・自動学習）でも
    使えるよう、ここに正を置く。
    """
    name = getattr(model, "model", None) or getattr(model, "DEFAULT_MODEL", None)
    return str(name) if name else ""
