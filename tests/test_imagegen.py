"""画像生成（generate_image 道具）のテスト。

Gemini API への通信は httpx.MockTransport で差し替えて、
ネットワークには出ない。生成は頭脳の選択と切り離されていて、
Gemini のキー（GEMINI_API_KEY / GOOGLE_API_KEY）があれば動く。
生成した画像は成果物（OutputBoard）に登録されて渡る。
"""
import base64
import json
from pathlib import Path

import httpx
import pytest

from kuma_agent import imagegen
from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.outputs import OutputBoard
from kuma_agent.session import Session
from kuma_agent.tools import ToolBox, activity_label

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-data"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake-jpeg-data"


def _image_body(data: bytes = PNG_BYTES, mime: str = "image/png",
                text: str = "") -> dict:
    parts = []
    if text:
        parts.append({"text": text})
    parts.append({"inlineData": {
        "mimeType": mime, "data": base64.b64encode(data).decode("ascii")}})
    return {"candidates": [{"content": {"parts": parts}}]}


def _transport(body: dict, seen: dict | None = None,
               status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["url"] = str(request.url)
            seen["api_key"] = request.headers.get("x-goog-api-key")
            seen["payload"] = json.loads(request.content)
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "ok")


# ---------------------------------------------------------------- 生成の実処理

def test_generate_image_returns_image_and_sends_prompt():
    seen: dict = {}
    generated = imagegen.generate_image(
        "森の中のクマ", api_key="test-key",
        transport=_transport(_image_body(), seen))
    assert generated.data == PNG_BYTES
    assert generated.mime_type == "image/png"
    # リクエストは GeminiClient と同じ形（宛先・キー・モデル）
    assert seen["url"].endswith(
        "/v1beta/models/gemini-3.8-flash-image:generateContent")
    assert seen["api_key"] == "test-key"
    assert seen["payload"] == {
        "contents": [{"parts": [{"text": "森の中のクマ"}]}]}


def test_generate_image_keeps_model_text():
    generated = imagegen.generate_image(
        "クマ", api_key="test-key",
        transport=_transport(_image_body(text="できました")))
    assert generated.text == "できました"


def test_generate_image_uses_env_key_and_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setenv("KUMA_IMAGE_MODEL", "custom-image-model")
    seen: dict = {}
    imagegen.generate_image("クマ", transport=_transport(_image_body(), seen))
    assert seen["api_key"] == "google-key"
    assert "/models/custom-image-model:" in seen["url"]


def test_generate_image_prefers_gemini_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    seen: dict = {}
    imagegen.generate_image("クマ", transport=_transport(_image_body(), seen))
    assert seen["api_key"] == "gemini-key"


def test_generate_image_without_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(imagegen.ImageGenerationError, match="設定がありません"):
        imagegen.generate_image("クマ")


def test_generate_image_empty_prompt():
    with pytest.raises(imagegen.ImageGenerationError, match="空"):
        imagegen.generate_image("  ", api_key="test-key",
                                transport=_transport(_image_body()))


def test_generate_image_no_image_in_response():
    body = {"candidates": [{"content": {"parts": [
        {"text": "その画像は描けません"}]}}]}
    with pytest.raises(imagegen.ImageGenerationError) as excinfo:
        imagegen.generate_image("クマ", api_key="test-key",
                                transport=_transport(body))
    # モデルのテキスト応答が短く添えられる
    assert "その画像は描けません" in str(excinfo.value)


def test_generate_image_no_candidates():
    with pytest.raises(imagegen.ImageGenerationError,
                       match="画像が返ってきませんでした"):
        imagegen.generate_image("クマ", api_key="test-key",
                                transport=_transport({}))


def test_generate_image_http_error():
    with pytest.raises(imagegen.ImageGenerationError,
                       match="画像の生成に失敗しました"):
        imagegen.generate_image("クマ", api_key="test-key",
                                transport=_transport({}, status=500))


def test_generate_image_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(imagegen.ImageGenerationError,
                       match="画像の生成に失敗しました"):
        imagegen.generate_image(
            "クマ", api_key="test-key",
            transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- 道具

def _toolbox(tmp_path: Path) -> ToolBox:
    return ToolBox(tmp_path, output_board=OutputBoard(tmp_path / "user"))


def _stub_generation(monkeypatch: pytest.MonkeyPatch,
                     generated: imagegen.GeneratedImage) -> dict:
    seen: dict = {}

    def fake(prompt: str) -> imagegen.GeneratedImage:
        seen["prompt"] = prompt
        return generated

    monkeypatch.setattr(imagegen, "generate_image", fake)
    return seen


def test_tool_registered_with_output_board(tmp_path: Path):
    names = {s["name"] for s in _toolbox(tmp_path).schemas()}
    assert "generate_image" in names
    plain_names = {s["name"] for s in ToolBox(tmp_path).schemas()}
    assert "generate_image" not in plain_names
    assert activity_label("generate_image") == "画像を作っています"


def test_tool_generates_and_registers_output(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen = _stub_generation(
        monkeypatch, imagegen.GeneratedImage(PNG_BYTES, "image/png"))
    board = OutputBoard(tmp_path / "user")
    result = ToolBox(tmp_path, output_board=board).call(
        "generate_image", {"prompt": "森の中のクマ", "title": "森のクマ"})
    assert seen["prompt"] == "森の中のクマ"
    outputs = board.list()
    assert len(outputs) == 1
    assert outputs[0].title == "森のクマ"
    assert outputs[0].filename.endswith(".png")
    # 戻り値にファイル名と成果物パネルへの案内が含まれる
    assert outputs[0].filename in result
    assert "成果物" in result
    # 実ファイルの中身が生成された画像と一致する
    saved = board.outputs_dir / outputs[0].filename
    assert saved.read_bytes() == PNG_BYTES


def test_tool_extension_follows_mime(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_generation(
        monkeypatch, imagegen.GeneratedImage(JPEG_BYTES, "image/jpeg"))
    board = OutputBoard(tmp_path / "user")
    ToolBox(tmp_path, output_board=board).call(
        "generate_image", {"prompt": "クマの写真"})
    assert board.list()[0].filename.endswith(".jpg")


def test_tool_unknown_mime_falls_back_to_png(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _stub_generation(
        monkeypatch, imagegen.GeneratedImage(b"data", "image/x-new"))
    board = OutputBoard(tmp_path / "user")
    ToolBox(tmp_path, output_board=board).call(
        "generate_image", {"prompt": "クマ"})
    assert board.list()[0].filename.endswith(".png")


def test_tool_without_key_adds_no_output(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # キーが無ければ生成リクエスト自体が出ず、道具のエラーになる
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    board = OutputBoard(tmp_path / "user")
    result = ToolBox(tmp_path, output_board=board).call(
        "generate_image", {"prompt": "クマ"})
    assert result.startswith("error:")
    assert "設定がありません" in result
    assert board.list() == []


def test_tool_no_image_adds_no_output(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail(prompt: str) -> imagegen.GeneratedImage:
        raise imagegen.ImageGenerationError("画像が返ってきませんでした")

    monkeypatch.setattr(imagegen, "generate_image", fail)
    board = OutputBoard(tmp_path / "user")
    result = ToolBox(tmp_path, output_board=board).call(
        "generate_image", {"prompt": "クマ"})
    assert result.startswith("error:")
    assert board.list() == []


def test_tool_empty_prompt(tmp_path: Path):
    board = OutputBoard(tmp_path / "user")
    result = ToolBox(tmp_path, output_board=board).call(
        "generate_image", {"prompt": " "})
    assert result.startswith("error:")
    assert board.list() == []


# ---------------------------------------------------------------- 両モードでの登録

@pytest.mark.parametrize("mode", ["ledger", "wall"])
def test_tool_registered_in_both_modes(tmp_path: Path, mode: str):
    session = Session(Config(data_dir=tmp_path, mode=mode), _EchoModel())
    names = {s["name"] for s in session.tools.schemas()}
    assert "generate_image" in names
