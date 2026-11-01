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
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .llm import MAX_ATTACHMENT_BYTES, Attachment, guess_media_type
from .memory import list_session_ids, normalize_session_id
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
           flex-wrap: wrap; gap: 12px; padding: 8px 16px; }
  header .avatar { width: 44px; height: 44px; flex: none; }
  header .avatar svg { width: 100%; height: 100%; display: block; }
  header h1 { font-size: 18px; margin: 0; }
  header .provider { font-size: 12px; opacity: .8; }
  .sessions { display: flex; gap: 6px; align-items: center; margin-left: auto;
              flex-wrap: wrap; font-size: 13px; }
  .sessions select, .sessions input { padding: 6px; font-size: 13px;
           border-radius: 6px; border: 1px solid __COCOA__; }
  .sessions button { padding: 6px 12px; font-size: 13px; background: #fff;
           color: __COCOA__; border: 1px solid __COCOA__; }
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
  body.dragover #log { outline: 3px dashed __HONEY__; outline-offset: -8px; }
  #dropHint { display: none; text-align: center; padding: 8px; font-size: 14px;
              background: #fff; border-top: 1px solid __HONEY__; }
  body.dragover #dropHint { display: block; }
</style>
</head>
<body>
<header>
  <div class="avatar">__AVATAR__</div>
  <div>
    <h1>クマ</h1>
    <div class="provider">provider: __PROVIDER__</div>
  </div>
  <div class="sessions">
    <label for="sessionSelect">部屋</label>
    <select id="sessionSelect"></select>
    <input id="newSession" placeholder="新しい部屋" size="10">
    <button id="createSession" type="button">作成</button>
  </div>
</header>
<div id="log"></div>
<div id="dropHint">ファイルを離すと添付します</div>
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
const sessionSelect = document.getElementById('sessionSelect');
const newSessionInput = document.getElementById('newSession');
const createSessionBtn = document.getElementById('createSession');
let pendingFiles = [];
let currentSession = 'main';

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
    const res = await fetch('/api/history?session=' + encodeURIComponent(currentSession));
    const data = await res.json();
    for (const m of data.messages) {
      if ((m.role === 'user' || m.role === 'assistant') && (m.content || m.attachments)) {
        addMsg(m.role, describe(m.content, m.attachments));
      }
    }
  } catch (e) { /* 履歴が取れなくても会話は続けられる */ }
}

// 部屋（セッション）の切り替え。一覧はサーバーから取り、
// 選んだ部屋の履歴だけを表示する
function applySession(name) {
  currentSession = name;
  sessionSelect.value = name;
  log.innerHTML = '';
  loadHistory();
}

async function loadSessions() {
  try {
    const res = await fetch('/api/sessions');
    const data = await res.json();
    sessionSelect.innerHTML = '';
    for (const name of data.sessions) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      sessionSelect.appendChild(opt);
    }
    if (!data.sessions.includes(currentSession)) {
      currentSession = data.sessions[0] || 'main';
    }
    sessionSelect.value = currentSession;
  } catch (e) { /* 一覧が取れなくても既定の部屋で続けられる */ }
}

sessionSelect.addEventListener('change', () => {
  if (sessionSelect.value && sessionSelect.value !== currentSession) {
    applySession(sessionSelect.value);
  }
});
createSessionBtn.addEventListener('click', () => {
  const name = newSessionInput.value.trim();
  if (!name) return;
  newSessionInput.value = '';
  if (![...sessionSelect.options].some((o) => o.value === name)) {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    sessionSelect.appendChild(opt);
  }
  applySession(name);
});

function setPending(files) {
  pendingFiles = Array.from(files);
  filesLabel.textContent = pendingFiles.length
    ? '添付: ' + pendingFiles.map((f) => f.name).join('、') : '';
}

attachBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => setPending(fileInput.files));

// ドラッグ＆ドロップでも添付できる（画面全体がドロップ先）
document.addEventListener('dragover', (ev) => {
  ev.preventDefault();
  document.body.classList.add('dragover');
});
document.addEventListener('dragleave', (ev) => {
  if (!ev.relatedTarget) document.body.classList.remove('dragover');
});
document.addEventListener('drop', (ev) => {
  ev.preventDefault();
  document.body.classList.remove('dragover');
  if (ev.dataTransfer && ev.dataTransfer.files.length) {
    setPending(ev.dataTransfer.files);
  }
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
      body: JSON.stringify({message: text, attachments, session: currentSession}),
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

loadSessions();
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


class SessionStore:
    """名前 → Session の対応を管理する（Web UI の部屋の切り替え用）。

    初回アクセス時に factory で Session を作る。factory を省略すると
    最初の Session と同じ設定・モデルを共有して新しい部屋を作る。
    """

    def __init__(self, initial: Session,
                 factory: Callable[[str], Session] | None = None) -> None:
        self._sessions: dict[str, Session] = {initial.session_id: initial}
        self._factory = factory or (
            lambda name: Session(initial.cfg, initial.model, name))
        self._data_dir = initial.cfg.data_dir
        self._lock = threading.Lock()

    def get(self, name: str | None) -> Session:
        session_id = normalize_session_id(name)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = self._factory(session_id)
                self._sessions[session_id] = session
            return session

    def names(self) -> list[str]:
        """開いている部屋と、記録が残っている部屋の一覧。"""
        with self._lock:
            loaded = list(self._sessions)
        return sorted(set(loaded) | set(list_session_ids(self._data_dir)))


def make_handler(store: SessionStore, page: str):
    """SessionStore を受け取るリクエストハンドラクラスを作る。"""
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
            split = urlsplit(self.path)
            query = parse_qs(split.query)
            if split.path == "/":
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif split.path == "/api/sessions":
                self._send_json(200, {"sessions": store.names()})
            elif split.path == "/api/history":
                session = store.get(_first(query.get("session")))
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
            if urlsplit(self.path).path != "/api/chat":
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
            session = store.get(data.get("session"))
            try:
                with lock:  # Session はスレッド安全ではないので直列化する
                    reply = session.say(message, attachments)
            except Exception as exc:  # モデル側の失敗を画面に返す
                self._send_json(500, {"error": f"応答の生成に失敗しました: {exc}"})
                return
            self._send_json(200, {"reply": reply})

    return Handler


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None


def build_server(session: Session, host: str = "127.0.0.1",
                 port: int = 8765,
                 session_factory: Callable[[str], Session] | None = None,
                 ) -> ThreadingHTTPServer:
    """Session を差し込んだ HTTP サーバーを作る（テストからも使う）。

    session_factory を渡すと、ページから新しい部屋を開いたときに
    その factory で Session を作る。省略時は session と同じ設定・
    モデルを共有する。
    """
    page = render_page(session.cfg.provider)
    store = SessionStore(session, session_factory)
    return ThreadingHTTPServer((host, port), make_handler(store, page))


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
