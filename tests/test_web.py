import json
import threading
import urllib.request
from pathlib import Path

import pytest

from kuma_agent.config import Config
from kuma_agent.llm import Message
from kuma_agent.session import Session
from kuma_agent.web import COCOA, CREAM, HONEY, build_server, render_page


class _EchoModel:
    """受け取った発話をそのまま返す偽モデル。"""

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", f"返答: {messages[-1].content}")


@pytest.fixture()
def server(tmp_path: Path):
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


def _get(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post_json(url: str, payload: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_index_serves_chat_page_with_brand_colors(server: str):
    status, body = _get(server + "/")
    assert status == 200
    assert HONEY in body and COCOA in body and CREAM in body
    assert "クマ" in body


def test_render_page_contains_avatar_and_provider():
    page = render_page("gemini", avatar_svg="<svg>avatar</svg>")
    assert "<svg>avatar</svg>" in page
    assert "gemini" in page


def test_render_page_has_theme_switcher():
    page = render_page("muse")
    assert 'id="themeSelect"' in page
    assert "昼" in page and "夜" in page and "システム" in page
    assert 'data-theme="dark"' in page  # 夜テーマの配色定義
    assert "kuma-theme" in page  # 選択を覚える保存キー


def test_chat_endpoint_replies_with_fake_model(server: str):
    status, data = _post_json(server + "/api/chat", {"message": "こんにちは"})
    assert status == 200
    assert data == {"reply": "返答: こんにちは"}


def test_history_endpoint_returns_conversation(server: str):
    _post_json(server + "/api/chat", {"message": "やあ"})
    status, body = _get(server + "/api/history")
    assert status == 200
    messages = json.loads(body)["messages"]
    assert {"role": "user", "content": "やあ"} in messages
    assert {"role": "assistant", "content": "返答: やあ"} in messages


def test_page_supports_drag_and_drop():
    from kuma_agent.web import render_page

    page = render_page("muse")
    assert "dragover" in page and "drop" in page
    assert "dataTransfer" in page and "dropHint" in page
