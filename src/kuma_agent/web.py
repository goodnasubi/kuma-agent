"""Web UI。標準ライブラリの http.server だけで動く単一ページのチャット画面。

前提: これは単一ユーザー・ローカル利用専用の UI である。認証や
アクセス制御は持たないので、既定では 127.0.0.1 にだけバインドする。
外部に公開する用途では使わないこと。
"""
from __future__ import annotations

import base64
import binascii
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .llm import MAX_ATTACHMENT_BYTES, Attachment, guess_media_type
from .session import Session

# ブランド色（README のイメージカラー）
HONEY = "#E79A3C"
COCOA = "#4B3226"
CREAM = "#FFF3E0"

_FALLBACK_AVATAR = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" role="img" aria-label="クマ">
  <circle cx="100" cy="100" r="98" fill="#4B3226"/>
  <text x="100" y="118" font-size="52" text-anchor="middle" fill="#FFF3E0">クマ</text>
</svg>"""


def load_avatar_svg() -> str:
    """リポジトリの assets/avatar.svg を読む。無ければ簡易表示で代替する。"""
    path = Path(__file__).resolve().parents[2] / "assets" / "avatar.svg"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return _FALLBACK_AVATAR


_PAGE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kuma-agent</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: sans-serif; background: __CREAM__; color: __COCOA__;
         display: flex; flex-direction: column; height: 100dvh; }
  header { background: __COCOA__; color: __CREAM__; display: flex; align-items: center;
           gap: 12px; padding: 8px 16px; }
  header .avatar { width: 44px; height: 44px; flex: none; }
  header .avatar svg { width: 100%; height: 100%; display: block; }
  header h1 { font-size: 18px; margin: 0; }
  header .provider { font-size: 12px; opacity: .8; }
  #log { flex: 1; overflow-y: auto; padding: 16px; display: flex;
         flex-direction: column; gap: 10px; }
  .msg { max-width: 80%; padding: 10px 14px; border-radius: 14px;
         white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
  .msg.user { align-self: flex-end; background: __HONEY__; color: #fff; }
  .msg.assistant { align-self: flex-start; background: #fff;
                    border: 1px solid __HONEY__; }
  form { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px; background: #fff;
         border-top: 1px solid __HONEY__; }
  #files { flex-basis: 100%; font-size: 13px; min-height: 1em; }
  #files:empty { display: none; }
  #input { flex: 1; padding: 10px; font-size: 16px; border: 1px solid __COCOA__;
           border-radius: 8px; }
  button { padding: 10px 20px; font-size: 16px; border: none; border-radius: 8px;
           background: __HONEY__; color: #fff; cursor: pointer; }
  button:disabled { opacity: .5; }
  #attach { background: #fff; color: __COCOA__; border: 1px solid __COCOA__; }
</style>
</head>
<body>
<header>
  <div class="avatar">__AVATAR__</div>
  <div>
    <h1>クマ</h1>
    <div class="provider">provider: __PROVIDER__</div>
  </div>
</header>
<div id="log"></div>
<form id="form">
  <div id="files"></div>
  <button id="attach" type="button">添付</button>
  <input id="file" type="file" multiple hidden>
  <input id="input" autocomplete="off" placeholder="話しかけてください">
  <button id="send" type="submit">送信</button>
</form>
<script>
const log = document.getElementById('log');
const form = document.getElementById('form');
const input = document.getElementById('input');
const send = document.getElementById('send');
const attachBtn = document.getElementById('attach');
const fileInput = document.getElementById('file');
const filesLabel = document.getElementById('files');
let pendingFiles = [];

function addMsg(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function describe(text, names) {
  if (!names || !names.length) return text;
  const suffix = names.map((n) => '[添付: ' + n + ']').join(' ');
  return text ? text + ' ' + suffix : suffix;
}

async function loadHistory() {
  try {
    const res = await fetch('/api/history');
    const data = await res.json();
    for (const m of data.messages) {
      if ((m.role === 'user' || m.role === 'assistant') && (m.content || m.attachments)) {
        addMsg(m.role, describe(m.content, m.attachments));
      }
    }
  } catch (e) { /* 履歴が取れなくても会話は続けられる */ }
}

attachBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  pendingFiles = Array.from(fileInput.files);
  filesLabel.textContent = pendingFiles.length
    ? '添付: ' + pendingFiles.map((f) => f.name).join('、') : '';
});

function readAttachment(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({
      name: file.name,
      media_type: file.type || '',
      data_base64: String(reader.result).split(',')[1],
    });
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  const files = pendingFiles;
  addMsg('user', describe(text, files.map((f) => f.name)));
  input.value = '';
  pendingFiles = [];
  fileInput.value = '';
  filesLabel.textContent = '';
  send.disabled = true;
  try {
    const attachments = await Promise.all(files.map(readAttachment));
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, attachments}),
    });
    const data = await res.json();
    addMsg('assistant', data.reply || ('エラー: ' + (data.error || res.status)));
  } catch (e) {
    addMsg('assistant', '通信に失敗しました');
  } finally {
    send.disabled = false;
    input.focus();
  }
});

loadHistory();
input.focus();
</script>
</body>
</html>
"""


def render_page(provider: str, avatar_svg: str | None = None) -> str:
    """チャット画面の HTML を組み立てる。"""
    return (
        _PAGE_TEMPLATE
        .replace("__AVATAR__", avatar_svg if avatar_svg is not None else load_avatar_svg())
        .replace("__PROVIDER__", provider)
        .replace("__HONEY__", HONEY)
        .replace("__COCOA__", COCOA)
        .replace("__CREAM__", CREAM)
    )


def parse_attachments(raw: object) -> list[Attachment]:
    """JSON の添付配列を Attachment のリストに復号する。

    形式は [{"name": ..., "media_type": ..., "data_base64": ...}]。
    不正な base64 や上限（MAX_ATTACHMENT_BYTES）を超える添付は
    ValueError にする（呼び出し側で 400 として返す）。
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("attachments は配列で指定してください")
    attachments: list[Attachment] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("添付の形式が不正です")
        name = Path(str(item.get("name") or "attachment")).name or "attachment"
        media_type = str(item.get("media_type") or "") or guess_media_type(name)
        try:
            data = base64.b64decode(str(item["data_base64"]), validate=True)
        except (KeyError, binascii.Error) as exc:
            raise ValueError("添付の base64 が不正です") from exc
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"添付が大きすぎます（上限 {MAX_ATTACHMENT_BYTES} バイト）: {name}")
        attachments.append(Attachment(name=name, media_type=media_type, data=data))
    return attachments


def make_handler(session: Session, page: str):
    """Session を受け取るリクエストハンドラクラスを作る。"""
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # 標準のアクセスログを止める
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, obj: dict) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/history":
                messages = []
                for m in session.history:
                    entry: dict = {"role": m.role, "content": m.content}
                    if m.attachments:  # 添付がある発話だけ名前一覧を足す
                        entry["attachments"] = [a.name for a in m.attachments]
                    messages.append(entry)
                self._send_json(200, {"messages": messages})
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if self.path != "/api/chat":
                self._send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                message = data["message"]
                if not isinstance(message, str) or not message.strip():
                    raise ValueError("message が空です")
                attachments = parse_attachments(data.get("attachments"))
            except (ValueError, KeyError, TypeError):
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            try:
                with lock:  # Session はスレッド安全ではないので直列化する
                    reply = session.say(message, attachments)
            except Exception as exc:  # モデル側の失敗を画面に返す
                self._send_json(500, {"error": f"応答の生成に失敗しました: {exc}"})
                return
            self._send_json(200, {"reply": reply})

    return Handler


def build_server(session: Session, host: str = "127.0.0.1",
                 port: int = 8765) -> ThreadingHTTPServer:
    """Session を差し込んだ HTTP サーバーを作る（テストからも使う）。"""
    page = render_page(session.cfg.provider)
    return ThreadingHTTPServer((host, port), make_handler(session, page))


def serve(session: Session, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Web UI を起動し、Ctrl-C まで待ち続ける。"""
    server = build_server(session, host, port)
    print(f"クマの Web UI: http://{host}:{server.server_address[1]} （終了は Ctrl-C）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
