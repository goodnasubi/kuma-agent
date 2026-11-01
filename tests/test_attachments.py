"""ファイル添付の直列化・UI 往復のテスト。"""
import base64
import json
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path

import httpx
import pytest

from kuma_agent.config import Config
from kuma_agent.llm import (
    AnthropicClient,
    Attachment,
    GeminiClient,
    Message,
    MetaModelClient,
    guess_media_type,
)
from kuma_agent.session import Session

PNG = Attachment("cat.png", "image/png", b"\x89PNG")
PDF = Attachment("doc.pdf", "application/pdf", b"%PDF-1.4")
TXT = Attachment("note.md", "text/markdown", "# メモ\n本文です".encode("utf-8"))
BIN = Attachment("data.bin", "application/octet-stream", b"\x00\x01")
PNG_B64 = base64.b64encode(PNG.data).decode("ascii")
PDF_B64 = base64.b64encode(PDF.data).decode("ascii")


def _capture(client_cls, response_json: dict, **client_kwargs) -> dict:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=response_json)

    client = client_cls("k", transport=httpx.MockTransport(handler), **client_kwargs)
    return seen, client


def test_guess_media_type():
    assert guess_media_type("a.png") == "image/png"
    assert guess_media_type("a.JPG") == "image/jpeg"
    assert guess_media_type("a.pdf") == "application/pdf"
    assert guess_media_type("a.md") == "text/markdown"
    assert guess_media_type("a.json") == "application/json"
    assert guess_media_type("a.unknownext") == "application/octet-stream"


# ---------------------------------------------------------------- Muse（OpenAI 互換）

_CLAUDE_OK = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def test_muse_image_becomes_data_uri():
    seen, client = _capture(MetaModelClient, _CLAUDE_OK)
    client.chat("sys", [Message("user", "この画像は？", attachments=[PNG])], [])
    content = seen["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "この画像は？"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"


def test_muse_pdf_becomes_placeholder_and_text_is_embedded():
    seen, client = _capture(MetaModelClient, _CLAUDE_OK)
    client.chat("sys", [Message("user", "読んで", attachments=[PDF, TXT])], [])
    content = seen["messages"][1]["content"]
    assert content[1] == {"type": "text", "text": "[添付: doc.pdf（application/pdf）]"}
    assert content[2]["type"] == "text"
    assert "本文です" in content[2]["text"]


# ---------------------------------------------------------------- Claude

_ANTHROPIC_OK = {"content": [{"type": "text", "text": "ok"}]}


def test_anthropic_image_and_pdf_blocks():
    seen, client = _capture(AnthropicClient, _ANTHROPIC_OK)
    client.chat("sys", [Message("user", "見て", attachments=[PNG, PDF])], [])
    blocks = seen["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "見て"}
    assert blocks[1] == {"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": PNG_B64}}
    assert blocks[2] == {"type": "document", "source": {
        "type": "base64", "media_type": "application/pdf", "data": PDF_B64}}


def test_anthropic_text_doc_embedded_and_binary_placeholder():
    seen, client = _capture(AnthropicClient, _ANTHROPIC_OK)
    client.chat("sys", [Message("user", "読んで", attachments=[TXT, BIN])], [])
    blocks = seen["messages"][0]["content"]
    assert blocks[1]["type"] == "text" and "本文です" in blocks[1]["text"]
    assert blocks[2] == {"type": "text",
                         "text": "[添付: data.bin（application/octet-stream）]"}


# ---------------------------------------------------------------- Gemini

_GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}


def test_gemini_image_and_pdf_inline_data():
    seen, client = _capture(GeminiClient, _GEMINI_OK)
    client.chat("sys", [Message("user", "見て", attachments=[PNG, PDF])], [])
    parts = seen["contents"][0]["parts"]
    assert parts[0] == {"text": "見て"}
    assert parts[1] == {"inlineData": {"mimeType": "image/png", "data": PNG_B64}}
    assert parts[2] == {"inlineData": {"mimeType": "application/pdf", "data": PDF_B64}}


def test_gemini_text_doc_embedded():
    seen, client = _capture(GeminiClient, _GEMINI_OK)
    client.chat("sys", [Message("user", "読んで", attachments=[TXT])], [])
    parts = seen["contents"][0]["parts"]
    assert parts[1]["text"].startswith("[添付文書: note.md]")
    assert "本文です" in parts[1]["text"]


# ---------------------------------------------------------------- CLI 系

def test_cli_attachment_saved_and_path_in_prompt(tmp_path: Path):
    from kuma_agent.agent_cli import ATTACHMENTS_DIR, ClaudeCodeClient

    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\n"
        "echo done\n"
        "echo \"$@\" > \"$(dirname \"$0\")/args.txt\"\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    client = ClaudeCodeClient(command=str(script), work_dir=tmp_path)
    client.chat("sys", [Message("user", "見て", attachments=[PNG])], tools=[])
    saved = tmp_path / ATTACHMENTS_DIR / "cat.png"
    assert saved.read_bytes() == PNG.data
    args = (tmp_path / "args.txt").read_text(encoding="utf-8")
    assert f"[添付ファイル: {saved}]" in args


# ---------------------------------------------------------------- Session

class _RecordingModel:
    def __init__(self) -> None:
        self.seen: list[Message] = []

    def chat(self, system_prompt, messages, tools):
        self.seen = list(messages)
        return Message("assistant", "受け取りました")


def test_session_passes_attachments_and_memory_keeps_summary_only(tmp_path: Path):
    model = _RecordingModel()
    session = Session(Config(data_dir=tmp_path), model)
    reply = session.say("これ見て", [PDF])
    assert reply == "受け取りました"
    assert model.seen[-1].attachments == [PDF]
    # 会話ログにはプレースホルダだけが残り、base64 は残らない
    logs = list((tmp_path / "conversations").glob("*.jsonl"))
    assert logs
    text = logs[0].read_text(encoding="utf-8")
    assert "[添付: doc.pdf（application/pdf）]" in text
    assert PDF_B64 not in text


# ---------------------------------------------------------------- Web

class _AttachmentEchoModel:
    def chat(self, system_prompt, messages, tools):
        names = [a.name for a in messages[-1].attachments]
        return Message("assistant", "添付: " + ",".join(names))


@pytest.fixture()
def web_server(tmp_path: Path):
    from kuma_agent.web import build_server

    session = Session(Config(data_dir=tmp_path), _AttachmentEchoModel())
    httpd = build_server(session, host="127.0.0.1", port=0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_web_chat_with_attachment_roundtrip(web_server: str):
    status, data = _post(web_server + "/api/chat", {
        "message": "見て",
        "attachments": [{"name": "cat.png", "media_type": "image/png",
                         "data_base64": PNG_B64}],
    })
    assert status == 200
    assert data == {"reply": "添付: cat.png"}
    with urllib.request.urlopen(web_server + "/api/history", timeout=10) as resp:
        history = json.loads(resp.read().decode("utf-8"))["messages"]
    assert history[0]["attachments"] == ["cat.png"]


def test_web_rejects_invalid_base64(web_server: str):
    status, _ = _post(web_server + "/api/chat", {
        "message": "x",
        "attachments": [{"name": "a.png", "data_base64": "!!not-base64!!"}],
    })
    assert status == 400


def test_web_rejects_oversized_attachment(web_server: str, monkeypatch: pytest.MonkeyPatch):
    import kuma_agent.web as web

    monkeypatch.setattr(web, "MAX_ATTACHMENT_BYTES", 3)
    status, _ = _post(web_server + "/api/chat", {
        "message": "x",
        "attachments": [{"name": "a.png", "data_base64": PNG_B64}],
    })
    assert status == 400


def test_web_page_has_file_input(web_server: str):
    with urllib.request.urlopen(web_server + "/", timeout=10) as resp:
        page = resp.read().decode("utf-8")
    assert 'type="file"' in page and "添付" in page


# ---------------------------------------------------------------- TUI

def test_parse_attach_command():
    from kuma_agent.tui import parse_attach_command

    assert parse_attach_command("/attach /tmp/a.png") == "/tmp/a.png"
    assert parse_attach_command("  /attach  b/c.pdf ") == "b/c.pdf"
    assert parse_attach_command("/attach") == ""
    assert parse_attach_command("/attachfoo") is None
    assert parse_attach_command("普通の発話") is None


def test_load_attachment_reads_file_and_guesses_type(tmp_path: Path):
    from kuma_agent.tui import load_attachment

    (tmp_path / "sub").mkdir()
    target = tmp_path / "sub" / "note.txt"
    target.write_text("hello", encoding="utf-8")
    att = load_attachment("sub/note.txt", tmp_path)
    assert att.name == "note.txt"
    assert att.media_type == "text/plain"
    assert att.data == b"hello"


def test_load_attachment_missing_and_oversized(tmp_path: Path):
    from kuma_agent.tui import load_attachment

    with pytest.raises(OSError):
        load_attachment("nothing.png", tmp_path)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 5)
    import kuma_agent.tui as tui

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tui, "MAX_ATTACHMENT_BYTES", 4)
        with pytest.raises(ValueError):
            load_attachment("big.bin", tmp_path)
