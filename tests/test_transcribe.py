"""音声の文字起こし（transcribe）のテスト。

頭脳が Gemini でない場合に、音声添付を Gemini で書き起こして
頭脳へ渡す仕組み。Gemini API への通信は httpx.MockTransport で
差し替え、Session 経由のテストでは transcribe.transcribe_audio
自体をスタブに差し替える（ネットワークには出ない）。
"""
import base64
import json
import stat
from pathlib import Path

import httpx
import pytest

from kuma_agent import transcribe
from kuma_agent.config import Config
from kuma_agent.llm import (
    AnthropicClient,
    Attachment,
    GeminiClient,
    Message,
    MetaModelClient,
    fallback_text,
)
from kuma_agent.session import Session

MP3_DATA = b"ID3-fake"
MP3_B64 = base64.b64encode(MP3_DATA).decode("ascii")
TRANSCRIPT = "こんにちは、明日の予定を教えてください"


def _mp3() -> Attachment:
    return Attachment("voice.mp3", "audio/mpeg", MP3_DATA)


def _transport(body: dict, seen: dict | None = None,
               status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen["url"] = str(request.url)
            seen["api_key"] = request.headers.get("x-goog-api-key")
            seen["payload"] = json.loads(request.content)
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def _text_body(text: str = TRANSCRIPT) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


# ---------------------------------------------------------------- 実処理

def test_transcribe_audio_returns_text_and_sends_audio():
    seen: dict = {}
    text = transcribe.transcribe_audio(
        _mp3(), api_key="test-key",
        transport=_transport(_text_body(), seen))
    assert text == TRANSCRIPT
    # リクエストは GeminiClient と同じ形（宛先・キー・既定モデル）
    assert seen["url"].endswith(
        "/v1beta/models/gemini-3.8-flash:generateContent")
    assert seen["api_key"] == "test-key"
    parts = seen["payload"]["contents"][0]["parts"]
    assert parts[0] == {"text": transcribe.INSTRUCTION}
    assert parts[1] == {"inlineData": {"mimeType": "audio/mpeg",
                                       "data": MP3_B64}}


def test_transcribe_audio_joins_multiple_text_parts():
    body = {"candidates": [{"content": {"parts": [
        {"text": "こんにちは、"}, {"text": "クマです"}]}}]}
    text = transcribe.transcribe_audio(
        _mp3(), api_key="test-key", transport=_transport(body))
    assert text == "こんにちは、クマです"


def test_transcribe_audio_uses_env_key_and_model(
        monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setenv("KUMA_TRANSCRIBE_MODEL", "custom-transcribe-model")
    seen: dict = {}
    transcribe.transcribe_audio(
        _mp3(), transport=_transport(_text_body(), seen))
    assert seen["api_key"] == "google-key"
    assert "/models/custom-transcribe-model:" in seen["url"]


def test_transcribe_audio_prefers_gemini_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    seen: dict = {}
    transcribe.transcribe_audio(
        _mp3(), transport=_transport(_text_body(), seen))
    assert seen["api_key"] == "gemini-key"


def test_transcribe_audio_without_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(transcribe.TranscriptionError, match="設定がありません"):
        transcribe.transcribe_audio(_mp3())


def test_transcribe_audio_http_error():
    with pytest.raises(transcribe.TranscriptionError,
                       match="文字起こしに失敗しました"):
        transcribe.transcribe_audio(
            _mp3(), api_key="test-key", transport=_transport({}, status=500))


def test_transcribe_audio_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(transcribe.TranscriptionError,
                       match="文字起こしに失敗しました"):
        transcribe.transcribe_audio(
            _mp3(), api_key="test-key",
            transport=httpx.MockTransport(handler))


def test_transcribe_audio_no_text_in_response():
    with pytest.raises(transcribe.TranscriptionError,
                       match="返ってきませんでした"):
        transcribe.transcribe_audio(
            _mp3(), api_key="test-key", transport=_transport({}))


def test_transcription_enabled_and_available(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("KUMA_TRANSCRIBE", raising=False)
    assert transcribe.transcription_enabled()
    assert transcribe.available()
    monkeypatch.setenv("KUMA_TRANSCRIBE", "0")
    assert not transcribe.transcription_enabled()
    assert not transcribe.available()
    monkeypatch.delenv("KUMA_TRANSCRIBE")
    monkeypatch.delenv("GEMINI_API_KEY")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert transcribe.transcription_enabled()
    assert not transcribe.available()  # キーが無ければ試みられない


# ---------------------------------------------------------------- 頭脳への渡し方（llm 層）

def test_model_note_includes_transcript():
    att = _mp3()
    assert att.model_note() == "[添付: 音声 voice.mp3（audio/mpeg）]"
    att.transcript = TRANSCRIPT
    assert att.model_note() == (
        "[添付: 音声 voice.mp3（audio/mpeg）]\n"
        f"（文字起こし）{TRANSCRIPT}")
    assert fallback_text(att) == att.model_note()


def test_anthropic_content_includes_transcript():
    att = _mp3()
    att.transcript = TRANSCRIPT
    blocks = AnthropicClient._content(
        Message("user", "聞いて", attachments=[att]))
    assert blocks[1] == {"type": "text", "text": att.model_note()}
    assert MP3_B64 not in json.dumps(blocks, ensure_ascii=False)


def test_muse_content_includes_transcript():
    att = _mp3()
    att.transcript = TRANSCRIPT
    parts = MetaModelClient._content(
        Message("user", "聞いて", attachments=[att]))
    assert parts[1] == {"type": "text", "text": att.model_note()}
    assert MP3_B64 not in json.dumps(parts, ensure_ascii=False)


# ---------------------------------------------------------------- Session の合流点

class _RecordingModel:
    def __init__(self) -> None:
        self.seen: list[Message] = []

    def chat(self, system_prompt, messages, tools):
        self.seen = list(messages)
        return Message("assistant", "受け取りました")


def _stub_transcribe(monkeypatch: pytest.MonkeyPatch,
                     text: str = TRANSCRIPT) -> list:
    calls: list = []

    def fake(attachment: Attachment) -> str:
        calls.append(attachment)
        return text

    monkeypatch.setattr(transcribe, "transcribe_audio", fake)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("KUMA_TRANSCRIBE", raising=False)
    return calls


def test_session_transcribes_for_non_gemini_brain(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = _stub_transcribe(monkeypatch)
    statuses: list[str] = []
    model = _RecordingModel()
    session = Session(Config(data_dir=tmp_path, provider="claude"), model,
                      on_status=statuses.append)
    att = _mp3()
    reply = session.say("これ聞いて", [att])
    assert reply == "受け取りました"
    # 文字起こしが走り、結果が添付に載って頭脳へ渡る
    assert calls == [att]
    assert att.transcript == TRANSCRIPT
    assert model.seen[-1].attachments[0].transcript == TRANSCRIPT
    # 作業中表示にも出る
    assert "音声を文字にしています" in statuses
    # 会話ログに文字起こしが残る（バイト列は残らない）
    logs = list((tmp_path / "conversations").glob("*.jsonl"))
    assert logs
    text = logs[0].read_text(encoding="utf-8")
    assert f"（文字起こし）{TRANSCRIPT}" in text
    assert MP3_B64 not in text


def test_session_cli_brain_gets_transcript_in_prompt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from kuma_agent.agent_cli import ATTACHMENTS_DIR, ClaudeCodeClient

    _stub_transcribe(monkeypatch)
    script = tmp_path / "fake-claude"
    script.write_text(
        "#!/bin/sh\n"
        "echo done\n"
        "echo \"$@\" > \"$(dirname \"$0\")/args.txt\"\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    client = ClaudeCodeClient(command=str(script), work_dir=tmp_path)
    session = Session(
        Config(data_dir=tmp_path / "data", provider="claude-code"), client)
    session.say("これ聞いて", [_mp3()])
    args = (tmp_path / "args.txt").read_text(encoding="utf-8")
    # ファイルとして置かれたうえで、文字起こしの文章も指示に含まれる
    saved = tmp_path / ATTACHMENTS_DIR / "voice.mp3"
    assert f"[添付ファイル: {saved}]" in args
    assert f"（文字起こし）{TRANSCRIPT}" in args


_GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}


def test_session_gemini_client_skips_transcription(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = _stub_transcribe(monkeypatch)
    seen: dict = {}
    client = GeminiClient(
        "k", transport=_transport(_GEMINI_OK, seen))
    # プロバイダ指定は gemini でなくても、頭脳が Gemini なら
    # 音声データ自体を渡し、文字起こしは呼ばれない
    session = Session(Config(data_dir=tmp_path, provider="claude"), client)
    att = _mp3()
    assert session.say("聞いて", [att]) == "ok"
    assert calls == []
    assert att.transcript is None
    parts = seen["payload"]["contents"][-1]["parts"]
    assert {"inlineData": {"mimeType": "audio/mpeg", "data": MP3_B64}} in parts


def test_session_gemini_provider_skips_transcription(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = _stub_transcribe(monkeypatch)
    session = Session(Config(data_dir=tmp_path, provider="gemini"),
                      _RecordingModel())
    att = _mp3()
    session.say("聞いて", [att])
    assert calls == []
    assert att.transcript is None


def test_session_without_key_keeps_old_behavior(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list = []

    def fake(attachment: Attachment) -> str:
        calls.append(attachment)
        return TRANSCRIPT

    monkeypatch.setattr(transcribe, "transcribe_audio", fake)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    session = Session(Config(data_dir=tmp_path, provider="claude"),
                      _RecordingModel())
    att = _mp3()
    assert session.say("聞いて", [att]) == "受け取りました"
    assert calls == []  # キーが無ければ試みもしない
    assert att.transcript is None
    logs = list((tmp_path / "conversations").glob("*.jsonl"))
    text = logs[0].read_text(encoding="utf-8")
    assert "[添付: 音声 voice.mp3（audio/mpeg）]" in text
    assert "（文字起こし）" not in text


def test_session_transcribe_failure_falls_back(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail(attachment: Attachment) -> str:
        raise transcribe.TranscriptionError("だめでした")

    monkeypatch.setattr(transcribe, "transcribe_audio", fail)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    session = Session(Config(data_dir=tmp_path, provider="claude"),
                      _RecordingModel())
    att = _mp3()
    # 失敗しても会話は壊れず、従来の扱い（注記のみ）になる
    assert session.say("聞いて", [att]) == "受け取りました"
    assert att.transcript is None


def test_session_transcribe_disabled_by_env(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = _stub_transcribe(monkeypatch)
    monkeypatch.setenv("KUMA_TRANSCRIBE", "0")
    session = Session(Config(data_dir=tmp_path, provider="claude"),
                      _RecordingModel())
    att = _mp3()
    assert session.say("聞いて", [att]) == "受け取りました"
    assert calls == []
    assert att.transcript is None


def test_session_non_audio_attachment_not_transcribed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = _stub_transcribe(monkeypatch)
    session = Session(Config(data_dir=tmp_path, provider="claude"),
                      _RecordingModel())
    image = Attachment("a.png", "image/png", b"png-fake")
    session.say("見て", [image])
    assert calls == []
