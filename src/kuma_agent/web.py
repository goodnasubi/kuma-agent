"""Web UI。標準ライブラリの http.server だけで動く単一ページのチャット画面。

前提: これは単一ユーザー・ローカル利用専用の UI である。既定では
127.0.0.1 にだけバインドし、認証も無い。外に出して使う場合は、
環境変数 KUMA_WEB_PASSWORD でパスワードログインを有効にすること
（有効化しなければ認証なしのままなので、公開は推奨しない）。
"""
from __future__ import annotations

import base64
import binascii
import hmac
import json
import mimetypes
import secrets
import shutil
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, quote, urlsplit

from .config import Config, mode_label, normalize_mode
from .export import (
    build_export_zip,
    default_export_filename,
    save_tenant_backup,
)
from .llm import MAX_ATTACHMENT_BYTES, Attachment, guess_media_type
from .memory import (
    DEFAULT_SESSION_ID,
    DEFAULT_TENANT,
    cleanup_expired,
    delete_session_record,
    list_session_ids,
    list_tenant_names,
    list_user_names,
    load_retention_overrides,
    normalize_session_id,
    normalize_tenant_name,
    normalize_user_name,
    retention_days_for,
    save_retention_override,
    tenant_dir,
    user_dir,
)
from .mcp_client import close_shared_managers
from .notify_prefs import (
    add_muted_topic,
    clear_quiet_hours,
    load_notify_prefs,
    remove_muted_topic,
    set_quiet_hours,
)
from .outputs import OutputBoard, format_created_at, format_size
from .proactive import run_sweep
from .profile_files import read_profile_file, save_profile_file
from .session import Session
from .telegram import build_proactive_notifier
from .telegram import start_polling_thread as start_telegram_polling
from .settings import (
    approvals_enabled_for,
    proactive_enabled_for,
    remove_tenant_settings,
    set_approvals_enabled,
    set_proactive_enabled,
)
from .tools import summarize_tool_args
from .usage import summarize_usage

# ブランド色（README のイメージカラー）
HONEY = "#E79A3C"
COCOA = "#4B3226"
CREAM = "#FFF3E0"

_FALLBACK_AVATAR = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" role="img" aria-label="クマ">
  <circle cx="100" cy="100" r="98" fill="#4B3226"/>
  <text x="100" y="118" font-size="52" text-anchor="middle" fill="#FFF3E0">クマ</text>
</svg>"""


def _avatar_svg_candidates() -> list[Path]:
    """アバター画像（assets/avatar.svg）の候補パスを優先順に返す。

    1つ目はリポジトリ配置（src の2つ上）で、ソースから動かす場合の
    従来の場所。pip で導入した環境ではそこに assets が無いため、
    2つ目にカレントディレクトリの assets/ も候補にする（コンテナ
    では WORKDIR に assets をコピーしてある前提）。
    """
    return [
        Path(__file__).resolve().parents[2] / "assets" / "avatar.svg",
        Path.cwd() / "assets" / "avatar.svg",
    ]


def load_avatar_svg() -> str:
    """assets/avatar.svg を候補の順に探して読む。無ければ簡易表示で代替する。"""
    for path in _avatar_svg_candidates():
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            continue
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
  .sessions[hidden] { display: none; }
  #tenantPanel { background: #fff; border-bottom: 1px solid __HONEY__;
                 padding: 12px 16px; }
  #tenantPanel[hidden] { display: none; }
  #tenantPanel h2 { font-size: 15px; margin: 0 0 8px; }
  .tenantRow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               padding: 8px 0; border-top: 1px dashed __HONEY__; font-size: 14px; }
  .tenantRow .tname { font-weight: bold; min-width: 8em; }
  .tenantRow input[type="number"] { width: 5em; padding: 4px; font-size: 13px;
               border-radius: 6px; border: 1px solid __COCOA__; }
  .tenantRow button, .adminNew button { padding: 4px 12px; font-size: 13px;
               background: #fff; color: __COCOA__; border: 1px solid __COCOA__; }
  .adminNew { display: flex; gap: 8px; align-items: center; margin-top: 10px;
              font-size: 14px; }
  .adminNew input { padding: 6px; font-size: 13px; border-radius: 6px;
               border: 1px solid __COCOA__; }
  #tenantPanel h3 { font-size: 14px; margin: 16px 0 4px;
               border-top: 1px solid __HONEY__; padding-top: 12px; }
  #outputsPanel { background: #fff; border-bottom: 1px solid __HONEY__;
                 padding: 12px 16px; }
  #outputsPanel[hidden] { display: none; }
  #outputsPanel h2 { font-size: 15px; margin: 0 0 8px; }
  .outputRow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               padding: 8px 0; border-top: 1px dashed __HONEY__; font-size: 14px; }
  .outputRow .otitle { font-weight: bold; min-width: 8em; }
  .outputRow .oname, .outputRow .ometa { opacity: .85; }
  .outputRow a { padding: 4px 12px; font-size: 13px; background: #fff;
               color: __COCOA__; border: 1px solid __COCOA__;
               border-radius: 8px; text-decoration: none; }
  #profilePanel { background: #fff; border-bottom: 1px solid __HONEY__;
                 padding: 12px 16px; }
  #profilePanel[hidden] { display: none; }
  #profilePanel h2 { font-size: 15px; margin: 0 0 8px; }
  #profilePanel .profileNote { font-size: 13px; margin: 0 0 8px; }
  #profileTabs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  #profileTabs button { padding: 4px 12px; font-size: 13px; background: #fff;
               color: __COCOA__; border: 1px solid __COCOA__; }
  #profileTabs button.active { background: __HONEY__; color: #fff;
               border-color: __HONEY__; }
  #profileText { width: 100%; padding: 10px; font-size: 14px; line-height: 1.5;
               border: 1px solid __COCOA__; border-radius: 8px;
               font-family: monospace; }
  .profileActions { display: flex; gap: 10px; align-items: center;
               margin-top: 8px; }
  .profileActions button { padding: 6px 16px; font-size: 14px; }
  #profileStatus { font-size: 13px; }
  #usagePanel { background: #fff; border-bottom: 1px solid __HONEY__;
                 padding: 12px 16px; }
  #usagePanel[hidden] { display: none; }
  #usagePanel h2 { font-size: 15px; margin: 0 0 8px; }
  #usagePanel h3 { font-size: 14px; margin: 14px 0 4px; }
  #usagePanel .usageTotal { font-size: 14px; margin: 0 0 4px; }
  #usagePanel table { border-collapse: collapse; font-size: 14px; }
  #usagePanel th, #usagePanel td { padding: 4px 10px 4px 0;
               text-align: right; }
  #usagePanel th:first-child, #usagePanel td:first-child { text-align: left; }
  .notifyRow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               padding: 6px 0; font-size: 14px; }
  .notifyRow .tname { min-width: 8em; }
  .notifyRow button { padding: 4px 12px; font-size: 13px;
               background: #fff; color: __COCOA__; border: 1px solid __COCOA__; }
  .adminNew input[type="time"] { padding: 4px; }
  #approvalPanel { background: #fff; border-bottom: 2px solid __HONEY__;
                 padding: 12px 16px; }
  #approvalPanel[hidden] { display: none; }
  #approvalPanel h2 { font-size: 15px; margin: 0 0 8px; }
  .approvalRow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               padding: 8px 0; border-top: 1px dashed __HONEY__; font-size: 14px; }
  .approvalRow .asummary { flex: 1; min-width: 12em; word-break: break-all; }
  .approvalRow button { padding: 6px 14px; font-size: 13px; }
  .approvalRow button.deny { background: #fff; color: __COCOA__;
               border: 1px solid __COCOA__; }
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
  .quote-btn, .speak-btn { display: inline-block; margin-top: 6px; padding: 2px 8px;
               font-size: 12px; background: transparent; color: inherit;
               border: 1px solid currentColor; border-radius: 999px; }
  .quote-btn { margin-right: 6px; }
  #record.recording { background: #B3402A; color: #fff; border-color: #B3402A; }
  #recordStatus { font-size: 13px; align-self: center; }
  #recordStatus[hidden] { display: none; }
  form { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 16px; background: #fff;
         border-top: 1px solid __HONEY__; }
  #quoteBox { flex-basis: 100%; display: flex; gap: 8px; align-items: center;
              font-size: 13px; background: __CREAM__; padding: 6px 8px;
              border-left: 4px solid __HONEY__; }
  #quoteBox[hidden] { display: none; }
  #quoteText { flex: 1; overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; }
  #clearQuote { padding: 4px 10px; font-size: 12px; }
  #statusBox { flex-basis: 100%; display: flex; gap: 2px; align-items: center;
               font-size: 13px; color: __COCOA__; min-height: 1em; }
  #statusBox[hidden] { display: none; }
  #statusBox .dot { animation: statusBlink 1.2s infinite; }
  #statusBox .dot:nth-child(2) { animation-delay: .2s; }
  #statusBox .dot:nth-child(3) { animation-delay: .4s; }
  @keyframes statusBlink { 0%, 60%, 100% { opacity: .15; } 30% { opacity: 1; } }
  #files { flex-basis: 100%; font-size: 13px; min-height: 1em; }
  #files:empty { display: none; }
  #input { flex: 1; padding: 10px; font-size: 16px; border: 1px solid __COCOA__;
           border-radius: 8px; }
  button { padding: 10px 20px; font-size: 16px; border: none; border-radius: 8px;
           background: __HONEY__; color: #fff; cursor: pointer; }
  button:disabled { opacity: .5; }
  #attach, #record { background: #fff; color: __COCOA__; border: 1px solid __COCOA__; }
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
    <div class="provider" id="modeName"></div>
  </div>
  <div class="sessions">
    <label for="tenantSelect">テナント</label>
    <select id="tenantSelect"></select>
    <input id="newTenant" placeholder="新しいテナント" size="10">
    <button id="createTenant" type="button">作成</button>
  </div>
  <div class="sessions">
    <label for="userSelect">ユーザー</label>
    <select id="userSelect"></select>
    <input id="newUser" placeholder="新しいユーザー" size="10">
    <button id="createUser" type="button">作成</button>
  </div>
  <div class="sessions" id="sessionBox">
    <label for="sessionSelect" id="sessionLabel">部屋</label>
    <select id="sessionSelect"></select>
    <input id="newSession" placeholder="新しい部屋" size="10">
    <button id="createSession" type="button">作成</button>
    <button id="deleteSession" type="button">削除</button>
  </div>
  <div class="sessions">
    <button id="tenantAdminBtn" type="button">テナント管理</button>
    <button id="outputsBtn" type="button">成果物</button>
    <button id="profileBtn" type="button">人格・記憶</button>
    <button id="usageBtn" type="button">使用量</button>
    <button id="exportBtn" type="button">書き出し</button>
    __LOGOUT_BUTTON__
  </div>
</header>
<section id="outputsPanel" hidden>
  <h2>成果物</h2>
  <div id="outputRows"></div>
</section>
<section id="profilePanel" hidden>
  <h2>人格・記憶</h2>
  <p class="profileNote">クマの性格や長期記憶を編集できます。保存した内容は、新しい会話から反映されます（今開いている会話には影響しません）。</p>
  <div id="profileTabs"></div>
  <textarea id="profileText" rows="14" spellcheck="false"></textarea>
  <div class="profileActions">
    <button id="profileSave" type="button">保存</button>
    <span id="profileStatus"></span>
  </div>
</section>
<section id="usagePanel" hidden>
  <h2>使用量</h2>
  <p class="usageTotal" id="usageTotal"></p>
  <h3>頭脳別</h3>
  <div id="usageProviderRows"></div>
  <h3>日別（直近30日）</h3>
  <div id="usageDayRows"></div>
</section>
<section id="tenantPanel" hidden>
  <h2>テナント管理</h2>
  <div id="tenantRows"></div>
  <div class="adminNew">
    <input id="adminNewTenant" placeholder="新しいテナント名" size="14">
    <button id="adminCreateTenant" type="button">テナントを作成</button>
  </div>
  <h3>知らせ方の好み（現在のユーザー）</h3>
  <div id="notifyMutedRows"></div>
  <div class="adminNew">
    <input id="notifyNewTopic" placeholder="知らせない話題" size="14">
    <button id="notifyAddTopic" type="button">追加</button>
  </div>
  <div class="adminNew">
    <label>静かな時間帯:
      <input id="notifyQuietStart" type="time"> 〜
      <input id="notifyQuietEnd" type="time"></label>
    <button id="notifySaveQuiet" type="button">時間帯を保存</button>
    <button id="notifyClearQuiet" type="button">時間帯を解除</button>
  </div>
</section>
<section id="approvalPanel" hidden>
  <h2>承認の確認</h2>
  <div id="approvalRows"></div>
</section>
<div id="log"></div>
<div id="dropHint">ファイルを離すと添付します</div>
<form id="form">
  <div id="statusBox" hidden><span class="dot">・</span><span class="dot">・</span><span class="dot">・</span><span id="statusText"></span></div>
  <div id="quoteBox" hidden>
    <span id="quoteText"></span>
    <button id="clearQuote" type="button">解除</button>
  </div>
  <div id="files"></div>
  <button id="attach" type="button">添付</button>
  <button id="record" type="button">録音</button>
  <span id="recordStatus" hidden></span>
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
const recordBtn = document.getElementById('record');
const recordStatus = document.getElementById('recordStatus');
const fileInput = document.getElementById('file');
const filesLabel = document.getElementById('files');
const tenantSelect = document.getElementById('tenantSelect');
const newTenantInput = document.getElementById('newTenant');
const createTenantBtn = document.getElementById('createTenant');
const userSelect = document.getElementById('userSelect');
const newUserInput = document.getElementById('newUser');
const createUserBtn = document.getElementById('createUser');
const sessionBox = document.getElementById('sessionBox');
const sessionLabel = document.getElementById('sessionLabel');
const sessionSelect = document.getElementById('sessionSelect');
const newSessionInput = document.getElementById('newSession');
const createSessionBtn = document.getElementById('createSession');
const deleteSessionBtn = document.getElementById('deleteSession');
const tenantAdminBtn = document.getElementById('tenantAdminBtn');
const tenantPanel = document.getElementById('tenantPanel');
const outputsBtn = document.getElementById('outputsBtn');
const outputsPanel = document.getElementById('outputsPanel');
const outputRows = document.getElementById('outputRows');
const profileBtn = document.getElementById('profileBtn');
const profilePanel = document.getElementById('profilePanel');
const usageBtn = document.getElementById('usageBtn');
const exportBtn = document.getElementById('exportBtn');
const usagePanel = document.getElementById('usagePanel');
const usageTotal = document.getElementById('usageTotal');
const usageProviderRows = document.getElementById('usageProviderRows');
const usageDayRows = document.getElementById('usageDayRows');
const profileTabs = document.getElementById('profileTabs');
const profileText = document.getElementById('profileText');
const profileSaveBtn = document.getElementById('profileSave');
const profileStatus = document.getElementById('profileStatus');
const profileFiles = [
  {name: 'SOUL.md', label: '性格（SOUL.md）'},
  {name: 'IDENTITY.md', label: '自己紹介（IDENTITY.md）'},
  {name: 'USER.md', label: 'あなたについて（USER.md）'},
  {name: 'MEMORY.md', label: '長期記憶（MEMORY.md）'},
];
let currentProfileFile = 'SOUL.md';
const tenantRows = document.getElementById('tenantRows');
const approvalPanel = document.getElementById('approvalPanel');
const approvalRows = document.getElementById('approvalRows');
const adminNewTenant = document.getElementById('adminNewTenant');
const adminCreateTenant = document.getElementById('adminCreateTenant');
const notifyMutedRows = document.getElementById('notifyMutedRows');
const notifyNewTopic = document.getElementById('notifyNewTopic');
const notifyAddTopic = document.getElementById('notifyAddTopic');
const notifyQuietStart = document.getElementById('notifyQuietStart');
const notifyQuietEnd = document.getElementById('notifyQuietEnd');
const notifySaveQuiet = document.getElementById('notifySaveQuiet');
const notifyClearQuiet = document.getElementById('notifyClearQuiet');
const quoteBox = document.getElementById('quoteBox');
const quoteText = document.getElementById('quoteText');
const clearQuoteBtn = document.getElementById('clearQuote');
const statusBox = document.getElementById('statusBox');
const statusText = document.getElementById('statusText');
let pendingFiles = [];
let pendingQuote = null;
let mediaRecorder = null;
let recordedChunks = [];
let speakingBtn = null;
let currentTenant = 'default';
let currentUser = 'default';
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
  if (role === 'assistant') {
    const speakBtn = document.createElement('button');
    speakBtn.type = 'button';
    speakBtn.className = 'speak-btn';
    speakBtn.textContent = '読み上げ';
    speakBtn.addEventListener('click', () => toggleSpeak(speakBtn, text));
    div.appendChild(speakBtn);
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

// 読み上げ: ブラウザの speechSynthesis（ja-JP）で読む。読み上げ中に
// 同じボタンか別の発話のボタンを押したら、いったん止めてから
// （別の発話なら）そちらを読む。サーバー側の処理は無い
function stopSpeaking() {
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  if (speakingBtn) {
    speakingBtn.textContent = '読み上げ';
    speakingBtn = null;
  }
}

function toggleSpeak(btn, text) {
  if (!('speechSynthesis' in window) || !text) {
    window.alert('このブラウザでは読み上げに対応していません');
    return;
  }
  if (speakingBtn === btn) {
    stopSpeaking();
    return;
  }
  stopSpeaking();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = 'ja-JP';
  const done = () => {
    if (speakingBtn === btn) {
      btn.textContent = '読み上げ';
      speakingBtn = null;
    }
  };
  utterance.onend = done;
  utterance.onerror = done;
  speakingBtn = btn;
  btn.textContent = '止める';
  window.speechSynthesis.speak(utterance);
}

function describe(text, names) {
  if (!names || !names.length) return text;
  const suffix = names.map((n) => '[添付: ' + n + ']').join(' ');
  return text ? text + ' ' + suffix : suffix;
}

async function loadHistory() {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser)
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
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/sessions' + query);
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

// ユーザーの切り替え。ユーザーが変われば、そのユーザーの部屋一覧と
// 履歴に切り替える（部屋名は引き継ぎ、無ければ先頭の部屋にする）
async function applyUser(name) {
  currentUser = name;
  userSelect.value = name;
  await loadSessions();
  applySession(currentSession);
}

async function loadUsers() {
  try {
    const res = await fetch('/api/users?tenant=' + encodeURIComponent(currentTenant));
    const data = await res.json();
    userSelect.innerHTML = '';
    for (const name of data.users) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name;
      userSelect.appendChild(opt);
    }
    if (!data.users.includes(currentUser)) {
      currentUser = (data.current && data.users.includes(data.current))
        ? data.current : (data.users[0] || 'default');
    }
    userSelect.value = currentUser;
  } catch (e) { /* 一覧が取れなくても既定のユーザーで続けられる */ }
}

// テナントの切り替え。テナントが変われば、そのテナントのユーザー一覧・
// 部屋一覧と履歴に切り替える（ユーザー名・部屋名は引き継ぎ、
// 無ければそのテナントの既定にする）
async function applyTenant(name) {
  currentTenant = name;
  tenantSelect.value = name;
  await loadUsers();
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

userSelect.addEventListener('change', () => {
  if (userSelect.value && userSelect.value !== currentUser) {
    applyUser(userSelect.value);
  }
});
createUserBtn.addEventListener('click', () => {
  const name = newUserInput.value.trim();
  if (!name) return;
  newUserInput.value = '';
  if (![...userSelect.options].some((o) => o.value === name)) {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    userSelect.appendChild(opt);
  }
  applyUser(name);
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

// 選択中のスレッド（セッション）の記録を削除する。確認を出してから
// サーバーに消してもらい、一覧を取り直す。表示中だったスレッドを
// 消した場合は main の表示に戻る（記録は次に開けば空で再開する）
deleteSessionBtn.addEventListener('click', async () => {
  const name = sessionSelect.value || currentSession;
  if (!name) return;
  if (!window.confirm('「' + name + '」の記録を削除します。よろしいですか？')) return;
  try {
    const res = await fetch('/api/sessions/delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session: name, tenant: currentTenant,
                            user: currentUser}),
    });
    if (!res.ok) throw new Error('delete failed');
  } catch (e) {
    window.alert('削除に失敗しました');
    return;
  }
  const wasCurrent = name === currentSession;
  if (wasCurrent) currentSession = 'main';
  await loadSessions();
  if (wasCurrent) applySession('main');
});

// テナント管理パネル。テナントごとに能動発信の ON/OFF と保持期限
// （日数）を編集でき、新しいテナントの作成もできる。設定の保存は
// /api/tenant_settings、作成は /api/tenants/create を使う
tenantAdminBtn.addEventListener('click', () => {
  tenantPanel.hidden = !tenantPanel.hidden;
  if (!tenantPanel.hidden) loadTenantAdmin();
});

// 成果物パネル。現在のテナント×ユーザーの成果物を一覧し、各行から
// ダウンロードできる。パネルを開くたびに /api/outputs で一覧を
// 取り直す（会話中に新しい成果物が登録されるため）
outputsBtn.addEventListener('click', () => {
  outputsPanel.hidden = !outputsPanel.hidden;
  if (!outputsPanel.hidden) loadOutputs();
});

// 人格・記憶パネル。現在のテナント×ユーザーの人格ファイル
// （SOUL / IDENTITY / USER）と長期記憶（MEMORY）を切り替えて
// 編集する。読み書きは /api/profile を使い、保存した内容は
// 新しい会話から反映される（パネル内の案内文のとおり）
profileBtn.addEventListener('click', () => {
  profilePanel.hidden = !profilePanel.hidden;
  if (!profilePanel.hidden) loadProfileFile(currentProfileFile);
});

// 使用量パネル。現在のテナント×ユーザーの頭脳の使用量（呼び出し
// 回数とトークン数）を合計・頭脳別・日別で表示する。読み取りは
// /api/usage を使い、パネルを開くたびに取り直す。費用への換算は
// しない（単価が頭脳ごとに異なるため、トークン数だけを見せる）
usageBtn.addEventListener('click', () => {
  usagePanel.hidden = !usagePanel.hidden;
  if (!usagePanel.hidden) loadUsage();
});

// 書き出しボタン。現在のテナント×ユーザーの全データを ZIP で
// ダウンロードする（/api/export。ZIP はサーバー側でメモリ上に
// 組み立てられ、サーバーにファイルは残らない）
exportBtn.addEventListener('click', () => {
  const query = '?tenant=' + encodeURIComponent(currentTenant)
    + '&user=' + encodeURIComponent(currentUser);
  const link = document.createElement('a');
  link.href = '/api/export' + query;
  document.body.appendChild(link);
  link.click();
  link.remove();
});

function formatCount(value) {
  return (Number(value) || 0).toLocaleString('ja-JP');
}

function usageTokenCells(totals) {
  // トークン数が記録されていない区分（CLI 系の頭脳など）は「—」
  if (!totals.has_tokens) return ['—', '—', '—'];
  return [formatCount(totals.input_tokens), formatCount(totals.output_tokens),
          formatCount(totals.total_tokens)];
}

function usageTable(rows, firstHeader) {
  const table = document.createElement('table');
  const head = document.createElement('tr');
  for (const label of [firstHeader, '呼び出し', '入力', '出力', '合計']) {
    const th = document.createElement('th');
    th.textContent = label;
    head.appendChild(th);
  }
  table.appendChild(head);
  for (const [label, totals] of rows) {
    const tr = document.createElement('tr');
    const name = document.createElement('td');
    name.textContent = label;
    tr.appendChild(name);
    const calls = document.createElement('td');
    calls.textContent = formatCount(totals.calls) + ' 回';
    tr.appendChild(calls);
    for (const text of usageTokenCells(totals)) {
      const td = document.createElement('td');
      td.textContent = text;
      tr.appendChild(td);
    }
    table.appendChild(tr);
  }
  return table;
}

async function loadUsage() {
  usageTotal.textContent = '';
  usageProviderRows.innerHTML = '';
  usageDayRows.innerHTML = '';
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/usage' + query);
    const data = await res.json();
    if (!data.calls) {
      usageTotal.textContent = '使用量の記録はまだありません';
      return;
    }
    usageTotal.textContent = '呼び出し ' + formatCount(data.calls) + ' 回'
      + (data.has_tokens
        ? '、入力 ' + formatCount(data.input_tokens)
          + '、出力 ' + formatCount(data.output_tokens)
          + '、合計 ' + formatCount(data.total_tokens) + ' トークン'
        : '（トークン数は記録されていません）');
    usageProviderRows.appendChild(usageTable(
      (data.by_provider || []).map((p) => [
        p.provider + (p.model ? ' / ' + p.model : ''), p]), '頭脳'));
    const days = (data.by_day || []).slice().reverse();
    usageDayRows.appendChild(usageTable(
      days.map((d) => [d.date, d]), '日付'));
  } catch (e) { /* 取れなければパネルは空のままにする */ }
}

function renderProfileTabs() {
  profileTabs.innerHTML = '';
  for (const file of profileFiles) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = file.label;
    if (file.name === currentProfileFile) btn.className = 'active';
    btn.addEventListener('click', () => loadProfileFile(file.name));
    profileTabs.appendChild(btn);
  }
}

async function loadProfileFile(name) {
  currentProfileFile = name;
  renderProfileTabs();
  profileStatus.textContent = '';
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser)
      + '&name=' + encodeURIComponent(name);
    const res = await fetch('/api/profile' + query);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '読み込めませんでした');
    profileText.value = data.content || '';
  } catch (e) {
    profileStatus.textContent = '読み込めませんでした: ' + (e.message || e);
  }
}

profileSaveBtn.addEventListener('click', async () => {
  try {
    const res = await fetch('/api/profile', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: currentProfileFile,
                            content: profileText.value,
                            tenant: currentTenant, user: currentUser}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '保存に失敗しました');
    profileStatus.textContent = '保存しました（新しい会話から反映されます）';
  } catch (e) {
    profileStatus.textContent = '保存に失敗しました: ' + (e.message || e);
  }
});

function formatOutputSize(size) {
  const bytes = Number(size) || 0;
  if (bytes < 1024) return bytes + ' B';
  const kb = bytes / 1024;
  if (kb < 1024) return kb.toFixed(1) + ' KB';
  return (kb / 1024).toFixed(1) + ' MB';
}

async function loadOutputs() {
  outputRows.innerHTML = '';
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/outputs' + query);
    const data = await res.json();
    const outputs = data.outputs || [];
    if (!outputs.length) {
      const row = document.createElement('div');
      row.className = 'outputRow';
      row.textContent = '成果物はまだありません';
      outputRows.appendChild(row);
      return;
    }
    for (const item of outputs) {
      const row = document.createElement('div');
      row.className = 'outputRow';
      const title = document.createElement('span');
      title.className = 'otitle';
      title.textContent = item.title || item.filename;
      row.appendChild(title);
      const name = document.createElement('span');
      name.className = 'oname';
      name.textContent = item.filename;
      row.appendChild(name);
      const meta = document.createElement('span');
      meta.className = 'ometa';
      meta.textContent = formatOutputSize(item.size)
        + (item.created_at ? ' / ' + String(item.created_at).replace('T', ' ').slice(0, 16) : '');
      row.appendChild(meta);
      const link = document.createElement('a');
      link.textContent = 'ダウンロード';
      link.href = '/api/outputs/download' + query
        + '&name=' + encodeURIComponent(item.filename);
      row.appendChild(link);
      outputRows.appendChild(row);
    }
  } catch (e) { /* 一覧が取れなければパネルは空のままにする */ }
}

async function loadTenantAdmin() {
  try {
    const res = await fetch('/api/tenants');
    const data = await res.json();
    tenantRows.innerHTML = '';
    for (const name of data.tenants) {
      tenantRows.appendChild(await buildTenantRow(name));
    }
  } catch (e) { /* 一覧が取れなければパネルは空のままにする */ }
  loadNotifyPrefs();
}

// 知らせ方の好み（現在のテナント×ユーザー分）。ミュート一覧の
// 表示・追加・削除と、静かな時間帯の入力・保存を扱う。読み書きは
// /api/notify_prefs を使う
async function postNotifyPrefs(body) {
  const res = await fetch('/api/notify_prefs', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({tenant: currentTenant, user: currentUser, ...body}),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || '保存に失敗しました');
  return data;
}

function renderNotifyPrefs(prefs) {
  notifyMutedRows.innerHTML = '';
  const topics = prefs.muted_topics || [];
  if (!topics.length) {
    const row = document.createElement('div');
    row.className = 'notifyRow';
    row.textContent = '知らせない話題はありません';
    notifyMutedRows.appendChild(row);
  }
  for (const topic of topics) {
    const row = document.createElement('div');
    row.className = 'notifyRow';
    const label = document.createElement('span');
    label.className = 'tname';
    label.textContent = topic;
    row.appendChild(label);
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.textContent = '削除';
    delBtn.addEventListener('click', async () => {
      try {
        renderNotifyPrefs(await postNotifyPrefs({remove_topic: topic}));
      } catch (e) {
        window.alert('削除に失敗しました: ' + (e.message || e));
      }
    });
    row.appendChild(delBtn);
    notifyMutedRows.appendChild(row);
  }
  notifyQuietStart.value = prefs.quiet_start || '';
  notifyQuietEnd.value = prefs.quiet_end || '';
}

async function loadNotifyPrefs() {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/notify_prefs' + query);
    renderNotifyPrefs(await res.json());
  } catch (e) { /* 好みが取れなければ節は空のままにする */ }
}

notifyAddTopic.addEventListener('click', async () => {
  const topic = notifyNewTopic.value.trim();
  if (!topic) return;
  try {
    renderNotifyPrefs(await postNotifyPrefs({add_topic: topic}));
    notifyNewTopic.value = '';
  } catch (e) {
    window.alert('追加に失敗しました: ' + (e.message || e));
  }
});

notifySaveQuiet.addEventListener('click', async () => {
  if (!notifyQuietStart.value || !notifyQuietEnd.value) {
    window.alert('静かな時間帯は開始と終了の両方を入れてください');
    return;
  }
  try {
    renderNotifyPrefs(await postNotifyPrefs(
      {quiet_start: notifyQuietStart.value,
       quiet_end: notifyQuietEnd.value}));
    window.alert('静かな時間帯を保存しました');
  } catch (e) {
    window.alert('保存に失敗しました: ' + (e.message || e));
  }
});

notifyClearQuiet.addEventListener('click', async () => {
  try {
    renderNotifyPrefs(await postNotifyPrefs(
      {quiet_start: null, quiet_end: null}));
  } catch (e) {
    window.alert('解除に失敗しました: ' + (e.message || e));
  }
});

async function buildTenantRow(name) {
  const row = document.createElement('div');
  row.className = 'tenantRow';
  const label = document.createElement('span');
  label.className = 'tname';
  label.textContent = name;
  row.appendChild(label);
  let settings = {};
  try {
    const res = await fetch(
      '/api/tenant_settings?tenant=' + encodeURIComponent(name));
    settings = await res.json();
  } catch (e) { /* 設定が取れなければ既定の表示で続ける */ }
  const proactiveLabel = document.createElement('label');
  const toggle = document.createElement('input');
  toggle.type = 'checkbox';
  toggle.checked = settings.proactive_enabled !== false;
  proactiveLabel.appendChild(toggle);
  proactiveLabel.appendChild(document.createTextNode(' 能動発信'));
  row.appendChild(proactiveLabel);
  const approvalLabel = document.createElement('label');
  const approvalToggle = document.createElement('input');
  approvalToggle.type = 'checkbox';
  approvalToggle.checked = settings.approvals_enabled !== false;
  approvalLabel.appendChild(approvalToggle);
  approvalLabel.appendChild(document.createTextNode(' 承認確認'));
  row.appendChild(approvalLabel);
  const retentionLabel = document.createElement('label');
  retentionLabel.appendChild(document.createTextNode('保持期限（日）: '));
  const days = document.createElement('input');
  days.type = 'number';
  days.min = '0';
  if (settings.retention_days != null) days.value = settings.retention_days;
  if (settings.retention_effective_days != null) {
    days.placeholder = '既定 ' + settings.retention_effective_days;
  }
  retentionLabel.appendChild(days);
  row.appendChild(retentionLabel);
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.textContent = '保存';
  saveBtn.addEventListener('click', async () => {
    const body = {tenant: name, proactive_enabled: toggle.checked,
                  approvals_enabled: approvalToggle.checked,
                  retention_days: days.value === '' ? null : Number(days.value)};
    try {
      const res = await fetch('/api/tenant_settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error('save failed');
      window.alert('「' + name + '」の設定を保存しました');
      loadTenantAdmin();
    } catch (e) {
      window.alert('保存に失敗しました');
    }
  });
  row.appendChild(saveBtn);
  const switchBtn = document.createElement('button');
  switchBtn.type = 'button';
  switchBtn.textContent = '切り替え';
  switchBtn.addEventListener('click', () => applyTenant(name));
  row.appendChild(switchBtn);
  // 削除は取り消せないため既定テナントには出さず、確認として
  // テナント名の入力を求める（一致しない場合は進めない）。実際の
  // 削除は /api/tenants/delete が担い、サーバー側で控えを取って
  // から置き場を消す
  if (name !== 'default') {
    const deleteBtn = document.createElement('button');
    deleteBtn.type = 'button';
    deleteBtn.textContent = '削除';
    deleteBtn.addEventListener('click', async () => {
      const typed = window.prompt(
        'テナント「' + name + '」を削除します。'
        + 'データは控え（ZIP）を取ってから消え、取り消せません。'
        + '確認のため、テナント名を入力してください');
      if (typed === null) return;
      if (typed.trim() !== name) {
        window.alert('テナント名が一致しないため、削除しませんでした');
        return;
      }
      let data = {};
      try {
        const res = await fetch('/api/tenants/delete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({tenant: name}),
        });
        data = await res.json();
        if (!res.ok) throw new Error(data.error || '削除に失敗しました');
      } catch (e) {
        window.alert('削除に失敗しました: ' + (e.message || e));
        return;
      }
      window.alert('テナント「' + name + '」を削除しました'
        + (data.backup ? '（控え: ' + data.backup + '）' : ''));
      const wasCurrent = currentTenant === name;
      await loadTenants();
      await loadTenantAdmin();
      if (wasCurrent) applyTenant('default');
    });
    row.appendChild(deleteBtn);
  }
  return row;
}

adminCreateTenant.addEventListener('click', async () => {
  const name = adminNewTenant.value.trim();
  if (!name) return;
  let data = {};
  try {
    const res = await fetch('/api/tenants/create', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name}),
    });
    data = await res.json();
    if (!res.ok) throw new Error(data.error || '作成に失敗しました');
  } catch (e) {
    window.alert('テナントを作成できませんでした: ' + (e.message || e));
    return;
  }
  adminNewTenant.value = '';
  await loadTenants();
  await loadTenantAdmin();
  applyTenant(data.tenant || name);
});

function setPending(files) {
  pendingFiles = Array.from(files);
  filesLabel.textContent = pendingFiles.length
    ? '添付: ' + pendingFiles.map((f) => f.name).join('、') : '';
}

attachBtn.addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => setPending(fileInput.files));

// 録音: ブラウザの MediaRecorder でマイクから録り、止めたら音声
// ファイルとして通常の添付（pendingFiles）に合流させる。送信は
// 既存の送信処理がそのまま行う。マイクの許可が得られない場合や
// ブラウザが未対応の場合は、状態表示にエラーを出して会話は壊さない
function setRecording(on) {
  recordBtn.textContent = on ? '録音を止める' : '録音';
  recordBtn.classList.toggle('recording', on);
  recordStatus.hidden = !on;
  recordStatus.textContent = on ? '録音中…' : '';
}

function showRecordError(message) {
  mediaRecorder = null;
  recordBtn.textContent = '録音';
  recordBtn.classList.remove('recording');
  recordStatus.hidden = false;
  recordStatus.textContent = message;
}

recordBtn.addEventListener('click', async () => {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    return;
  }
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    showRecordError('このブラウザでは録音に対応していません');
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: true});
  } catch (e) {
    showRecordError('マイクを使えませんでした（許可が必要です）');
    return;
  }
  recordedChunks = [];
  try {
    mediaRecorder = new MediaRecorder(stream);
  } catch (e) {
    stream.getTracks().forEach((t) => t.stop());
    showRecordError('録音を開始できませんでした');
    return;
  }
  mediaRecorder.ondataavailable = (ev) => {
    if (ev.data && ev.data.size) recordedChunks.push(ev.data);
  };
  mediaRecorder.onstop = () => {
    stream.getTracks().forEach((t) => t.stop());
    const mime = (mediaRecorder.mimeType || 'audio/webm').split(';')[0];
    const ext = mime.includes('ogg') ? '.ogg'
      : mime.includes('mp4') ? '.m4a'
      : mime.includes('mpeg') ? '.mp3'
      : mime.includes('wav') ? '.wav' : '.webm';
    const file = new File(recordedChunks,
                          'recording-' + Date.now() + ext, {type: mime});
    mediaRecorder = null;
    recordedChunks = [];
    setRecording(false);
    setPending([...pendingFiles, file]);
  };
  mediaRecorder.start();
  setRecording(true);
});

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

// 作業中表示: 送信中は /api/status をポーリングして「今なにを
// しているか」を入力欄の上に出し、応答が返ったら消す
function showStatus(text) {
  if (text) {
    statusText.textContent = text;
    statusBox.hidden = false;
  } else {
    statusText.textContent = '';
    statusBox.hidden = true;
  }
}

async function pollStatus() {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser)
      + '&session=' + encodeURIComponent(currentSession);
    const res = await fetch('/api/status' + query);
    const data = await res.json();
    showStatus(data.status || '');
  } catch (e) { /* 状態が取れなくても会話は続けられる */ }
  pollApprovals();
}

// 承認の確認: 危険な道具の実行待ちがあれば、内容と許可・却下の
// ボタンを出す。承認待ちは会話リクエストの処理中にだけ現れる
// ため、状態ポーリングに相乗りして取りにいく。決定は
// /api/approvals/decide に送り、待ち中の会話が先へ進む
async function pollApprovals() {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/approvals' + query);
    const data = await res.json();
    renderApprovals(data.approvals || []);
  } catch (e) { /* 取れなければ前回の表示のままにする */ }
}

function renderApprovals(list) {
  approvalRows.innerHTML = '';
  approvalPanel.hidden = !list.length;
  for (const item of list) {
    const row = document.createElement('div');
    row.className = 'approvalRow';
    const text = document.createElement('span');
    text.className = 'asummary';
    text.textContent = '「' + item.tool + '」を実行しようとしています'
      + (item.summary ? ': ' + item.summary : '');
    row.appendChild(text);
    row.appendChild(approvalButton(item, true, '許可'));
    row.appendChild(approvalButton(item, false, '却下'));
    approvalRows.appendChild(row);
  }
}

function approvalButton(item, approved, label) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = label;
  if (!approved) btn.className = 'deny';
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      const res = await fetch('/api/approvals/decide', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({id: item.id, approved,
                              tenant: currentTenant, user: currentUser}),
      });
      if (!res.ok) throw new Error('decide failed');
    } catch (e) {
      window.alert('承認の決定に失敗しました');
    }
    pollApprovals();
  });
  return btn;
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
  showStatus('考え中');
  const poller = setInterval(pollStatus, 400);
  try {
    const attachments = await Promise.all(files.map(readAttachment));
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: text, attachments, quote,
                            session: currentSession, tenant: currentTenant,
                            user: currentUser}),
    });
    const data = await res.json();
    addMsg('assistant', data.reply || ('エラー: ' + (data.error || res.status)));
  } catch (e) {
    addMsg('assistant', '通信に失敗しました');
  } finally {
    clearInterval(poller);
    showStatus('');
    pollApprovals();
    send.disabled = false;
    input.focus();
  }
});

// 起動時のメタ情報。モードの表示名をヘッダーに出し、相棒
// モード（ledger）では部屋は 1 つに固定されるため、スレッドの
// 選択・作成・削除 UI 自体を出さない（履歴と送信は固定の部屋で
// 動く）。スレッドモード（wall）では呼称を「部屋」から
// 「スレッド」に切り替える
const modeName = document.getElementById('modeName');
async function loadMeta() {
  try {
    const res = await fetch('/api/meta');
    const data = await res.json();
    if (data.mode_label) {
      modeName.textContent = '分け方: ' + data.mode_label;
    }
    if (data.mode === 'ledger') {
      sessionBox.hidden = true;
    } else if (data.mode === 'wall') {
      sessionLabel.textContent = 'スレッド';
      newSessionInput.placeholder = '新しいスレッド';
    }
  } catch (e) { /* 取れなければ従来どおり全部出す */ }
}

(async () => {
  await loadMeta();
  await loadTenants();
  await loadUsers();
  await loadSessions();
  loadHistory();
})();
input.focus();
</script>
</body>
</html>
"""


def render_page(provider: str, avatar_svg: str | None = None,
                web_auth: bool = False) -> str:
    """チャット画面の HTML を組み立てる。

    web_auth が True（ログイン有効）のときだけ、ヘッダーに
    ログアウトボタンを出す。無効時は従来の見た目のままにする。
    """
    return (
        _PAGE_TEMPLATE
        .replace("__AVATAR__", avatar_svg if avatar_svg is not None else load_avatar_svg())
        .replace("__PROVIDER__", provider)
        .replace("__LOGOUT_BUTTON__",
                 _LOGOUT_BUTTON_HTML if web_auth else "")
        .replace("__HONEY__", HONEY)
        .replace("__COCOA__", COCOA)
        .replace("__CREAM__", CREAM)
    )


# ログイン有効時にヘッダーへ差し込むログアウトボタン。押すと
# POST /api/logout でトークンを破棄してからログイン画面へ移る
_LOGOUT_BUTTON_HTML = """\
<button id="logoutBtn" type="button">ログアウト</button>
    <script>
    document.getElementById('logoutBtn').addEventListener('click', async () => {
      try { await fetch('/api/logout', {method: 'POST'}); } catch (e) { /* 失敗してもログイン画面へは移る */ }
      location.href = '/login';
    });
    </script>"""


_LOGIN_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ログイン - kuma-agent</title>
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: sans-serif; background: __CREAM__; color: __COCOA__;
         display: flex; align-items: center; justify-content: center; min-height: 100dvh; }
  .card { background: #fff; border: 1px solid __HONEY__; border-radius: 14px;
          padding: 24px; width: min(360px, 90vw); text-align: center; }
  .card .avatar { width: 64px; height: 64px; margin: 0 auto 8px; }
  .card .avatar svg { width: 100%; height: 100%; display: block; }
  .card h1 { font-size: 20px; margin: 0 0 4px; }
  .card p { font-size: 14px; margin: 0 0 16px; }
  .card .error { color: #B3402A; min-height: 1.2em; }
  .card form { display: flex; flex-direction: column; gap: 10px; }
  .card input { padding: 10px; font-size: 16px; border: 1px solid __COCOA__;
                border-radius: 8px; }
  .card button { padding: 10px 20px; font-size: 16px; border: none; border-radius: 8px;
                 background: __HONEY__; color: #fff; cursor: pointer; }
</style>
</head>
<body>
  <div class="card">
    <div class="avatar">__AVATAR__</div>
    <h1>クマ</h1>
    <p>続けるにはパスワードを入力してください</p>
    <p class="error">__ERROR__</p>
    <form method="post" action="/login">
      <input type="password" name="password" autocomplete="current-password"
             placeholder="パスワード" autofocus>
      <button type="submit">ログイン</button>
    </form>
  </div>
</body>
</html>
"""


def render_login_page(error: str = "", avatar_svg: str | None = None) -> str:
    """ログイン画面の HTML を組み立てる。error は失敗時の文言。"""
    return (
        _LOGIN_TEMPLATE
        .replace("__AVATAR__", avatar_svg if avatar_svg is not None else load_avatar_svg())
        .replace("__ERROR__", error)
        .replace("__HONEY__", HONEY)
        .replace("__COCOA__", COCOA)
        .replace("__CREAM__", CREAM)
    )


class AuthGate:
    """Web UI のログイン（アクセス制御）を担う。

    運用者がパスワード（Config.web_password、環境変数
    KUMA_WEB_PASSWORD）を設定したときだけ有効になる。未設定なら
    enabled が False で、ハンドラは従来どおり素通しする。

    ログインに成功したら推測できないトークンを発行し、Cookie と
    対応付けてメモリ上にだけ保持する。トークンはサーバーの再起動で
    すべて失効する（その場合は再ログインが必要）。パスワードの
    連続失敗は同一 IP ごとに数え、上限（MAX_FAILURES 回）に達したら
    LOCKOUT_SECONDS 秒のあいだ、その IP からのログインを受け付け
    ない（その間は正しいパスワードでも通さない）。
    """

    COOKIE_NAME = "kuma_session"
    TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 日
    MAX_FAILURES = 5
    LOCKOUT_SECONDS = 60

    def __init__(self, password: str, secure_cookie: bool = False) -> None:
        self._password = password
        self.secure_cookie = secure_cookie
        self._tokens: dict[str, float] = {}  # トークン → 失効時刻
        self._failures: dict[str, int] = {}  # IP → 連続失敗の回数
        self._lockouts: dict[str, float] = {}  # IP → 受付を再開する時刻
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._password)

    def try_login(self, ip: str, candidate: str) -> str | None:
        """パスワードを照合し、合えば新しいトークンを返す。

        失敗した場合と、連続失敗で受け付けを止めている間は None
        を返す。照合は hmac.compare_digest で行う。
        """
        if not self._password:
            return None
        now = time.time()
        with self._lock:
            if now < self._lockouts.get(ip, 0.0):
                return None
            if hmac.compare_digest(candidate, self._password):
                self._failures.pop(ip, None)
                token = secrets.token_urlsafe(32)
                self._tokens[token] = now + self.TOKEN_TTL_SECONDS
                return token
            count = self._failures.get(ip, 0) + 1
            if count >= self.MAX_FAILURES:
                self._lockouts[ip] = now + self.LOCKOUT_SECONDS
                self._failures.pop(ip, None)
            else:
                self._failures[ip] = count
            return None

    def validate(self, token: str | None) -> bool:
        """トークンが有効（発行済みで失効前）かどうかを返す。"""
        if not token:
            return False
        now = time.time()
        with self._lock:
            expiry = self._tokens.get(token)
            if expiry is None:
                return False
            if expiry <= now:  # 失効していたら掃除して無効とする
                del self._tokens[token]
                return False
            return True

    def revoke(self, token: str | None) -> None:
        """トークンを破棄する（ログアウト）。無いトークンでもよい。"""
        if token:
            with self._lock:
                self._tokens.pop(token, None)

    def session_cookie(self, token: str) -> str:
        """ログイン用 Cookie の Set-Cookie ヘッダー値を組み立てる。"""
        return self._cookie(token, self.TOKEN_TTL_SECONDS)

    def cleared_cookie(self) -> str:
        """Cookie を消すための Set-Cookie ヘッダー値（即時失効）。"""
        return self._cookie("", 0)

    def _cookie(self, value: str, max_age: int) -> str:
        parts = [f"{self.COOKIE_NAME}={value}",
                 f"Max-Age={max_age}", "Path=/", "HttpOnly", "SameSite=Lax"]
        if self.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)


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
        # ブラウザの録音は "audio/webm;codecs=opus" のような種別を
        # 送ることがあるため、パラメータ部分は落としておく
        media_type = str(item.get("media_type") or "").split(";")[0].strip()
        media_type = media_type or guess_media_type(name)
        try:
            data = base64.b64decode(str(item["data_base64"]), validate=True)
        except (KeyError, binascii.Error) as exc:
            raise ValueError("添付の base64 が不正です") from exc
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"添付が大きすぎます（上限 {MAX_ATTACHMENT_BYTES} バイト）: {name}")
        attachments.append(Attachment(name=name, media_type=media_type, data=data))
    return attachments


class ApprovalBroker:
    """保留中の承認（危険な道具の実行許可）を保持し、決定を待つ。

    会話リクエストのスレッドが request() で承認待ちに入り、画面側が
    GET /api/approvals で保留一覧を見て、POST /api/approvals/decide
    で decide() を呼ぶまで待つ。待ちには上限（timeout 秒）があり、
    時間切れは却下として扱う。保留はテナント×ユーザー単位で分け、
    一覧と決定はその組み合わせの中だけで効く。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}

    def request(self, tenant: str, user: str, name: str, args: dict,
                timeout: float) -> bool:
        """承認を 1 件登録し、決定（または時間切れ）まで待つ。"""
        entry = {
            "id": uuid.uuid4().hex,
            "tenant": tenant,
            "user": user,
            "tool": name,
            "summary": summarize_tool_args(name, args),
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": threading.Event(),
            "approved": False,
        }
        with self._lock:
            self._pending[entry["id"]] = entry
        try:
            entry["event"].wait(timeout if timeout and timeout > 0 else None)
            return bool(entry["approved"])
        finally:
            with self._lock:
                self._pending.pop(entry["id"], None)

    def list_pending(self, tenant: str, user: str) -> list[dict]:
        """そのテナント×ユーザーの保留一覧（画面に出す項目だけ）。"""
        with self._lock:
            entries = [e for e in self._pending.values()
                       if e["tenant"] == tenant and e["user"] == user]
        entries.sort(key=lambda e: e["time"])
        return [{"id": e["id"], "tool": e["tool"], "summary": e["summary"],
                 "time": e["time"]} for e in entries]

    def decide(self, tenant: str, user: str, approval_id: str,
               approved: bool) -> bool:
        """保留中の承認を決定する。対象が無ければ False。"""
        with self._lock:
            entry = self._pending.get(approval_id)
            if (entry is None or entry["tenant"] != tenant
                    or entry["user"] != user):
                return False
            entry["approved"] = bool(approved)
        entry["event"].set()
        return True


class SessionStore:
    """名前 → Session の対応を管理する（Web UI の部屋の切り替え用）。

    初回アクセス時に factory で Session を作る。factory を省略すると
    最初の Session と同じ設定・モデルを共有して新しい部屋を作る。
    1 つの SessionStore は 1 テナント・1 ユーザー分だけを扱う。
    """

    def __init__(self, initial: Session,
                 factory: Callable[[str], Session] | None = None,
                 approvals: ApprovalBroker | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._factory = factory or (
            lambda name: Session(initial.cfg, initial.model, name))
        self.tenant = initial.tenant
        self.user = initial.user
        self._data_dir = initial.cfg.user_data_dir
        self._approvals = approvals
        self._lock = threading.Lock()
        # 部屋ごとの作業中表示（空文字列は「作業していない」）。
        # Session の状態コールバックをここへ差し込み、/api/status が読む
        self._statuses: dict[str, str] = {}
        self._track(initial)

    def _track(self, session: Session) -> None:
        """Session の状態コールバックをストアの保持先に差し込む。"""
        self._sessions[session.session_id] = session
        self._statuses.setdefault(session.session_id, "")

        def on_status(status: str, _id: str = session.session_id) -> None:
            with self._lock:
                self._statuses[_id] = status

        session.on_status = on_status
        if self._approvals is not None:
            # 承認者も差し込む: このストアのテナント×ユーザーの
            # 保留としてブローカーに登録し、画面の決定を待つ
            broker = self._approvals

            def approver(name: str, args: dict, _s: Session = session) -> bool:
                return broker.request(
                    _s.tenant, _s.user, name, args,
                    _s.cfg.approval_timeout_seconds)

            session.approver = approver

    def get(self, name: str | None) -> Session:
        session_id = normalize_session_id(name)
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = self._factory(session_id)
                self._track(session)
            return session

    def status(self, name: str | None) -> str:
        """部屋の現在の作業中表示（無ければ空文字列）。Session は作らない。"""
        session_id = normalize_session_id(name)
        with self._lock:
            return self._statuses.get(session_id, "")

    def discard(self, name: str | None) -> None:
        """開いている Session を破棄する（記録の削除後に使う）。

        次に get されたときに記録から作り直される。開いていなければ
        何もしない。記録ファイル自体は delete_session_record の仕事。
        """
        session_id = normalize_session_id(name)
        with self._lock:
            self._sessions.pop(session_id, None)
            self._statuses.pop(session_id, None)

    @property
    def data_dir(self) -> Path:
        """このストアが担当するユーザー置き場（記録の削除に使う）。"""
        return self._data_dir

    def names(self) -> list[str]:
        """開いている部屋と、記録が残っている部屋の一覧。"""
        with self._lock:
            loaded = list(self._sessions)
        return sorted(set(loaded) | set(list_session_ids(self._data_dir)))


class UserStore:
    """ユーザー → SessionStore の対応を管理する（1 テナント分）。

    テナントの中ではユーザーごとに SessionStore を持ち、初回アクセス
    時にそのユーザーの Session を作る。Session は make_session
    （ユーザー名と部屋名から Session を作る関数）で作るため、データの
    置き場はユーザーごとに分かれる。
    """

    def __init__(self, tenant: str, tenant_data_dir: Path,
                 default_user: str,
                 make_session: Callable[[str, str], Session],
                 initial: SessionStore | None = None,
                 approvals: ApprovalBroker | None = None) -> None:
        self.tenant = tenant
        self.default_user = default_user
        self._tenant_data_dir = tenant_data_dir
        self._make_session = make_session
        self._approvals = approvals
        self._stores: dict[str, SessionStore] = {}
        if initial is not None:
            self._stores[initial.user] = initial
        self._lock = threading.Lock()

    def store(self, user: str | None) -> SessionStore:
        name = normalize_user_name(user)
        with self._lock:
            store = self._stores.get(name)
            if store is None:
                initial = self._make_session(name, DEFAULT_SESSION_ID)
                store = SessionStore(initial, approvals=self._approvals)
                self._stores[name] = store
            return store

    def names(self) -> list[str]:
        """開いているユーザーと、保存済みユーザーの一覧。"""
        with self._lock:
            loaded = list(self._stores)
        return sorted(set(loaded) | set(list_user_names(self._tenant_data_dir)))


class TenantStore:
    """テナント → UserStore の対応を管理する（Web UI のテナント切替用）。

    サーバーはテナントごとに UserStore を持ち、その中ではユーザー
    ごとに SessionStore を持つ。初回アクセス時にその組み合わせの
    Session を作る。新しい組み合わせの Session は、起動時の設定
    （プロバイダやモデルなど）を引き継ぎ、テナントとユーザーだけを
    差し替えた Config で作るため、データの置き場は組み合わせごとに
    分かれる。session_factory は起動時テナント・起動時ユーザーの
    部屋の作り方だけを差し替える（テスト用）。
    """

    def __init__(self, initial: Session,
                 session_factory: Callable[[str], Session] | None = None,
                 ) -> None:
        self._cfg = initial.cfg
        self._model = initial.model
        self.default_tenant = initial.tenant
        self.default_user = initial.user
        # モードはサーバー起動時の設定で決まる（ページが /api/meta で知る）
        self.mode = normalize_mode(initial.cfg.mode)
        # 承認の保留はサーバー全体で 1 つのブローカーが持ち、
        # 全テナント×ユーザーの Session で共有する
        self.approvals = ApprovalBroker()
        seeded = UserStore(
            self.default_tenant, initial.cfg.tenant_data_dir,
            self.default_user, self._session_maker(self.default_tenant),
            initial=SessionStore(initial, session_factory,
                                 approvals=self.approvals),
            approvals=self.approvals)
        self._user_stores: dict[str, UserStore] = {self.default_tenant: seeded}
        self._lock = threading.Lock()

    def _session_maker(self, tenant: str) -> Callable[[str, str], Session]:
        def make(user: str, session_id: str) -> Session:
            cfg = replace(self._cfg,
                          tenant=normalize_tenant_name(tenant),
                          user=normalize_user_name(user))
            return Session(cfg, self._model, session_id)
        return make

    def _make_session(self, tenant: str, user: str, session_id: str) -> Session:
        return self._session_maker(tenant)(user, session_id)

    def users(self, tenant: str | None) -> UserStore:
        name = normalize_tenant_name(tenant)
        with self._lock:
            store = self._user_stores.get(name)
            if store is None:
                cfg = replace(self._cfg, tenant=name)
                store = UserStore(name, cfg.tenant_data_dir,
                                  self.default_user, self._session_maker(name),
                                  approvals=self.approvals)
                self._user_stores[name] = store
            return store

    def store(self, tenant: str | None, user: str | None = None) -> SessionStore:
        return self.users(tenant).store(user)

    def discard_tenant(self, tenant: str | None) -> None:
        """テナントの UserStore を破棄する（テナント削除後の後始末）。

        置き場を消した後も抱えたままにすると、一覧に消えたはずの
        テナントが残ったり、記録の保存で置き場が作り直されたり
        するため、削除の完了に合わせて落とす。起動時のテナントは
        サーバーの土台なので破棄しない（削除側でも拒否する）。
        """
        name = normalize_tenant_name(tenant)
        with self._lock:
            if name != self.default_tenant:
                self._user_stores.pop(name, None)

    def names(self) -> list[str]:
        """開いているテナントと、保存済みテナントの一覧。"""
        with self._lock:
            loaded = list(self._user_stores)
        return sorted(set(loaded) | set(list_tenant_names(self._cfg.data_dir)))

    @property
    def data_dir(self) -> Path:
        """データ置き場のルート（settings.json / retention.json の置き場）。"""
        return self._cfg.data_dir

    @property
    def retention_days(self) -> int:
        """全体の既定の保持期間（テナント別指定が無い場合の実効値）。"""
        return self._cfg.retention_days


def _notify_prefs_payload(tenants: TenantStore, tenant: str,
                          user: str) -> dict:
    """知らせ方の好み API の応答（GET / POST で共通の形）。

    好みはユーザーごとの置き場（notify_prefs.json）にあり、対象は
    tenant / user で指した組み合わせ。quiet_start / quiet_end は
    静かな時間帯が有効なときだけ "HH:MM"、無ければ None。
    """
    prefs = load_notify_prefs(
        user_dir(tenant_dir(tenants.data_dir, tenant), user))
    payload = prefs.to_dict()
    if not prefs.quiet_enabled:
        payload["quiet_start"] = None
        payload["quiet_end"] = None
    return {"tenant": tenant, "user": user, **payload}


def _outputs_payload(tenants: TenantStore, tenant: str,
                     user: str) -> dict:
    """成果物 API の応答（GET /api/outputs）。

    成果物はユーザーごとの置き場（outputs/ と index.json）にあり、
    対象は tenant / user で指した組み合わせ。size_text と
    created_text は表示用の整形済み文字列で、画面がそのまま
    出せるように添える。
    """
    board = OutputBoard(
        user_dir(tenant_dir(tenants.data_dir, tenant), user))
    outputs = []
    for output in board.list():
        entry = output.to_dict()
        entry["size_text"] = format_size(output.size)
        entry["created_text"] = format_created_at(output.created_at)
        outputs.append(entry)
    return {"tenant": tenant, "user": user, "outputs": outputs}


def _usage_payload(tenants: TenantStore, tenant: str,
                   user: str) -> dict:
    """使用量 API の応答（GET /api/usage）。

    使用量の記録はユーザーごとの置き場（usage.jsonl）にあり、対象
    は tenant / user で指した組み合わせ。集計（合計・頭脳／モデル
    別・日別）は usage.summarize_usage が担う。費用への換算は
    しない（単価が頭脳ごとに異なるため）。
    """
    summary = summarize_usage(
        user_dir(tenant_dir(tenants.data_dir, tenant), user))
    return {"tenant": tenant, "user": user, **summary.to_dict()}


def _tenant_settings_payload(tenants: TenantStore, name: str) -> dict:
    """テナント設定 API の応答（GET / POST で共通の形）。

    retention_days はそのテナントの指定値（未設定なら None）で、
    retention_effective_days は全体の既定まで含めて解決した実効値。
    """
    return {
        "tenant": name,
        "proactive_enabled": proactive_enabled_for(tenants.data_dir, name),
        "approvals_enabled": approvals_enabled_for(tenants.data_dir, name),
        "retention_days": load_retention_overrides(tenants.data_dir).get(name),
        "retention_effective_days": retention_days_for(
            tenants.data_dir, name, tenants.retention_days),
    }


def make_handler(tenants: TenantStore, page: str, auth: AuthGate):
    """TenantStore を受け取るリクエストハンドラクラスを作る。

    auth（AuthGate）が有効なときは、ログイン画面（/login）以外の
    全経路でログインを求める: 画面はログイン画面へリダイレクトし、
    API は 401 を返す。無効なときは従来どおり素通しする。
    """
    lock = threading.Lock()
    login_page = render_login_page()
    login_error_page = render_login_page(error="パスワードが違います")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # 標準のアクセスログを止める
            pass

        def _send(self, status: int, body: bytes, content_type: str,
                  extra_headers: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, obj: dict,
                       extra_headers: dict | None = None) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra_headers)

        def _redirect(self, location: str,
                      extra_headers: dict | None = None) -> None:
            headers = {"Location": location}
            headers.update(extra_headers or {})
            self._send(302, b"", "text/plain; charset=utf-8", headers)

        def _cookie_token(self) -> str | None:
            """リクエストの Cookie からセッショントークンを取り出す。"""
            raw = self.headers.get("Cookie")
            if not raw:
                return None
            try:
                jar = SimpleCookie(raw)
            except CookieError:
                return None
            morsel = jar.get(AuthGate.COOKIE_NAME)
            return morsel.value if morsel is not None else None

        def _authenticated(self) -> bool:
            """ログインが不要か、トークンが有効なら True。"""
            return not auth.enabled or auth.validate(self._cookie_token())

        def _login_page(self) -> None:
            """ログイン画面を出す。

            ログインが無効か、すでにログイン済みなら画面（/）へ戻す。
            """
            if not auth.enabled or self._authenticated():
                self._redirect("/")
                return
            self._send(200, login_page.encode("utf-8"),
                       "text/html; charset=utf-8")

        def _login(self) -> None:
            """パスワードを照合し、合えば Cookie を発行して画面へ戻す。

            間違っている場合と、連続失敗で受け付けを止めている間は
            401 でログイン画面（失敗の文言付き）を返す。
            """
            if not auth.enabled:
                self._redirect("/")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                form = {}
            candidate = _first(form.get("password")) or ""
            token = auth.try_login(self.client_address[0], candidate)
            if token is None:
                self._send(401, login_error_page.encode("utf-8"),
                           "text/html; charset=utf-8")
                return
            self._redirect("/", {"Set-Cookie": auth.session_cookie(token)})

        def _logout(self) -> None:
            """トークンを破棄し、Cookie を消してログアウトする。"""
            auth.revoke(self._cookie_token())
            self._send_json(200, {"logged_out": True},
                            {"Set-Cookie": auth.cleared_cookie()})

        def do_GET(self) -> None:
            split = urlsplit(self.path)
            query = parse_qs(split.query)
            if split.path == "/login":
                self._login_page()
                return
            if not self._authenticated():
                if split.path.startswith("/api/"):
                    self._send_json(401, {"error": "ログインが必要です"})
                else:
                    self._redirect("/login")
                return
            if split.path == "/":
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif split.path == "/api/meta":
                self._send_json(200, {"mode": tenants.mode,
                                      "mode_label": mode_label(tenants.mode),
                                      "tenant": tenants.default_tenant,
                                      "user": tenants.default_user})
            elif split.path == "/api/tenants":
                self._send_json(200, {"tenants": tenants.names(),
                                      "current": tenants.default_tenant})
            elif split.path == "/api/tenant_settings":
                name = normalize_tenant_name(_first(query.get("tenant")))
                self._send_json(200, _tenant_settings_payload(tenants, name))
            elif split.path == "/api/notify_prefs":
                tenant = normalize_tenant_name(
                    _first(query.get("tenant")) or tenants.default_tenant)
                user = normalize_user_name(
                    _first(query.get("user")) or tenants.default_user)
                self._send_json(
                    200, _notify_prefs_payload(tenants, tenant, user))
            elif split.path == "/api/users":
                users = tenants.users(_first(query.get("tenant")))
                self._send_json(200, {"users": users.names(),
                                      "current": users.default_user})
            elif split.path == "/api/sessions":
                store = tenants.store(_first(query.get("tenant")),
                                      _first(query.get("user")))
                self._send_json(200, {"sessions": store.names()})
            elif split.path == "/api/status":
                store = tenants.store(_first(query.get("tenant")),
                                      _first(query.get("user")))
                self._send_json(200, {"status": store.status(
                    _first(query.get("session")))})
            elif split.path == "/api/approvals":
                tenant = normalize_tenant_name(_first(query.get("tenant")))
                user = normalize_user_name(_first(query.get("user")))
                self._send_json(200, {"approvals": tenants.approvals
                                      .list_pending(tenant, user)})
            elif split.path == "/api/outputs":
                tenant = normalize_tenant_name(
                    _first(query.get("tenant")) or tenants.default_tenant)
                user = normalize_user_name(
                    _first(query.get("user")) or tenants.default_user)
                self._send_json(
                    200, _outputs_payload(tenants, tenant, user))
            elif split.path == "/api/outputs/download":
                self._download_output(query)
            elif split.path == "/api/usage":
                tenant = normalize_tenant_name(
                    _first(query.get("tenant")) or tenants.default_tenant)
                user = normalize_user_name(
                    _first(query.get("user")) or tenants.default_user)
                self._send_json(
                    200, _usage_payload(tenants, tenant, user))
            elif split.path == "/api/export":
                tenant = normalize_tenant_name(
                    _first(query.get("tenant")) or tenants.default_tenant)
                user = normalize_user_name(
                    _first(query.get("user")) or tenants.default_user)
                self._download_export(tenant, user)
            elif split.path == "/api/profile":
                self._get_profile(query)
            elif split.path == "/api/history":
                store = tenants.store(_first(query.get("tenant")),
                                      _first(query.get("user")))
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

        def _download_output(self, query: dict) -> None:
            """成果物ファイルを attachment として返す。

            返してよいのは、そのテナント×ユーザーの index.json に
            登録されているファイル名だけ（OutputBoard
            .resolve_download が basename 検証と実在確認を担う）。
            登録されていない名前や ../ を含む名前は 404 にする。
            """
            tenant = normalize_tenant_name(
                _first(query.get("tenant")) or tenants.default_tenant)
            user = normalize_user_name(
                _first(query.get("user")) or tenants.default_user)
            name = _first(query.get("name"))
            if not name:
                self._send_json(400, {"error": "name を指定してください"})
                return
            board = OutputBoard(
                user_dir(tenant_dir(tenants.data_dir, tenant), user))
            path = board.resolve_download(name)
            if path is None:
                self._send_json(404, {"error": "成果物が見つかりません"})
                return
            try:
                body = path.read_bytes()
            except OSError:
                self._send_json(404, {"error": "成果物が見つかりません"})
                return
            content_type = (mimetypes.guess_type(path.name)[0]
                            or "application/octet-stream")
            disposition = ("attachment; filename*=UTF-8''"
                           + quote(path.name))
            self._send(200, body, content_type,
                       {"Content-Disposition": disposition})

        def _download_export(self, tenant: str, user: str) -> None:
            """ユーザーデータの書き出し ZIP を attachment として返す。

            ZIP はメモリ上で組み立てて返すため、サーバー側に書き
            出しファイルは残らない。対象は tenant / user で指した
            組み合わせのユーザー置き場の全データ（他のテナント／
            ユーザーの置き場の除外は export.build_export_zip 側が
            担う）。置き場が空でも export-info.json だけの ZIP が
            返る。
            """
            body = build_export_zip(tenants.data_dir, tenant, user)
            filename = default_export_filename(tenant, user)
            disposition = ("attachment; filename*=UTF-8''"
                           + quote(filename))
            self._send(200, body, "application/zip",
                       {"Content-Disposition": disposition})

        def _read_json_body(self) -> dict | None:
            """POST の JSON 本文を辞書として読む。読めなければ None。"""
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return None
            return data if isinstance(data, dict) else None

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path == "/login":
                self._login()
                return
            if not self._authenticated():
                self._send_json(401, {"error": "ログインが必要です"})
                return
            if path == "/api/logout":
                self._logout()
                return
            if path == "/api/sessions/delete":
                self._delete_session()
                return
            if path == "/api/tenant_settings":
                self._save_tenant_settings()
                return
            if path == "/api/notify_prefs":
                self._save_notify_prefs()
                return
            if path == "/api/tenants/create":
                self._create_tenant()
                return
            if path == "/api/tenants/delete":
                self._delete_tenant()
                return
            if path == "/api/profile":
                self._save_profile()
                return
            if path == "/api/approvals/decide":
                self._decide_approval()
                return
            if path != "/api/chat":
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
            session = tenants.store(
                data.get("tenant"), data.get("user")).get(data.get("session"))
            self._chat(session, message, attachments, quote)

        def _get_profile(self, query: dict) -> None:
            """人格・記憶ファイルの本文を返す。

            対象は query の tenant / user（無ければ起動時の組み
            合わせ）の置き場にある、編集できる 4 ファイル
            （SOUL.md / IDENTITY.md / USER.md / MEMORY.md）の
            どれか。ファイルが無ければ空の本文を返す。対象外の
            名前は 400。
            """
            tenant = normalize_tenant_name(
                _first(query.get("tenant")) or tenants.default_tenant)
            user = normalize_user_name(
                _first(query.get("user")) or tenants.default_user)
            name = _first(query.get("name"))
            if not name:
                self._send_json(400, {"error": "name を指定してください"})
                return
            target_dir = user_dir(tenant_dir(tenants.data_dir, tenant), user)
            try:
                content = read_profile_file(target_dir, name)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"tenant": tenant, "user": user,
                                  "name": name, "content": content})

        def _save_profile(self) -> None:
            """人格・記憶ファイルへ本文を保存する。

            対象は本文の tenant / user（無ければ起動時の組み合わせ）
            の置き場にある、編集できる 4 ファイルのどれか。保存は
            profile_files.save_profile_file が担い、既存の内容の
            控え（.bak-<日時>）を取ってから原子的に置き換える。
            対象外の名前とサイズ上限の超過は 400。保存後の本文を
            そのまま返す。反映は新しく作られるセッションから
            （すでに開いているセッションには影響しない）。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            tenant = normalize_tenant_name(
                data.get("tenant") or tenants.default_tenant)
            user = normalize_user_name(data.get("user") or tenants.default_user)
            name = data.get("name")
            content = data.get("content")
            if not isinstance(name, str) or not isinstance(content, str):
                self._send_json(400, {
                    "error": "name と content（文字列）を指定してください"})
                return
            target_dir = user_dir(tenant_dir(tenants.data_dir, tenant), user)
            try:
                save_profile_file(target_dir, name, content)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"tenant": tenant, "user": user,
                                  "name": name, "content": content,
                                  "saved": True})

        def _delete_session(self) -> None:
            """スレッド（セッション）の記録を削除する。

            記録ファイルだけを消し、サーバーが抱えている Session も
            破棄する（次に開けば空の記録で再開する）。"main" も削除
            できる。用事の一覧や長期記憶には触れない。
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("リクエストが不正です")
            except ValueError:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            store = tenants.store(data.get("tenant"), data.get("user"))
            session_id = normalize_session_id(data.get("session"))
            deleted = delete_session_record(store.data_dir, session_id)
            store.discard(session_id)
            self._send_json(200, {"deleted": deleted, "session": session_id})

        def _save_tenant_settings(self) -> None:
            """テナント設定（能動発信・承認の ON/OFF と保持期間）を保存する。

            能動発信と承認は settings.json、保持期間は retention.json
            に書く（どちらも既存の仕組み）。retention_days に null を
            渡すとそのテナントの指定を取り除いて全体の既定に戻す。
            渡されていない項目は変更しない。保存後の最新の設定を返す。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            name = normalize_tenant_name(
                data.get("tenant") or tenants.default_tenant)
            if "proactive_enabled" in data:
                enabled = data["proactive_enabled"]
                if not isinstance(enabled, bool):
                    self._send_json(400, {
                        "error": "proactive_enabled は true / false で指定してください"})
                    return
                set_proactive_enabled(tenants.data_dir, name, enabled)
            if "approvals_enabled" in data:
                enabled = data["approvals_enabled"]
                if not isinstance(enabled, bool):
                    self._send_json(400, {
                        "error": "approvals_enabled は true / false で指定してください"})
                    return
                set_approvals_enabled(tenants.data_dir, name, enabled)
            if "retention_days" in data:
                days = data["retention_days"]
                if days is not None and (isinstance(days, bool)
                                         or not isinstance(days, int)
                                         or days < 0):
                    self._send_json(400, {
                        "error": "retention_days は 0 以上の整数か null で指定してください"})
                    return
                save_retention_override(tenants.data_dir, name, days)
            self._send_json(200, _tenant_settings_payload(tenants, name))

        def _save_notify_prefs(self) -> None:
            """知らせ方の好み（ミュートと静かな時間帯）を保存する。

            対象は本文の tenant / user（無ければ起動時の組み合わせ）
            のユーザーの置き場（notify_prefs.json）。渡された項目
            だけを変更する:
            - add_topic: 知らせない話題の語を 1 つ追加
            - remove_topic: 知らせない話題の語を 1 つ削除
            - quiet_start / quiet_end: 両方そろえて渡す。どちらも
              "HH:MM" なら時間帯を設定し、どちらも null なら解除
            保存後の最新の好みを返す。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            tenant = normalize_tenant_name(
                data.get("tenant") or tenants.default_tenant)
            user = normalize_user_name(data.get("user") or tenants.default_user)
            target_dir = user_dir(tenant_dir(tenants.data_dir, tenant), user)
            for key, func in (("add_topic", add_muted_topic),
                              ("remove_topic", remove_muted_topic)):
                if key in data:
                    topic = data[key]
                    if not isinstance(topic, str) or not topic.strip():
                        self._send_json(400, {
                            "error": f"{key} は空でない文字列で指定してください"})
                        return
                    func(target_dir, topic)
            if "quiet_start" in data or "quiet_end" in data:
                start, end = data.get("quiet_start"), data.get("quiet_end")
                if start is None and end is None:
                    clear_quiet_hours(target_dir)
                elif isinstance(start, str) and isinstance(end, str):
                    try:
                        set_quiet_hours(target_dir, start, end)
                    except ValueError:
                        self._send_json(400, {
                            "error": "時間帯は HH:MM 形式で指定してください"})
                        return
                else:
                    self._send_json(400, {
                        "error": "quiet_start と quiet_end は両方そろえて"
                                 "指定してください（解除は両方 null）"})
                    return
            self._send_json(
                200, _notify_prefs_payload(tenants, tenant, user))

        def _decide_approval(self) -> None:
            """保留中の承認（危険な道具の実行許可）を決定する。

            対象は本文の tenant / user（無ければ起動時の組み合わせ）
            の保留だけ。決定すると、承認待ちで止まっていた会話
            リクエストのスレッドが先へ進む。対象が見つからなければ
            404（すでに時間切れ・決定済みのものを含む）。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            approved = data.get("approved")
            approval_id = data.get("id")
            if not isinstance(approved, bool) \
                    or not isinstance(approval_id, str) or not approval_id:
                self._send_json(400, {
                    "error": "id と approved（true / false）を指定してください"})
                return
            tenant = normalize_tenant_name(
                data.get("tenant") or tenants.default_tenant)
            user = normalize_user_name(data.get("user") or tenants.default_user)
            if not tenants.approvals.decide(tenant, user, approval_id, approved):
                self._send_json(404, {"error": "対象の承認が見つかりません"})
                return
            self._send_json(200, {"decided": True, "approved": approved})

        def _create_tenant(self) -> None:
            """テナントの置き場を新規作成する。

            名前は既存の正規化（normalize_tenant_name）を通して検証
            する。既定テナント（default）は作成済みとして扱い、すでに
            置き場がある名前も 400 で理由を返す。テナントの削除は
            /api/tenants/delete で扱う。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            raw = data.get("name")
            if not isinstance(raw, str) or not raw.strip():
                self._send_json(400, {"error": "テナント名を入力してください"})
                return
            name = normalize_tenant_name(raw)
            if name == DEFAULT_TENANT:
                self._send_json(400, {
                    "error": "既定のテナント（default）はすでにあります"})
                return
            if name in list_tenant_names(tenants.data_dir):
                self._send_json(400, {
                    "error": f"テナント「{name}」はすでに存在します"})
                return
            try:
                tenant_dir(tenants.data_dir, name).mkdir(
                    parents=True, exist_ok=True)
            except OSError:
                self._send_json(500, {
                    "error": "テナントの置き場を作れませんでした"})
                return
            self._send_json(200, {"tenant": name, "created": True})

        def _delete_tenant(self) -> None:
            """テナントを削除する（控えを取ってから置き場を丸ごと消す）。

            削除は取り消せないため、段取りを厳密に守る:
            1. テナントの全データの控えをデータ置き場ルートの
               backups/ に ZIP で保存する（作れなければ削除しない）
            2. テナントの置き場（tenants/<名前>）を丸ごと削除する
            3. 置き場の外にあるテナント設定（settings.json と
               retention.json のそのテナントの項目）を取り除き、
               サーバーが抱えている UserStore も破棄する
            既定テナント（default）は置き場がデータ置き場そのもの
            のため削除できない。サーバーの起動時テナントも、土台と
            なる UserStore を抱えているため削除できない。置き場が
            無いテナントは 404。名前は作成と同じ正規化を通す。
            応答には控えのファイル名（backup）を含める。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            raw = data.get("tenant", data.get("name"))
            if not isinstance(raw, str) or not raw.strip():
                self._send_json(400, {"error": "テナント名を指定してください"})
                return
            name = normalize_tenant_name(raw)
            if name == DEFAULT_TENANT:
                self._send_json(400, {
                    "error": "既定のテナント（default）は削除できません"})
                return
            if name == tenants.default_tenant:
                self._send_json(400, {
                    "error": "起動中のテナントは削除できません"})
                return
            tdir = tenant_dir(tenants.data_dir, name)
            if not tdir.is_dir():
                self._send_json(404, {
                    "error": f"テナント「{name}」が見つかりません"})
                return
            try:
                backup_path = save_tenant_backup(tenants.data_dir, name)
            except OSError:
                self._send_json(500, {
                    "error": "控え（バックアップ）を作れなかったため、"
                             "削除しませんでした"})
                return
            try:
                shutil.rmtree(tdir)
            except OSError:
                self._send_json(500, {
                    "error": "テナントの置き場を削除できませんでした"})
                return
            remove_tenant_settings(tenants.data_dir, name)
            if name in load_retention_overrides(tenants.data_dir):
                save_retention_override(tenants.data_dir, name, None)
            tenants.discard_tenant(name)
            self._send_json(200, {"tenant": name, "deleted": True,
                                  "backup": backup_path.name})

        def _chat(self, session: Session, message: str,
                  attachments: list[Attachment], quote: str | None) -> None:
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
    モデルを共有する。テナントとユーザーは TenantStore が組み合わせ
    ごとに SessionStore を持ち、ページからの切り替えに応じる。
    """
    auth = AuthGate(session.cfg.web_password,
                    session.cfg.web_secure_cookie)
    page = render_page(session.cfg.provider, web_auth=auth.enabled)
    tenants = TenantStore(session, session_factory)
    server = ThreadingHTTPServer((host, port),
                                 make_handler(tenants, page, auth))
    # 能動発信の巡回が保持中の Session を引けるよう、サーバーに
    # TenantStore をぶら下げておく（serve が使う）
    server.tenants = tenants  # type: ignore[attr-defined]
    return server


def _start_proactive_loop(tenants: TenantStore, cfg: Config) -> None:
    """能動発信の巡回を裏側のスレッドで始める。

    起動後まもなく 1 回、その後は Config.proactive_interval_seconds
    （環境変数 KUMA_PROACTIVE_INTERVAL_SECONDS、既定 300 秒）ごとに
    全テナント×全ユーザーを巡回し、言うべきことがあるユーザーに
    だけ main の会話へ発言する。投稿はサーバーが保持している
    Session を通す。Telegram が設定されていれば、対象ユーザーの
    投稿は Telegram へも届ける（telegram.build_proactive_notifier）。
    間隔が 0 以下なら巡回しない。1 回の巡回の失敗で Web サーバー
    自体は落とさない。
    """
    interval = cfg.proactive_interval_seconds
    if interval <= 0:
        return
    stopped = threading.Event()
    # Telegram が設定されていれば、投稿を Telegram へも届ける通知を
    # 用意する（未設定・無効化・許可リスト空なら None で従来どおり）
    notify = build_proactive_notifier(cfg)

    def loop() -> None:
        while not stopped.is_set():
            try:
                run_sweep(cfg, session_getter=lambda t, u: tenants.store(
                    t, u).get(DEFAULT_SESSION_ID), notify=notify)
            except Exception:
                pass  # 巡回の失敗は次回に回す
            stopped.wait(interval)

    threading.Thread(target=loop, daemon=True, name="kuma-proactive").start()


def serve(session: Session, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Web UI を起動し、Ctrl-C まで待ち続ける。

    起動時に保持期限を過ぎた記録の掃除を 1 回だけ行う。保持期間は
    Config.retention_days（全体の既定）が基本で、データ置き場の
    retention.json に指定があるテナントはその期間が優先される。
    サービスとして動かす場合、利用者はデータ置き場のファイルを
    自分で消せないため、この自動掃除が前提になる。
    """
    cfg = session.cfg
    result = cleanup_expired(cfg.data_dir, cfg.retention_days)
    if result.total:
        print(f"保持期限を過ぎた記録を掃除しました: "
              f"部屋の記録 {result.sessions_removed} 件、"
              f"完了した用事 {result.tasks_removed} 件")
    server = build_server(session, host, port)
    if cfg.web_password:
        print("Web UI のログインが有効です（パスワードは環境変数 KUMA_WEB_PASSWORD）")
    _start_proactive_loop(server.tenants, cfg)  # type: ignore[attr-defined]
    # Telegram のトークンが設定されていれば、裏側スレッドで受信も始める
    # （未設定なら何もしない。Telegram 用の Session は承認者なしで作る）
    if start_telegram_polling(cfg, session.model) is not None:
        print("Telegram の受信を開始しました")
    print(f"クマの Web UI: http://{host}:{server.server_address[1]} （終了は Ctrl-C）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
        close_shared_managers()  # MCP サーバーの子プロセスを残さない
