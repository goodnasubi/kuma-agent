"""音声添付（受理・形式判定・頭脳ごとの渡し方）と、Web UI の
録音・読み上げ（ブラウザ機能の要素確認）のテスト。"""
import base64
import json
import stat
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

MP3 = Attachment("voice.mp3", "audio/mpeg", b"ID3-fake")
WAV = Attachment("voice.wav", "audio/wav", b"RIFF-fake")
WEBM = Attachment("recording.webm", "audio/webm", b"webm-fake")
MP3_B64 = base64.b64encode(MP3.data).decode("ascii")


def _capture(client_cls, response_json: dict) -> tuple[dict, object]:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json=response_json)

    client = client_cls("k", transport=httpx.MockTransport(handler))
    return seen, client


# ---------------------------------------------------------------- 形式判定

def test_guess_media_type_audio_extensions():
    assert guess_media_type("a.mp3") == "audio/mpeg"
    assert guess_media_type("a.wav") == "audio/wav"
    assert guess_media_type("a.m4a") == "audio/mp4"
    assert guess_media_type("a.ogg") == "audio/ogg"
    assert guess_media_type("a.webm") == "audio/webm"
    assert guess_media_type("A.MP3") == "audio/mpeg"


def test_is_audio():
    assert MP3.is_audio and WAV.is_audio and WEBM.is_audio
    # ブラウザの録音が video/webm で来ても音声として扱う
    assert Attachment("r.webm", "video/webm", b"x").is_audio
    assert not Attachment("a.png", "image/png", b"x").is_audio
    assert not Attachment("a.pdf", "application/pdf", b"x").is_audio


def test_audio_placeholder_mentions_voice():
    assert MP3.placeholder() == "[添付: 音声 voice.mp3（audio/mpeg）]"


# ---------------------------------------------------------------- Gemini（データをそのまま）

_GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}


def test_gemini_audio_becomes_inline_data():
    seen, client = _capture(GeminiClient, _GEMINI_OK)
    client.chat("sys", [Message("user", "聞いて", attachments=[MP3])], [])
    parts = seen["contents"][0]["parts"]
    assert parts[0] == {"text": "聞いて"}
    assert parts[1] == {"inlineData": {"mimeType": "audio/mpeg", "data": MP3_B64}}


# ---------------------------------------------------------------- Claude / Muse（注記だけ）

_ANTHROPIC_OK = {"content": [{"type": "text", "text": "ok"}]}
_MUSE_OK = {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}


def test_anthropic_audio_becomes_note_only():
    seen, client = _capture(AnthropicClient, _ANTHROPIC_OK)
    client.chat("sys", [Message("user", "聞いて", attachments=[MP3])], [])
    blocks = seen["messages"][0]["content"]
    assert blocks[1] == {"type": "text",
                         "text": "[添付: 音声 voice.mp3（audio/mpeg）]"}
    assert MP3_B64 not in json.dumps(seen)


def test_muse_audio_becomes_note_only():
    seen, client = _capture(MetaModelClient, _MUSE_OK)
    client.chat("sys", [Message("user", "聞いて", attachments=[MP3])], [])
    content = seen["messages"][1]["content"]
    assert content[1] == {"type": "text",
                          "text": "[添付: 音声 voice.mp3（audio/mpeg）]"}
    assert MP3_B64 not in json.dumps(seen)


# ---------------------------------------------------------------- CLI 系（ファイルとして）

def test_cli_audio_saved_as_file(tmp_path: Path):
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
    client.chat("sys", [Message("user", "聞いて", attachments=[MP3])], tools=[])
    saved = tmp_path / ATTACHMENTS_DIR / "voice.mp3"
    assert saved.read_bytes() == MP3.data
    args = (tmp_path / "args.txt").read_text(encoding="utf-8")
    assert f"[添付ファイル: {saved}]" in args


# ---------------------------------------------------------------- Session と会話ログ

class _RecordingModel:
    def __init__(self) -> None:
        self.seen: list[Message] = []

    def chat(self, system_prompt, messages, tools):
        self.seen = list(messages)
        return Message("assistant", "受け取りました")


def test_session_passes_audio_and_log_keeps_summary_only(tmp_path: Path):
    model = _RecordingModel()
    session = Session(Config(data_dir=tmp_path), model)
    reply = session.say("これ聞いて", [MP3])
    assert reply == "受け取りました"
    assert model.seen[-1].attachments == [MP3]
    logs = list((tmp_path / "conversations").glob("*.jsonl"))
    assert logs
    text = logs[0].read_text(encoding="utf-8")
    assert "[添付: 音声 voice.mp3（audio/mpeg）]" in text
    assert MP3_B64 not in text


# ---------------------------------------------------------------- TUI の /attach

def test_tui_load_attachment_audio(tmp_path: Path):
    from kuma_agent.tui import load_attachment

    target = tmp_path / "voice.mp3"
    target.write_bytes(b"ID3-fake")
    att = load_attachment("voice.mp3", tmp_path)
    assert att.name == "voice.mp3"
    assert att.media_type == "audio/mpeg"
    assert att.is_audio


# ---------------------------------------------------------------- Web（受理と上限）

class _EchoModel:
    def chat(self, system_prompt, messages, tools):
        names = [a.name for a in messages[-1].attachments]
        return Message("assistant", "添付: " + ",".join(names))


@pytest.fixture()
def web_server(tmp_path: Path):
    import threading
    import urllib.request  # noqa: F401  (fixture の利用側で使う)
    from kuma_agent.web import build_server

    session = Session(Config(data_dir=tmp_path), _EchoModel())
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
    import json as _json
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url, data=_json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, _json.loads(exc.read().decode("utf-8"))


def test_web_accepts_audio_attachment(web_server: str):
    status, data = _post(web_server + "/api/chat", {
        "message": "聞いて",
        "attachments": [{"name": "voice.mp3", "media_type": "audio/mpeg",
                         "data_base64": MP3_B64}],
    })
    assert status == 200
    assert data == {"reply": "添付: voice.mp3"}


def test_web_accepts_recorded_webm_with_codecs_param(web_server: str):
    # ブラウザの録音は media_type に ;codecs=opus が付くことがある
    status, data = _post(web_server + "/api/chat", {
        "message": "聞いて",
        "attachments": [{"name": "recording-1.webm",
                         "media_type": "audio/webm;codecs=opus",
                         "data_base64": base64.b64encode(b"webm").decode()}],
    })
    assert status == 200
    assert data == {"reply": "添付: recording-1.webm"}


def test_web_rejects_oversized_audio(web_server: str,
                                      monkeypatch: pytest.MonkeyPatch):
    import kuma_agent.web as web

    monkeypatch.setattr(web, "MAX_ATTACHMENT_BYTES", 3)
    status, _ = _post(web_server + "/api/chat", {
        "message": "x",
        "attachments": [{"name": "voice.mp3", "media_type": "audio/mpeg",
                         "data_base64": MP3_B64}],
    })
    assert status == 400


def test_tui_load_attachment_oversized_audio(tmp_path: Path):
    import kuma_agent.tui as tui
    from kuma_agent.tui import load_attachment

    big = tmp_path / "big.mp3"
    big.write_bytes(b"x" * 5)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tui, "MAX_ATTACHMENT_BYTES", 4)
        with pytest.raises(ValueError):
            load_attachment("big.mp3", tmp_path)


# ---------------------------------------------------------------- Web UI の録音・読み上げ要素

def test_web_page_has_recording_and_speech_controls():
    from kuma_agent.web import render_page

    page = render_page("muse")
    # 録音: ボタン、MediaRecorder、getUserMedia、録音中の表示、エラー表示
    assert 'id="record"' in page and "録音" in page
    assert "MediaRecorder" in page
    assert "getUserMedia" in page
    assert "録音中" in page
    assert "マイクを使えませんでした" in page
    # 読み上げ: speechSynthesis、ja-JP、読み上げボタン
    assert "speechSynthesis" in page
    assert "SpeechSynthesisUtterance" in page
    assert "ja-JP" in page
    assert "読み上げ" in page
    assert "speak-btn" in page
