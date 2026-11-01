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
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .llm import MAX_ATTACHMENT_BYTES, Attachment, guess_media_type
from .memory import (
    DEFAULT_SESSION_ID,
    list_session_ids,
    list_tenant_names,
    normalize_session_id,
    normalize_tenant_name,
)
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
  .msg .quoted { display: block; font-size: 12px; opacity: .85;
                 border-left: 3px solid currentColor; padding-left: 6px;
                 margin-bottom: 4px; }
  .msg .bodyText { display: block; }
  .quote-btn { display: block; margin-top: 6px; padding: 2px 8px;
               font-size: 12px; background: transparent; color: inherit;
               border: 1px solid currentColor; border-radius: 999px; }
  form { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px; background: #fff;
         border-top: 1px solid __HONEY__; }
  #quoteBox { flex-basis: 100%; display: flex; gap: 8px; align-items: center;
              font-size: 13px; background: __CREAM__; padding: 6px 8px;
              border-left: 4px solid __HONEY__; }
  #quoteBox[hidden] { display: none; }
  #quoteText { flex: 1; overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; }
  #clearQuote { padding: 4px 10px; font-size: 12px; }
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
    <label for="tenantSelect">テナント</label>
    <select id="tenantSelect"></select>
    <input id="newTenant" placeholder="新しいテナント" size="10">
    <button id="createTenant" type="button">作成</button>
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
  <div id="quoteBox" hidden>
    <span id="quoteText"></span>
    <button id="clearQuote" type="button">解除</button>
  </div>
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
const tenantSelect = document.getElementById('tenantSelect');
const newTenantInput = document.getElementById('newTenant');
const createTenantBtn = document.getElementById('createTenant');
const sessionSelect = document.getElementById('sessionSelect');
const newSessionInput = document.getElementById('newSession');
const createSessionBtn = document.getElementById('createSession');
const quoteBox = document.getElementById('quoteBox');
const quoteText = document.getElementById('quoteText');
const clearQuoteBtn = document.getElementById('clearQuote');
let pendingFiles = [];
let pendingQuote = null;
let currentTenant = 'default';
let currentSession = 'main';

function setQuote(text) {
  if (!text) return;
  pendingQuote = text;
  quoteText.textContent = '引用: ' + text;
  quoteBox.hidden = false;
  input.focus();
}

function clearQuote() {
  pendingQuote = null;
  quoteText.textContent = '';
  quoteBox.hidden = true;
}

clearQuoteBtn.addEventListener('click', clearQuote);

function addMsg(role, text, quote = null, quoteSource = null) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  if (quote) {
    const quoted = document.createElement('span');
    quoted.className = 'quoted';
    quoted.textContent = '「' + quote + '」への返信';
    div.appendChild(quoted);
  }
  const body = document.createElement('span');
  body.className = 'bodyText';
  body.textContent = text;
  div.appendChild(body);
  const quoteBtn = document.createElement('button');
  quoteBtn.type = 'button';
  quoteBtn.className = 'quote-btn';
  quoteBtn.textContent = '引用';
  quoteBtn.addEventListener('click', () => setQuote(quoteSource || text));
  div.appendChild(quoteBtn);
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
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&session=' + encodeURIComponent(currentSession);
    const res = await fetch('/api/history' + query);
    const data = await res.json();
    for (const m of data.messages) {
      if ((m.role === 'user' || m.role === 'assistant') && (m.content || m.attachments)) {
        const shown = describe(m.content, m.attachments);
        addMsg(m.role, shown, m.quote || null, m.content || shown);
      }
    }
  } catch (e) { /* 履歴が取れなくても会話は続けられる */ }
}

// 部屋（セッション）の切り替え。一覧はサーバーから取り、
// 選んだ部屋の履歴だけを表示する
function applySession(name) {
  currentSession = name;
  sessionSelect.value = name;
  clearQuote();
  log.innerHTML = '';
  loadHistory();
}

async function loadSessions() {
  try {
    const res = await fetch('/api/sessions?tenant=' + encodeURIComponent(currentTenant));
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

// テナントの切り替え。テナントが変われば、そのテナントの部屋一覧と
// 履歴に切り替える（部屋名は引き継ぎ、無ければ先頭の部屋にする）
async function applyTenant(name) {
  currentTenant = name;
  tenantSelect.value = name;
  await loadSessions();
  applySession(currentSession);
}

async function loadTenants() {
  try {
    const res = await fetch('/api/tenants');
    const data = await res.json();
    tenantSelect.innerHTML = '';
    for (const name of data.tenants) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      tenantSelect.appendChild(opt);
    }
    if (data.current && data.tenants.includes(data.current)) {
      currentTenant = data.current;
    }
    if (!data.tenants.includes(currentTenant)) {
      currentTenant = data.tenants[0] || 'default';
    }
    tenantSelect.value = currentTenant;
  } catch (e) { /* 一覧が取れなくても既定のテナントで続けられる */ }
}

tenantSelect.addEventListener('change', () => {
  if (tenantSelect.value && tenantSelect.value !== currentTenant) {
    applyTenant(tenantSelect.value);
  }
});
createTenantBtn.addEventListener('click', () => {
  const name = newTenantInput.value.trim();
  if (!name) return;
  newTenantInput.value = '';
  if (![...tenantSelect.options].some((o) => o.value === name)) {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    tenantSelect.appendChild(opt);
  }
  applyTenant(name);
});

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
  const quote = pendingQuote;
  addMsg('user', describe(text, files.map((f) => f.name)), quote, text);
  input.value = '';
  clearQuote();
  pendingFiles = [];
  fileInput.value = '';
  filesLabel.textContent = '';
  send.disabled = true;
  try {
    const attachments = await Promise.all(files.map(readAttachment));
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, attachments, quote,
                            session: currentSession, tenant: currentTenant}),
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

(async () => {
  await loadTenants();
  await loadSessions();
  loadHistory();
})();
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
        self.tenant = initial.tenant
        self._data_dir = initial.cfg.tenant_data_dir
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


class TenantStore:
    """テナント → SessionStore の対応を管理する（Web UI のテナント切替用）。

    サーバーはテナントごとに SessionStore を持ち、初回アクセス時に
    そのテナントの Session を作る。新しいテナントの Session は、
    起動時の設定（プロバイダやモデルなど）を引き継ぎ、テナントだけを
    差し替えた Config で作るため、データの置き場はテナントごとに
    分かれる。session_factory は起動時テナントの部屋の作り方だけを
    差し替える（テスト用）。
    """

    def __init__(self, initial: Session,
                 session_factory: Callable[[str], Session] | None = None,
                 ) -> None:
        self._cfg = initial.cfg
        self._model = initial.model
        self.default_tenant = initial.tenant
        self._stores: dict[str, SessionStore] = {
            self.default_tenant: SessionStore(initial, session_factory),
        }
        self._lock = threading.Lock()

    def _make_session(self, tenant: str, session_id: str) -> Session:
        cfg = replace(self._cfg, tenant=normalize_tenant_name(tenant))
        return Session(cfg, self._model, session_id)

    def store(self, tenant: str | None) -> SessionStore:
        name = normalize_tenant_name(tenant)
        with self._lock:
            store = self._stores.get(name)
            if store is None:
                initial = self._make_session(name, DEFAULT_SESSION_ID)
                store = SessionStore(initial)
                self._stores[name] = store
            return store

    def names(self) -> list[str]:
        """開いているテナントと、保存済みテナントの一覧。"""
        with self._lock:
            loaded = list(self._stores)
        return sorted(set(loaded) | set(list_tenant_names(self._cfg.data_dir)))


def make_handler(tenants: TenantStore, page: str):
    """TenantStore を受け取るリクエストハンドラクラスを作る。"""
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
            elif split.path == "/api/tenants":
                self._send_json(200, {"tenants": tenants.names(),
                                      "current": tenants.default_tenant})
            elif split.path == "/api/sessions":
                store = tenants.store(_first(query.get("tenant")))
                self._send_json(200, {"sessions": store.names()})
            elif split.path == "/api/history":
                store = tenants.store(_first(query.get("tenant")))
                session = store.get(_first(query.get("session")))
                messages = []
                for m in session.history:
                    entry: dict = {"role": m.role, "content": m.content}
                    if m.attachments:  # 添付がある発話だけ名前一覧を足す
                        entry["attachments"] = [a.name for a in m.attachments]
                    if m.quote:  # 引用がある発話だけ引用元の本文を足す
                        entry["quote"] = m.quote
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
                quote = data.get("quote")
                if quote is not None and not isinstance(quote, str):
                    raise ValueError("quote は文字列で指定してください")
                if quote is not None and not quote.strip():
                    quote = None
                attachments = parse_attachments(data.get("attachments"))
            except (ValueError, KeyError, TypeError):
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            session = tenants.store(data.get("tenant")).get(data.get("session"))
            try:
                with lock:  # Session はスレッド安全ではないので直列化する
                    reply = session.say(message, attachments, quote=quote)
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
    モデルを共有する。テナントは TenantStore がテナントごとに
    SessionStore を持ち、ページからの切り替えに応じる。
    """
    page = render_page(session.cfg.provider)
    tenants = TenantStore(session, session_factory)
    return ThreadingHTTPServer((host, port), make_handler(tenants, page))


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
