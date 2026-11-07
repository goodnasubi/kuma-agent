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

from .agent_cli import CLI_PROVIDERS, cli_command_name
from .config import Config, mode_label, normalize_mode
from .export import (
    build_export_zip,
    default_export_filename,
    save_tenant_backup,
)
from .avatar import AvatarStore
from .feed import load_feed_entries
from .goals import (
    GoalBoard,
    delete_goal,
    goal_view,
    link_task,
    unlink_task,
)
from .ideas import (
    VISIBLE_STATUSES,
    IdeaBoard,
    accept_idea,
    dismiss_idea,
    generate_ideas,
)
from .line_api import HttpxLineApi, LineBot, line_enabled
from .line_api import build_proactive_notifier as build_line_proactive_notifier
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
from .proactive import combine_notifiers, run_sweep
from .profile_files import read_profile_file, save_profile_file
from .restore import restore_user_data
from .session import Session
from .spaces import SpaceStore, validate_space
from .tasks import TaskBoard
from .worksteps import WorkSteps
from .telegram import build_proactive_notifier
from .telegram import start_polling_thread as start_telegram_polling
from .brain import (
    ALL_PROVIDERS,
    MAX_BRAIN_API_KEY_CHARS,
    MAX_BRAIN_MODEL_CHARS,
    brain_key_status,
    cli_brain_tenants,
    delete_brain_credentials,
    effective_brain,
    has_tenant_brain_override,
    resolve_brain,
    save_brain_credentials,
    tenant_brain_providers,
)
from .settings import (
    approvals_enabled_for,
    brain_setting_for,
    proactive_enabled_for,
    remove_tenant_settings,
    set_approvals_enabled,
    set_brain_setting,
    set_proactive_enabled,
)
from .tools import summarize_tool_args
from .activity import load_activity_records
from .usage import summarize_usage

# ブランド色（README のイメージカラー）
HONEY = "#E79A3C"
COCOA = "#3A332C"
CREAM = "#FAF9F7"
LINE = "#E7DFD3"

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
<script>
  try {
    document.documentElement.dataset.theme =
      localStorage.getItem("kuma-theme") || "system";
  } catch (e) {
    document.documentElement.dataset.theme = "system";
  }
</script>
<style>
  * { box-sizing: border-box; }
  :root { --bg: __CREAM__; --surface: #FFFFFF; --ink: __COCOA__;
          --line: __LINE__; --bubble: #F3E9DC; --muted: #8A8175;
          --quote-bg: #FBF4E8; --danger-bg: #FDF3F2; }
  :root[data-theme="dark"] { --bg: #201D1A; --surface: #2B2723;
          --ink: #EFE9E0; --line: #453E34; --bubble: #4A3A24;
          --muted: #A79C8D; --quote-bg: #38302A; --danger-bg: #3B2523; }
  @media (prefers-color-scheme: dark) {
    :root[data-theme="system"] { --bg: #201D1A; --surface: #2B2723;
          --ink: #EFE9E0; --line: #453E34; --bubble: #4A3A24;
          --muted: #A79C8D; --quote-bg: #38302A; --danger-bg: #3B2523; } }
  :root[data-theme="pixel"] { --bg: #030308; --surface: #0B0B18;
          --ink: #EFFFF2; --line: #00FF66; --bubble: #07331B;
          --muted: #5CFF9D; --quote-bg: #063F3C; --danger-bg: #4A0E24; }
  input, select, textarea { background: var(--surface); color: var(--ink); }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans",
                      "Noto Sans JP", sans-serif;
         display: flex; flex-direction: column; height: 100dvh; }
  header { background: var(--surface); color: var(--ink); display: flex; align-items: center;
           flex-wrap: wrap; gap: 12px; padding: 10px 16px;
           border-bottom: 1px solid var(--line); }
  header .avatar { width: 40px; height: 40px; flex: none; cursor: pointer; }
  header .avatar svg { width: 100%; height: 100%; display: block; }
  header .avatar img { width: 100%; height: 100%; display: block;
               border-radius: 50%; object-fit: cover; }
  header h1 { font-size: 17px; margin: 0; }
  header .provider { font-size: 12px; color: var(--muted); }
  .sessions { display: flex; gap: 6px; align-items: center; margin-left: auto;
              flex-wrap: wrap; font-size: 13px; }
  .sessions select, .sessions input { padding: 6px; font-size: 13px;
           border-radius: 8px; border: 1px solid var(--line); background: var(--surface); }
  .sessions button { padding: 6px 14px; font-size: 13px; background: var(--surface);
           color: var(--ink); border: 1px solid var(--line); border-radius: 8px; }
  .sessions[hidden] { display: none; }
  #tenantPanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #tenantPanel[hidden] { display: none; }
  #tenantPanel h2 { font-size: 15px; margin: 0 0 8px; }
  .tenantRow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               padding: 8px 0; border-top: 1px dashed var(--line); font-size: 14px; }
  .tenantRow .tname { font-weight: bold; min-width: 8em; }
  .tenantRow input[type="number"] { width: 5em; padding: 4px; font-size: 13px;
               border-radius: 6px; border: 1px solid var(--line); }
  .tenantRow button, .adminNew button { padding: 4px 14px; font-size: 13px;
               background: var(--surface); color: var(--ink); border: 1px solid var(--line);
               border-radius: 8px; }
  .adminNew { display: flex; gap: 8px; align-items: center; margin-top: 10px;
              font-size: 14px; }
  .adminNew input { padding: 6px; font-size: 13px; border-radius: 6px;
               border: 1px solid var(--line); }
  #tenantDeleteConfirm { border: 2px solid #b3261e; border-radius: 8px;
               padding: 12px 14px; margin: 12px 0; background: var(--danger-bg);
               font-size: 14px; }
  #tenantDeleteConfirm[hidden] { display: none; }
  #tenantDeleteConfirm h3 { margin: 0 0 8px; font-size: 15px; }
  #tenantDeleteConfirm ul { margin: 6px 0 10px; padding-left: 1.4em; }
  #tenantDeleteConfirm .warn { color: #b3261e; font-weight: bold; }
  #tenantDeleteConfirm .buttons { display: flex; gap: 10px; margin-top: 10px; }
  #tenantDeleteConfirm .deleteDo { background: #b3261e; color: #fff;
               border: 1px solid #b3261e; padding: 0; border-radius: 6px;
               width: 32px; height: 32px; display: grid; place-items: center; }
  #tenantPanel h3 { font-size: 14px; margin: 16px 0 4px;
               border-top: 1px solid var(--line); padding-top: 12px; }
  #outputsPanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #outputsPanel[hidden] { display: none; }
  #outputsPanel h2 { font-size: 15px; margin: 0 0 8px; }
  .outputRow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               padding: 8px 0; border-top: 1px dashed var(--line); font-size: 14px; }
  .outputRow .otitle { font-weight: bold; min-width: 8em; }
  .outputRow .oname, .outputRow .ometa { opacity: .85; }
  .outputRow a { padding: 4px 14px; font-size: 13px; background: var(--surface);
               color: var(--ink); border: 1px solid var(--line);
               border-radius: 8px; text-decoration: none; }
  #feedPanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #feedPanel[hidden] { display: none; }
  #feedPanel h2 { font-size: 15px; margin: 0 0 8px; }
  .feedRow { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
               padding: 10px 0; border-top: 1px dashed var(--line);
               font-size: 14px; }
  .feedRow .fkind { padding: 1px 9px; font-size: 12px; line-height: 1.6;
               background: var(--quote-bg); color: var(--ink);
               border: 1px solid var(--line); border-radius: 8px;
               white-space: nowrap; }
  .feedRow[data-kind="briefing"] .fkind { border-color: __HONEY__;
               font-weight: bold; }
  .feedRow .ftime { opacity: .85; font-size: 13px; }
  .feedRow .ftitle { font-weight: bold; }
  .feedRow .fbody { flex-basis: 100%; white-space: pre-wrap;
               word-break: break-word; line-height: 1.55; }
  #ideasPanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #ideasPanel[hidden] { display: none; }
  #ideasPanel h2 { font-size: 15px; margin: 0 0 8px; }
  #ideasPanel .ideasNote { font-size: 13px; margin: 0 0 10px; }
  .ideaTools { display: flex; gap: 10px; align-items: center;
               margin-bottom: 4px; }
  .ideaTools button { padding: 6px 16px; font-size: 14px;
               background: var(--surface); color: var(--ink);
               border: 1px solid var(--line); border-radius: 8px;
               cursor: pointer; }
  .ideaTools button:disabled { opacity: .6; cursor: default; }
  #ideaStatus { font-size: 13px; }
  .ideaRow { padding: 10px 0; border-top: 1px dashed var(--line);
               font-size: 14px; }
  .ideaRow .ihead { display: flex; flex-wrap: wrap; gap: 8px;
               align-items: center; }
  .ideaRow .ititle { font-weight: bold; }
  .ideaRow .itime { opacity: .85; font-size: 13px; }
  .ideaRow .istatus { padding: 1px 9px; font-size: 12px; line-height: 1.6;
               background: var(--quote-bg); color: var(--ink);
               border: 1px solid __HONEY__; border-radius: 8px;
               white-space: nowrap; font-weight: bold; }
  .ideaRow .ibody { white-space: pre-wrap; word-break: break-word;
               line-height: 1.55; margin-top: 4px; }
  .ideaRow .ireason { font-size: 13px; opacity: .85; margin-top: 4px; }
  .ideaRow .iactions { display: flex; gap: 8px; margin-top: 8px; }
  .ideaRow .iactions button { padding: 5px 14px; font-size: 13px;
               background: var(--surface); color: var(--ink);
               border: 1px solid var(--line); border-radius: 8px;
               cursor: pointer; }
  #goalsPanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #goalsPanel[hidden] { display: none; }
  #goalsPanel h2 { font-size: 15px; margin: 0 0 8px; }
  #goalsPanel h3 { font-size: 14px; margin: 16px 0 6px;
               border-top: 1px solid var(--line); padding-top: 12px; }
  #goalsPanel .goalsNote { font-size: 13px; margin: 0 0 10px; }
  #goalStatus { font-size: 13px; margin: 8px 0 0; }
  .goalRow { padding: 10px 0; border-top: 1px dashed var(--line);
               font-size: 14px; }
  .goalRow .ghead { display: flex; flex-wrap: wrap; gap: 8px;
               align-items: center; }
  .goalRow .gtitle { font-weight: bold; }
  .goalRow.done .gtitle { opacity: .7; }
  .goalRow .gtime { opacity: .85; font-size: 13px; }
  .goalRow .gstatus { padding: 1px 9px; font-size: 12px; line-height: 1.6;
               background: var(--quote-bg); color: var(--ink);
               border: 1px solid var(--line); border-radius: 8px;
               white-space: nowrap; }
  .goalRow .gstatus.done { border-color: __HONEY__; font-weight: bold; }
  .goalRow .gdesc { white-space: pre-wrap; word-break: break-word;
               line-height: 1.55; margin-top: 4px; }
  .goalRow .gprogress { margin-top: 8px; font-size: 13px; }
  .goalRow .gbar { height: 8px; background: var(--quote-bg);
               border: 1px solid var(--line); border-radius: 4px;
               margin-top: 4px; overflow: hidden; }
  .goalRow .gbarFill { height: 100%; background: __HONEY__; }
  .goalRow .gtasks, .goalRow .gnotes { margin-top: 8px; font-size: 13px; }
  .goalRow .gtask, .goalRow .gnote { display: flex; flex-wrap: wrap;
               gap: 8px; align-items: center; padding: 3px 0; }
  .goalRow .gtask .tstate { opacity: .85; }
  .goalRow .gnote .ntime { opacity: .85; }
  .goalRow .gnote .nbody { white-space: pre-wrap; word-break: break-word; }
  .goalRow .gtools { display: flex; flex-wrap: wrap; gap: 8px;
               align-items: center; margin-top: 8px; }
  .goalRow button, .goalNew button { padding: 5px 14px; font-size: 13px;
               background: var(--surface); color: var(--ink);
               border: 1px solid var(--line); border-radius: 8px;
               cursor: pointer; }
  .goalRow input, .goalRow select, .goalNew input { padding: 5px 8px;
               font-size: 13px; background: var(--surface);
               color: var(--ink); border: 1px solid var(--line);
               border-radius: 8px; }
  .goalRow .gedit { display: flex; flex-wrap: wrap; gap: 8px;
               align-items: center; margin-top: 8px; }
  .goalNew { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  #spacesPanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #spacesPanel[hidden] { display: none; }
  #spacesPanel h2 { font-size: 15px; margin: 0 0 8px; }
  #spacesPanel h3 { font-size: 14px; margin: 16px 0 6px;
               border-top: 1px solid var(--line); padding-top: 12px; }
  #spacesPanel .spacesNote { font-size: 13px; margin: 0 0 10px; }
  #spaceStatus { font-size: 13px; margin: 8px 0 0; }
  .spaceRow { padding: 10px 0; border-top: 1px dashed var(--line);
               font-size: 14px; }
  .spaceRow .shead { display: flex; flex-wrap: wrap; gap: 8px;
               align-items: center; }
  .spaceRow .stitle { font-weight: bold; }
  .spaceRow .stime { opacity: .85; font-size: 13px; }
  .spaceRow button, .spaceEdit button { padding: 5px 14px; font-size: 13px;
               background: var(--surface); color: var(--ink);
               border: 1px solid var(--line); border-radius: 8px;
               cursor: pointer; }
  #spaceFrame { width: 100%; height: 480px; background: #fff;
               border: 1px solid var(--line); border-radius: 8px; }
  .spaceEdit { display: flex; flex-direction: column; gap: 8px; }
  .spaceEdit input, .spaceEdit textarea { padding: 5px 8px;
               font-size: 13px; background: var(--surface);
               color: var(--ink); border: 1px solid var(--line);
               border-radius: 8px; }
  .spaceEditTools { display: flex; flex-wrap: wrap; gap: 8px;
               align-items: center; }
  #profilePanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #profilePanel[hidden] { display: none; }
  #profilePanel h2 { font-size: 15px; margin: 0 0 8px; }
  #profilePanel .profileNote { font-size: 13px; margin: 0 0 8px; }
  #profileTabs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  #profileTabs button { padding: 4px 14px; font-size: 13px; background: var(--surface);
               color: var(--ink); border: 1px solid var(--line); border-radius: 8px; }
  #profileTabs button.active { background: __HONEY__; color: #fff;
               border-color: __HONEY__; }
  #profileText { width: 100%; padding: 10px; font-size: 14px; line-height: 1.5;
               border: 1px solid var(--line); border-radius: 8px;
               font-family: monospace; }
  .profileActions { display: flex; gap: 10px; align-items: center;
               margin-top: 8px; }
  .profileActions button { padding: 6px 16px; font-size: 14px; }
  #profileStatus { font-size: 13px; }
  #profilePanel h3 { font-size: 14px; margin: 16px 0 6px;
               border-top: 1px solid var(--line); padding-top: 12px; }
  .avatarBox { display: flex; gap: 14px; align-items: flex-start; }
  .avatarPreview { width: 64px; height: 64px; border-radius: 50%;
               object-fit: cover; border: 1px solid var(--line);
               background: var(--surface); flex: none; }
  #avatarRows { display: flex; flex-direction: column; gap: 6px; }
  .avatarRow { display: flex; gap: 8px; align-items: center;
               font-size: 13px; }
  .avatarRow img { width: 32px; height: 32px; border-radius: 50%;
               object-fit: cover; border: 1px solid var(--line); }
  .avatarRow button { padding: 3px 10px; font-size: 12px;
               background: var(--surface); color: var(--ink);
               border: 1px solid var(--line); border-radius: 8px;
               cursor: pointer; }
  .avatarRow .nowBadge { padding: 1px 8px; font-size: 12px;
               border: 1px solid __HONEY__; border-radius: 8px;
               font-weight: bold; }
  #avatarStatus { font-size: 13px; }
  #usagePanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #usagePanel[hidden] { display: none; }
  #usagePanel h2 { font-size: 15px; margin: 0 0 8px; }
  #usagePanel h3 { font-size: 14px; margin: 14px 0 4px; }
  #usagePanel .usageTotal { font-size: 14px; margin: 0 0 4px; }
  #usagePanel table { border-collapse: collapse; font-size: 14px; }
  #usagePanel th, #usagePanel td { padding: 4px 10px 4px 0;
               text-align: right; }
  #usagePanel th:first-child, #usagePanel td:first-child { text-align: left; }
  #activityPanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #activityPanel[hidden] { display: none; }
  #activityPanel h2 { font-size: 15px; margin: 0 0 8px; }
  #activityPanel .activityNote { font-size: 13px; margin: 0 0 8px; }
  .activityRow { padding: 6px 0; font-size: 14px;
                 border-bottom: 1px solid var(--line); }
  .activityRow:last-child { border-bottom: none; }
  .activityRow .activityTime { white-space: nowrap; }
  .activityRow .activityKind { font-weight: bold; }
  .notifyRow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               padding: 6px 0; font-size: 14px; }
  .notifyRow .tname { min-width: 8em; }
  .notifyRow button { padding: 4px 14px; font-size: 13px;
               background: var(--surface); color: var(--ink); border: 1px solid var(--line);
               border-radius: 8px; }
  .adminNew input[type="time"] { padding: 4px; }
  #approvalPanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #statusPanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #statusPanel[hidden] { display: none; }
  #statusPanel h2 { font-size: 15px; margin: 0 0 8px; }
  #statusPanel .statusLine { font-size: 14px; margin: 0 0 6px; }
  #statusFacts { margin: 6px 0 0; padding-left: 1.4em; font-size: 14px; }
  #statusSteps h3 { font-size: 14px; margin: 12px 0 6px; }
  #statusSteps ul { margin: 0; padding-left: 1.4em; font-size: 14px; }
  #statusSteps li.doing { font-weight: bold; }
  #statusSteps li.done { opacity: 0.65; }
  .iconBtn { width: 38px; height: 38px; padding: 0; display: grid;
             place-items: center; background: var(--surface);
             color: var(--ink); border: 1px solid var(--line);
             border-radius: 8px; cursor: pointer; }
  .iconBtn svg { width: 20px; height: 20px; display: block; }
  .iconBtn.sm { width: 32px; height: 32px; }
  .iconBtn.sm svg { width: 17px; height: 17px; }
  .iconBtn.go { background: __HONEY__; color: #fff; border-color: __HONEY__; }
  .msg .reactions { display: flex; gap: 4px; flex-wrap: wrap;
            align-items: center; }
  .msg .reactions[hidden] { display: none; }
  .reactionChip { padding: 2px 9px; font-size: 14px; line-height: 1.5;
            background: var(--quote-bg); color: var(--ink);
            border: 1px solid var(--line); border-radius: 8px;
            cursor: pointer; }
  .reactionChip.agentChip { display: inline-flex; align-items: center;
            gap: 3px; border-color: __HONEY__; cursor: default; }
  .reactionChip.agentChip .agentMark { display: inline-flex; }
  .reactionChip.agentChip .agentMark svg,
  .reactionChip.agentChip .agentMark img { width: 14px; height: 14px;
               border-radius: 50%; object-fit: cover;
            display: block; }
  #reactionPalette { position: fixed; z-index: 60; display: flex; gap: 2px;
            background: var(--surface); border: 1px solid var(--line);
            border-radius: 8px; padding: 6px;
            box-shadow: 0 8px 24px rgba(58, 51, 44, .18); }
  #reactionPalette button { padding: 4px 7px; font-size: 19px; line-height: 1;
            background: transparent; border: none; border-radius: 6px;
            cursor: pointer; color: var(--ink); }
  #reactionPalette button:hover { background: var(--quote-bg); }
  #tabBar button { display: inline-flex; align-items: center; gap: 6px; }
  #tabBar button svg { width: 18px; height: 18px; display: block; }
  @media (max-width: 720px) {
    body { padding-bottom: 78px; }
    header { gap: 6px; padding: 8px 10px; }
    header .sessions input { width: 6em; }
    #tabBar { position: fixed; left: 0; right: 0; bottom: 0; width: 100%;
              background: var(--surface); border-top: 1px solid var(--line);
              padding: 6px 4px calc(6px + env(safe-area-inset-bottom));
              justify-content: space-around; z-index: 40; }
    #tabBar button { flex: 1 1 0; min-width: 0;
              padding: 8px 4px; border: none; background: transparent;
              color: var(--ink); }
    #tabBar button .tabLabel { display: none; }
    #tabBar button.active { background: transparent; color: __HONEY__;
              border: none; }
    #tabBar button svg { width: 24px; height: 24px; }
    #outputsPanel, #feedPanel, #ideasPanel, #goalsPanel, #profilePanel,
    #usagePanel, #activityPanel, #tenantPanel, #approvalPanel, #choicePanel, #statusPanel, .tabNote,
    #adminMenu,
    #form, #quoteBar {
      width: calc(100% - 16px); }
  }
  #tabBar { display: flex; gap: 6px; flex-wrap: nowrap; overflow-x: auto;
            width: min(880px, calc(100% - 24px)); align-self: center;
            padding: 10px 2px 2px; }
  #tabBar button { padding: 6px 16px; font-size: 14px; white-space: nowrap;
            background: var(--surface); color: var(--ink);
            border: 1px solid var(--line); border-radius: 8px; }
  #tabBar button.active { background: __HONEY__; color: #fff;
            border-color: __HONEY__; }
  #log[hidden], #form[hidden], #dropHint[hidden] { display: none; }
  .tabNote { background: var(--surface); border: 1px solid var(--line);
            border-radius: 6px; padding: 28px 20px; text-align: center;
            width: min(880px, calc(100% - 24px)); align-self: center;
            margin-top: 12px; color: var(--muted); font-size: 14px; }
  .tabNote[hidden] { display: none; }
  .tabNote h2 { font-size: 15px; color: var(--ink); margin: 0 0 8px; }
  .tabNote p { margin: 0; }
  #adminMenu { background: var(--surface); border: 1px solid var(--line);
            border-radius: 6px; padding: 16px 20px;
            width: min(880px, calc(100% - 24px)); align-self: center;
            margin-top: 12px; }
  #adminMenu[hidden] { display: none; }
  #adminMenu h2 { font-size: 15px; margin: 0 0 8px; }
  .adminBtns { display: flex; flex-wrap: wrap; gap: 6px; }
  .adminBtns button { padding: 6px 14px; font-size: 13px;
            background: var(--surface); color: var(--ink);
            border: 1px solid var(--line); border-radius: 8px; }
  #approvalPanel[hidden] { display: none; }
  #approvalPanel h2 { font-size: 15px; margin: 0 0 8px; }
  .approvalRow { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
               padding: 8px 0; border-top: 1px dashed var(--line); font-size: 14px; }
  .approvalRow .asummary { flex: 1; min-width: 12em; word-break: break-all; }
  .approvalRow button { padding: 6px 14px; font-size: 13px; }
  .approvalRow button.deny { background: var(--surface); color: var(--ink);
               border: 1px solid var(--line); }
  #choicePanel { background: var(--surface); border: 1px solid var(--line);
                 border-radius: 6px; padding: 16px 20px;
                 width: min(880px, calc(100% - 24px)); align-self: center;
                 margin-top: 12px; }
  #choicePanel[hidden] { display: none; }
  #choicePanel h2 { font-size: 15px; margin: 0 0 8px; }
  .choiceCard { padding: 8px 0; border-top: 1px dashed var(--line);
               font-size: 14px; }
  .choiceCard .cquestion { font-weight: bold; margin-bottom: 8px;
               word-break: break-word; }
  .choiceOptions { display: flex; flex-wrap: wrap; gap: 8px; }
  .choiceOptions button { padding: 6px 14px; font-size: 13px;
               text-align: left; }
  .choiceOptions button .cnote { display: block; font-size: 12px;
               opacity: .75; }
  .choiceText { display: flex; gap: 8px; margin-top: 8px; }
  .choiceText input { flex: 1; min-width: 8em; padding: 6px 8px;
               font-size: 14px; }
  .choiceText button { padding: 6px 14px; font-size: 13px;
               background: var(--surface); color: var(--ink);
               border: 1px solid var(--line); }
  #log { flex: 1; overflow-y: auto; padding: 20px 16px 8px; display: flex;
         flex-direction: column; gap: 20px;
         width: min(880px, 100%); align-self: center; }
  .msg { max-width: 78%; padding: 10px 14px; border-radius: 6px;
         white-space: pre-wrap; word-break: break-word; line-height: 1.65;
         font-size: 15px; }
  .msg.user { align-self: flex-end; background: var(--bubble); color: var(--ink); }
  .msg.assistant { align-self: stretch; max-width: 100%;
                    background: transparent; border: none; padding: 0 2px; }
  .msg .quoted { display: block; font-size: 12px; opacity: .85;
                 border-left: 3px solid currentColor; padding-left: 6px;
                 margin-bottom: 4px; }
  .msg .bodyText { display: block; }
  /* 発話の下の操作ボタン（引用・読み上げ・リアクション）は1行に並べ、
     デザインはリアクションのボタン（iconBtn）にそろえる */
  .msg .msgActions { display: flex; flex-wrap: wrap; gap: 6px;
               margin-top: 6px; align-items: center; }
  #record.recording { background: #B3402A; color: #fff; border-color: #B3402A; }
  #recordStatus { font-size: 13px; align-self: center; }
  #recordStatus[hidden] { display: none; }
  form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
         padding: 10px 12px; background: var(--surface);
         border: 1px solid var(--line); border-radius: 8px;
         width: min(880px, calc(100% - 24px)); align-self: center;
         margin: 8px 0 14px;
         box-shadow: 0 4px 18px rgba(58, 51, 44, .06); }
  form:focus-within { border-color: __HONEY__; }
  form > button { flex-shrink: 0; }
  #send { margin-left: auto; }
  #quoteBox { flex-basis: 100%; display: flex; gap: 8px; align-items: center;
              font-size: 13px; background: var(--quote-bg); padding: 6px 8px;
              border-left: 4px solid __HONEY__; border-radius: 6px; }
  #quoteBox[hidden] { display: none; }
  #quoteText { flex: 1; overflow: hidden; text-overflow: ellipsis;
               white-space: nowrap; }
  #clearQuote { padding: 4px 10px; font-size: 12px; }
  #statusBox { flex-basis: 100%; display: flex; gap: 2px; align-items: center;
               font-size: 13px; color: var(--ink); min-height: 1em; }
  #statusBox[hidden] { display: none; }
  #statusBox .dot { animation: statusBlink 1.2s infinite; }
  #statusBox .dot:nth-child(2) { animation-delay: .2s; }
  #statusBox .dot:nth-child(3) { animation-delay: .4s; }
  @keyframes statusBlink { 0%, 60%, 100% { opacity: .15; } 30% { opacity: 1; } }
  #files { flex-basis: 100%; font-size: 13px; min-height: 1em; }
  #files:empty { display: none; }
  #input { flex: 1; min-width: 0; padding: 8px 6px; font-size: 16px;
           border: none; background: transparent; outline: none; }
  button { padding: 10px 18px; font-size: 16px; border: none; border-radius: 8px;
           background: __HONEY__; color: #fff; cursor: pointer; }
  button:disabled { opacity: .5; }
  #attach, #record { background: var(--surface); color: var(--ink); border: 1px solid var(--line); }
  body.dragover #log { outline: 3px dashed __HONEY__; outline-offset: -8px; }
  #dropHint { display: none; text-align: center; padding: 8px; font-size: 14px;
              background: var(--surface); border-top: 1px solid var(--line); }
  body.dragover #dropHint { display: block; }
  /* アイコンだけのボタンは、置き場所ごとのボタンの余白指定に
     勝って余白を無くす（余白が残ると絵柄が中央からずれる） */
  button.iconBtn, a.iconBtn { padding: 0; }
  /* 8ビットテーマ: インベーダーゲーム風。黒い宇宙にネオンの緑、
     角なし・太枠・ベタ影、押し込みで沈むボタン、背景に色つきの星空 */
  :root[data-theme="pixel"] body { font-family: "Courier New",
          ui-monospace, monospace;
          background-image:
            radial-gradient(1.5px 1.5px at 24px 36px,
              rgba(255,255,255,.7) 50%, transparent 51%),
            radial-gradient(1px 1px at 130px 90px,
              rgba(0,229,255,.65) 50%, transparent 51%),
            radial-gradient(1.5px 1.5px at 220px 150px,
              rgba(0,255,102,.6) 50%, transparent 51%),
            radial-gradient(1px 1px at 320px 60px,
              rgba(255,0,229,.55) 50%, transparent 51%);
          background-size: 360px 190px; }
  :root[data-theme="pixel"] * { border-radius: 0 !important; }
  :root[data-theme="pixel"] button, :root[data-theme="pixel"] input,
  :root[data-theme="pixel"] select, :root[data-theme="pixel"] textarea {
          border-width: 2px; }
  :root[data-theme="pixel"] button {
          box-shadow: 2px 2px 0 rgba(0, 255, 102, .5); }
  :root[data-theme="pixel"] #reactionPalette button,
  :root[data-theme="pixel"] .reactions button { box-shadow: none; }
  :root[data-theme="pixel"] button:active {
          transform: translate(2px, 2px); box-shadow: none; }
  :root[data-theme="pixel"] .iconBtn { background: var(--surface); }
  :root[data-theme="pixel"] header { border-bottom: 3px solid var(--line); }
  :root[data-theme="pixel"] #tabBar button.active,
  :root[data-theme="pixel"] #profileTabs button.active,
  :root[data-theme="pixel"] .iconBtn.go,
  :root[data-theme="pixel"] #send { background: #00FF66;
          border-color: #00FF66; color: #030308; }
  :root[data-theme="pixel"] form:focus-within { border-color: #00E5FF; }
  @media (max-width: 720px) {
    :root[data-theme="pixel"] #tabBar button.active {
            color: #00FF66; background: transparent; } }
</style>
</head>
<body>
<header>
  <div class="avatar" id="avatarBtn" role="button" tabindex="0"
       title="いまの状況を見る">__AVATAR__</div>
  <div>
    <h1>クマ</h1>
    <div class="provider" id="providerName">provider: __PROVIDER__</div>
    <div id="cliNoteSlot">__CLI_PROVIDER_NOTE__</div>
    <div class="provider" id="modeName"></div>
  </div>
  <div class="sessions">
    <label for="tenantSelect">テナント</label>
    <select id="tenantSelect"></select>
    <input id="newTenant" placeholder="新しいテナント" size="10">
    <button id="createTenant" type="button" class="iconBtn sm" title="テナントを作成" aria-label="テナントを作成"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>
  </div>
  <div class="sessions">
    <label for="userSelect">ユーザー</label>
    <select id="userSelect"></select>
    <input id="newUser" placeholder="新しいユーザー" size="10">
    <button id="createUser" type="button" class="iconBtn sm" title="ユーザーを作成" aria-label="ユーザーを作成"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>
  </div>
  <div class="sessions" id="sessionBox">
    <label for="sessionSelect" id="sessionLabel">部屋</label>
    <select id="sessionSelect"></select>
    <input id="newSession" placeholder="新しい部屋" size="10">
    <button id="createSession" type="button" class="iconBtn sm" title="部屋を作成" aria-label="部屋を作成"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>
    <button id="deleteSession" type="button" class="iconBtn sm" title="部屋を削除" aria-label="部屋を削除"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg></button>
  </div>
  <div class="sessions">
    <button id="themeBtn" type="button" class="iconBtn"
            title="テーマを切り替える"></button>
  </div>
</header>
<nav id="tabBar">
  <button type="button" data-tab="chat" class="active" title="会話" aria-label="会話"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.5 8.5 0 0 1-12.4 7.5L3 20l1-5.6A8.5 8.5 0 1 1 21 11.5z"/></svg><span class="tabLabel">会話</span></button>
  <button type="button" data-tab="feed" title="フィード" aria-label="フィード"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h13v14H6a2 2 0 0 1-2-2zM17 8h3v9a2 2 0 0 1-2 2M7 9h7M7 13h7"/></svg><span class="tabLabel">フィード</span></button>
  <button type="button" data-tab="ideas" title="アイデア" aria-label="アイデア"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6M10 22h4M12 2a7 7 0 0 1 4 12.7c-.7.6-1 1-1.2 2.3H9.2C9 15.7 8.4 15.3 8 14.7A7 7 0 0 1 12 2z"/></svg><span class="tabLabel">アイデア</span></button>
  <button type="button" data-tab="goals" title="目標" aria-label="目標"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1" fill="currentColor"/></svg><span class="tabLabel">目標</span></button>
  <button type="button" data-tab="spaces" title="画面" aria-label="画面"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18"/></svg><span class="tabLabel">画面</span></button>
  <button type="button" data-tab="explore" title="探す" aria-label="探す"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg><span class="tabLabel">探す</span></button>
  <button type="button" data-tab="admin" title="管理" aria-label="管理"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 8h9M19 8h1M4 16h3M13 16h7"/><circle cx="16" cy="8" r="2"/><circle cx="10" cy="16" r="2"/></svg><span class="tabLabel">管理</span></button>
</nav>
<section id="adminMenu" hidden>
  <h2>管理</h2>
  <div class="adminBtns">
    <button id="tenantAdminBtn" type="button" class="iconBtn" title="テナント管理" aria-label="テナント管理"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 21V5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v16M15 10h3a1 1 0 0 1 1 1v10M3 21h18M9 21v-4h2v4M8 7h2M8 11h2"/></svg></button>
    <button id="outputsBtn" type="button" class="iconBtn" title="成果物" aria-label="成果物"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 8l-9-5-9 5v8l9 5 9-5zM3 8l9 5 9-5M12 13v8"/></svg></button>
    <button id="profileBtn" type="button" class="iconBtn" title="人格・記憶" aria-label="人格・記憶"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V4H6.5A2.5 2.5 0 0 0 4 6.5zM4 19.5A2.5 2.5 0 0 0 6.5 22H20v-5"/></svg></button>
    <button id="usageBtn" type="button" class="iconBtn" title="使用量" aria-label="使用量"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 20v-6M11 20V5M17 20v-9M3 20h18"/></svg></button>
    <button id="activityBtn" type="button" class="iconBtn" title="活動の記録" aria-label="活動の記録"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg></button>
    <button id="exportBtn" type="button" class="iconBtn" title="書き出し" aria-label="書き出し"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M6 10l6-6 6 6M4 20h16"/></svg></button>
    <button id="importBtn" type="button" class="iconBtn" title="復元" aria-label="復元"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12M6 10l6 6 6-6M4 20h16"/></svg></button>
    <input id="importFile" type="file" accept=".zip" hidden>
    __LOGOUT_BUTTON__
  </div>
</section>
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
    <button id="profileSave" type="button" class="iconBtn sm" title="保存" aria-label="保存"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg></button>
    <span id="profileStatus"></span>
  </div>
  <h3>アバター</h3>
  <p class="profileNote">クマの顔に使う画像です。アップロードした画像が、そのまま今のアバターになります（PNG / JPEG / GIF / WebP / SVG、2MB まで）。</p>
  <div class="avatarBox">
    <img id="avatarPreview" class="avatarPreview" alt="今のアバター">
    <div id="avatarRows"></div>
  </div>
  <div class="profileActions">
    <input id="avatarFile" type="file" accept="image/png,image/jpeg,image/gif,image/webp,image/svg+xml" hidden>
    <button id="avatarUpload" type="button">画像をアップロード</button>
    <button id="avatarReset" type="button">元に戻す</button>
    <span id="avatarStatus"></span>
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
<section id="activityPanel" hidden>
  <h2>活動の記録</h2>
  <p class="activityNote">道具の呼び出し・承認・外への送信など、クマの活動の記録です（新しい順）。</p>
  <div id="activityRows"></div>
</section>
<section id="statusPanel" hidden>
  <h2>いまの状況</h2>
  <p class="statusLine">状態: <span id="statusNowText">待機中</span></p>
  <ul id="statusFacts"></ul>
  <div id="statusSteps"></div>
</section>
<section id="tenantPanel" hidden>
  <h2>テナント管理</h2>
  <div id="tenantRows"></div>
  <div id="tenantDeleteConfirm" hidden></div>
  <div class="adminNew">
    <input id="adminNewTenant" placeholder="新しいテナント名" size="14">
    <button id="adminCreateTenant" type="button" class="iconBtn sm" title="テナントを作成" aria-label="テナントを作成"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>
  </div>
  <h3>知らせ方の好み（現在のユーザー）</h3>
  <div id="notifyMutedRows"></div>
  <div class="adminNew">
    <input id="notifyNewTopic" placeholder="知らせない話題" size="14">
    <button id="notifyAddTopic" type="button" class="iconBtn sm" title="話題を追加" aria-label="話題を追加"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg></button>
  </div>
  <div class="adminNew">
    <label>静かな時間帯:
      <input id="notifyQuietStart" type="time"> 〜
      <input id="notifyQuietEnd" type="time"></label>
    <button id="notifySaveQuiet" type="button" class="iconBtn sm" title="時間帯を保存" aria-label="時間帯を保存"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg></button>
    <button id="notifyClearQuiet" type="button" class="iconBtn sm" title="時間帯を解除" aria-label="時間帯を解除"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
  </div>
</section>
<section id="feedPanel" hidden>
  <h2>フィード</h2>
  <div id="feedRows"></div>
</section>
<section id="ideasPanel" hidden>
  <h2>アイデア</h2>
  <p class="ideasNote">クマからの提案をカードで並べます。提案は「提案をもらう」を押したときだけ考えます。</p>
  <div class="ideaTools">
    <button id="ideaGenerate" type="button">提案をもらう</button>
    <span id="ideaStatus"></span>
  </div>
  <div id="ideaRows"></div>
</section>
<section id="goalsPanel" hidden>
  <h2>目標</h2>
  <p class="goalsNote">長期の目標と進捗を残します。進み具合は、紐づけた用事の完了割合で自動で出ます。</p>
  <div id="goalRows"></div>
  <h3>目標を立てる</h3>
  <div class="goalNew">
    <input id="goalNewTitle" placeholder="目標のタイトル" size="24">
    <input id="goalNewDesc" placeholder="ひとこと説明（任意）" size="24">
    <button id="goalCreate" type="button">追加する</button>
  </div>
  <p id="goalStatus"></p>
</section>
<section id="spacesPanel" hidden>
  <h2>画面</h2>
  <p class="spacesNote">会話の中で作った小さな画面（記録表やボードなど）が残ります。クマに頼んで作る・更新するほか、ここで直接作ることもできます。</p>
  <div id="spaceRows"></div>
  <div id="spaceView" hidden>
    <h3 id="spaceViewTitle"></h3>
    <iframe id="spaceFrame" sandbox="allow-scripts" title="画面の内容"></iframe>
  </div>
  <h3 id="spaceEditHead">画面を作る</h3>
  <div class="spaceEdit">
    <input id="spaceNewTitle" placeholder="画面のタイトル" size="24">
    <textarea id="spaceNewHtml" placeholder="HTML（完結した文書）" rows="6"></textarea>
    <div class="spaceEditTools">
      <button id="spaceSave" type="button">保存する</button>
      <button id="spaceEditCancel" type="button" hidden>編集をやめる</button>
    </div>
  </div>
  <p id="spaceStatus"></p>
</section>
<section id="approvalPanel" hidden>
  <h2>承認の確認</h2>
  <div id="approvalRows"></div>
</section>
<section id="choicePanel" hidden>
  <h2>質問</h2>
  <div id="choiceRows"></div>
</section>
<div id="log"></div>
<div id="dropHint">ファイルを離すと添付します</div>
<form id="form">
  <div id="statusBox" hidden><span class="dot">・</span><span class="dot">・</span><span class="dot">・</span><span id="statusText"></span></div>
  <div id="quoteBox" hidden>
    <span id="quoteText"></span>
    <button id="clearQuote" type="button" class="iconBtn sm" title="引用を解除" aria-label="引用を解除"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>
  </div>
  <div id="files"></div>
  <button id="attach" type="button" class="iconBtn" title="ファイルを添付" aria-label="ファイルを添付"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.4 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 1 1 5.66 5.66l-9.19 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg></button>
  <button id="record" type="button" class="iconBtn" title="録音" aria-label="録音"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 19v3"/></svg></button>
  <span id="recordStatus" hidden></span>
  <input id="file" type="file" multiple hidden>
  <input id="input" autocomplete="off" placeholder="話しかけてください">
  <button id="send" type="submit" class="iconBtn go" title="送信" aria-label="送信"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20V4M5 11l7-7 7 7"/></svg></button>
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
const importBtn = document.getElementById('importBtn');
const importFile = document.getElementById('importFile');
const usagePanel = document.getElementById('usagePanel');
const usageTotal = document.getElementById('usageTotal');
const usageProviderRows = document.getElementById('usageProviderRows');
const usageDayRows = document.getElementById('usageDayRows');
const activityBtn = document.getElementById('activityBtn');
const activityPanel = document.getElementById('activityPanel');
const activityRows = document.getElementById('activityRows');
const profileTabs = document.getElementById('profileTabs');
const profileText = document.getElementById('profileText');
const profileSaveBtn = document.getElementById('profileSave');
const profileStatus = document.getElementById('profileStatus');
const avatarPreview = document.getElementById('avatarPreview');
const avatarRows = document.getElementById('avatarRows');
const avatarFileInput = document.getElementById('avatarFile');
const avatarUploadBtn = document.getElementById('avatarUpload');
const avatarResetBtn = document.getElementById('avatarReset');
const avatarStatus = document.getElementById('avatarStatus');
const profileFiles = [
  {name: 'SOUL.md', label: '性格（SOUL.md）'},
  {name: 'IDENTITY.md', label: '自己紹介（IDENTITY.md）'},
  {name: 'USER.md', label: 'あなたについて（USER.md）'},
  {name: 'MEMORY.md', label: '長期記憶（MEMORY.md）'},
];
let currentProfileFile = 'SOUL.md';
const tenantRows = document.getElementById('tenantRows');
const approvalPanel = document.getElementById('approvalPanel');
const choicePanel = document.getElementById('choicePanel');
const avatarBtn = document.getElementById('avatarBtn');
const statusPanel = document.getElementById('statusPanel');
const statusNowText = document.getElementById('statusNowText');
const statusFacts = document.getElementById('statusFacts');
const tabBar = document.getElementById('tabBar');
const dropHint = document.getElementById('dropHint');
const adminMenu = document.getElementById('adminMenu');
const feedPanel = document.getElementById('feedPanel');
const feedRows = document.getElementById('feedRows');
const ideasPanel = document.getElementById('ideasPanel');
const ideaRows = document.getElementById('ideaRows');
const ideaGenerateBtn = document.getElementById('ideaGenerate');
const ideaStatus = document.getElementById('ideaStatus');
const goalsPanel = document.getElementById('goalsPanel');
const goalRows = document.getElementById('goalRows');
const goalNewTitle = document.getElementById('goalNewTitle');
const goalNewDesc = document.getElementById('goalNewDesc');
const goalCreateBtn = document.getElementById('goalCreate');
const goalStatus = document.getElementById('goalStatus');
const spacesPanel = document.getElementById('spacesPanel');
const spaceRows = document.getElementById('spaceRows');
const spaceView = document.getElementById('spaceView');
const spaceViewTitle = document.getElementById('spaceViewTitle');
const spaceFrame = document.getElementById('spaceFrame');
const spaceEditHead = document.getElementById('spaceEditHead');
const spaceNewTitle = document.getElementById('spaceNewTitle');
const spaceNewHtml = document.getElementById('spaceNewHtml');
const spaceSaveBtn = document.getElementById('spaceSave');
const spaceEditCancelBtn = document.getElementById('spaceEditCancel');
const spaceStatus = document.getElementById('spaceStatus');
let spaceEditingId = null;
let activeTab = 'chat';
let lastApprovals = [];
let lastChoices = [];

// タブの切り替え。会話タブだけが会話ログと入力欄を持ち、探すタブは
// 成果物、フィードタブは能動発信と用事の進み具合の時系列、アイデア
// タブは提案カード、目標タブは長期の目標と進捗、画面タブは残して
// おける小さな画面、管理タブは管理メニューとその配下のパネルを出す
function showTab(name) {
  activeTab = name;
  for (const btn of tabBar.querySelectorAll('button')) {
    btn.classList.toggle('active', btn.dataset.tab === name);
  }
  const chat = name === 'chat';
  log.hidden = !chat;
  dropHint.hidden = !chat;
  form.hidden = !chat;
  if (chat) {
    renderApprovals(lastApprovals);
    renderChoices(lastChoices);
  } else {
    approvalPanel.hidden = true;
    choicePanel.hidden = true;
  }
  feedPanel.hidden = name !== 'feed';
  if (name === 'feed') loadFeed();
  ideasPanel.hidden = name !== 'ideas';
  if (name === 'ideas') loadIdeas();
  goalsPanel.hidden = name !== 'goals';
  if (name === 'goals') loadGoals();
  spacesPanel.hidden = name !== 'spaces';
  if (name === 'spaces') loadSpaces();
  outputsPanel.hidden = name !== 'explore';
  if (name === 'explore') loadOutputs();
  adminMenu.hidden = name !== 'admin';
  if (name !== 'admin') {
    tenantPanel.hidden = true;
    profilePanel.hidden = true;
    usagePanel.hidden = true;
    activityPanel.hidden = true;
  }
}

for (const btn of tabBar.querySelectorAll('button')) {
  btn.addEventListener('click', () => showTab(btn.dataset.tab));
}
const approvalRows = document.getElementById('approvalRows');
const choiceRows = document.getElementById('choiceRows');
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

function addMsg(role, text, quote = null, quoteSource = null,
                rid = null, reactions = [], agentReaction = null) {
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
  const actions = document.createElement('div');
  actions.className = 'msgActions';
  const quoteBtn = document.createElement('button');
  quoteBtn.type = 'button';
  quoteBtn.className = 'quote-btn';
  setIcon(quoteBtn, 'quote', '引用');
  quoteBtn.addEventListener('click', () => setQuote(quoteSource || text));
  actions.appendChild(quoteBtn);
  if (role === 'assistant') {
    const speakBtn = document.createElement('button');
    speakBtn.type = 'button';
    speakBtn.className = 'speak-btn';
    setIcon(speakBtn, 'speaker', '読み上げ');
    speakBtn.addEventListener('click', () => toggleSpeak(speakBtn, text));
    actions.appendChild(speakBtn);
  }
  div.appendChild(actions);
  if (rid !== null && rid !== undefined) {
    attachReactions(div, rid, reactions, agentReaction);
  }
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

// リアクション: 発話に絵文字を付け外しする。保存はサーバー側
// （/api/reactions）で、宛先は会話記録での位置（rid）
const REACTION_EMOJIS = ['👍', '❤️', '😂', '😮', '🎉', '🙏'];
let reactionPalette = null;

function renderReactionChips(box, rid, reactions, agentReaction) {
  box.innerHTML = '';
  for (const emoji of reactions) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'reactionChip';
    chip.textContent = emoji;
    chip.title = 'リアクションを外す';
    chip.setAttribute('aria-label', 'リアクション ' + emoji + ' を外す');
    chip.addEventListener('click', () => toggleReaction(rid, emoji, box));
    box.appendChild(chip);
  }
  if (agentReaction) {
    // クマのリアクション返し。アバターの印とブランド色の縁取りで
    // 利用者のチップと見分けられるようにし、クリックでは外れない
    const chip = document.createElement('span');
    chip.className = 'reactionChip agentChip';
    chip.title = 'クマのリアクション';
    chip.setAttribute('aria-label', 'クマのリアクション ' + agentReaction);
    const mark = document.createElement('span');
    mark.className = 'agentMark';
    const avatarFace = document.querySelector('#avatarBtn img, #avatarBtn svg');
    if (avatarFace) mark.appendChild(avatarFace.cloneNode(true));
    chip.appendChild(mark);
    chip.appendChild(document.createTextNode(agentReaction));
    box.appendChild(chip);
  }
  box.hidden = reactions.length === 0 && !agentReaction;
}

function attachReactions(div, rid, reactions, agentReaction) {
  let row = div.querySelector('.msgActions');
  if (!row) {
    row = document.createElement('div');
    row.className = 'msgActions';
    div.appendChild(row);
  }
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'react-btn';
  setIcon(btn, 'smile', 'リアクション');
  btn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    openReactionPalette(btn, div, rid);
  });
  row.appendChild(btn);
  const box = document.createElement('div');
  box.className = 'reactions';
  row.appendChild(box);
  div._reactionBox = box;
  renderReactionChips(box, rid, reactions || [], agentReaction || null);
}

function openReactionPalette(anchor, div, rid) {
  closeReactionPalette();
  const palette = document.createElement('div');
  palette.id = 'reactionPalette';
  for (const emoji of REACTION_EMOJIS) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = emoji;
    b.setAttribute('aria-label', 'リアクション ' + emoji);
    b.addEventListener('click', () => {
      toggleReaction(rid, emoji, div._reactionBox);
      closeReactionPalette();
    });
    palette.appendChild(b);
  }
  document.body.appendChild(palette);
  const rect = anchor.getBoundingClientRect();
  palette.style.left = Math.max(8,
    Math.min(rect.left - 8, window.innerWidth - 264)) + 'px';
  palette.style.top = (rect.bottom + 6) + 'px';
  reactionPalette = palette;
}

function closeReactionPalette() {
  if (reactionPalette) {
    reactionPalette.remove();
    reactionPalette = null;
  }
}

document.addEventListener('click', (ev) => {
  if (reactionPalette && !reactionPalette.contains(ev.target)) {
    closeReactionPalette();
  }
});

async function toggleReaction(rid, emoji, box) {
  try {
    const res = await fetch('/api/reactions', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tenant: currentTenant, user: currentUser,
                            session: currentSession, rid, emoji}),
    });
    if (!res.ok) throw new Error('reaction failed');
    const data = await res.json();
    if (box) renderReactionChips(box, rid, data.reactions || [],
                                 data.agent_reaction || null);
  } catch (e) { /* 付け外しに失敗したら表示はそのままにする */ }
}

// 読み上げ: ブラウザの speechSynthesis（ja-JP）で読む。読み上げ中に
// 同じボタンか別の発話のボタンを押したら、いったん止めてから
// （別の発話なら）そちらを読む。サーバー側の処理は無い
function stopSpeaking() {
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
  if (speakingBtn) {
    setIcon(speakingBtn, 'speaker', '読み上げ');
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
      setIcon(btn, 'speaker', '読み上げ');
      speakingBtn = null;
    }
  };
  utterance.onend = done;
  utterance.onerror = done;
  speakingBtn = btn;
  setIcon(btn, 'stop', '止める');
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
        addMsg(m.role, shown, m.quote || null, m.content || shown,
               (m.rid === undefined ? null : m.rid), m.reactions || [],
               m.agent_reaction || null);
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
  refreshAvatarFace();
  loadMeta();
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
  refreshAvatarFace();
  loadMeta();
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

// いまの状況パネル。アバターを押すと開き、開いている間は数秒ごとに
// 状態を取り直す。状態は /api/status（用事の件数つき）、承認待ちは
// /api/approvals、使用量は /api/usage から集める
let statusPanelTimer = null;

async function refreshStatusPanel() {
  const q = '?tenant=' + encodeURIComponent(currentTenant)
    + '&user=' + encodeURIComponent(currentUser);
  try {
    const res = await fetch('/api/status' + q
      + '&session=' + encodeURIComponent(currentSession));
    const data = await res.json();
    statusNowText.textContent = data.status || '待機中';
    const facts = [];
    if (data.tasks) {
      facts.push('用事の一覧: ' + data.tasks.total
        + ' 件（うち未完了 ' + data.tasks.open + ' 件）');
    }
    try {
      const aRes = await fetch('/api/approvals' + q);
      const aData = await aRes.json();
      const n = (aData.approvals || []).length;
      facts.push(n ? '承認待ち: ' + n + ' 件あります'
                   : '承認待ち: ありません');
    } catch (e) { /* 承認待ちが取れなくても他の項目は出す */ }
    try {
      const uRes = await fetch('/api/usage' + q);
      const uData = await uRes.json();
      if (uData.calls) {
        facts.push('頭脳の使用量（累計）: 呼び出し '
          + formatCount(uData.calls) + ' 回'
          + (uData.has_tokens
            ? '、合計 ' + formatCount(uData.total_tokens) + ' トークン'
            : ''));
      }
    } catch (e) { /* 使用量が取れなくても他の項目は出す */ }
    const room = sessionSelect.selectedOptions.length
      ? sessionSelect.selectedOptions[0].textContent : currentSession;
    facts.push('いまの場所: テナント ' + currentTenant
      + ' ／ ユーザー ' + currentUser + ' ／ 部屋 ' + room);
    statusFacts.innerHTML = '';
    for (const fact of facts) {
      const li = document.createElement('li');
      li.textContent = fact;
      statusFacts.appendChild(li);
    }
    renderStatusSteps(data.steps || []);
  } catch (e) { /* 状態が取れなければ前回の表示のままにする */ }
}

// 作業の手順（エージェントの作業メモ）を状況パネルに出す。
// 手順が無いときは何も出さない。状態の印は文字で表し、進行中の
// 手順は太字、終わった手順は薄くする
const stepMarks = {completed: '✓', in_progress: '▶', pending: '○',
                   blocked: '!', cancelled: '―'};

function renderStatusSteps(steps) {
  const box = document.getElementById('statusSteps');
  box.innerHTML = '';
  if (!steps.length) return;
  const h = document.createElement('h3');
  h.textContent = '作業の手順';
  box.appendChild(h);
  const ul = document.createElement('ul');
  for (const step of steps) {
    const li = document.createElement('li');
    if (step.status === 'in_progress') li.className = 'doing';
    if (step.status === 'completed' || step.status === 'cancelled') {
      li.className = 'done';
    }
    li.textContent = (stepMarks[step.status] || '○') + ' ' + step.text;
    ul.appendChild(li);
  }
  box.appendChild(ul);
}

function toggleStatusPanel() {
  statusPanel.hidden = !statusPanel.hidden;
  if (!statusPanel.hidden) {
    refreshStatusPanel();
    statusPanelTimer = setInterval(refreshStatusPanel, 3000);
  } else if (statusPanelTimer) {
    clearInterval(statusPanelTimer);
    statusPanelTimer = null;
  }
}

avatarBtn.addEventListener('click', toggleStatusPanel);
avatarBtn.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault();
    toggleStatusPanel();
  }
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
  showTab('explore');
});

// 人格・記憶パネル。現在のテナント×ユーザーの人格ファイル
// （SOUL / IDENTITY / USER）と長期記憶（MEMORY）を切り替えて
// 編集する。読み書きは /api/profile を使い、保存した内容は
// 新しい会話から反映される（パネル内の案内文のとおり）
profileBtn.addEventListener('click', () => {
  profilePanel.hidden = !profilePanel.hidden;
  if (!profilePanel.hidden) {
    loadProfileFile(currentProfileFile);
    loadAvatar();
  }
});

// アバターの管理（人格・記憶パネルの下段）。候補の一覧と
// 「今のアバター」は /api/avatar、アップロードは添付と同じ
// base64 の形で /api/avatar/upload、切り替え・削除・既定への
// 復帰は /api/avatar/act。変わったらヘッダーの顔も読み直す
function avatarAction(payload, doneMessage) {
  return fetch('/api/avatar/act', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign(
      {tenant: currentTenant, user: currentUser}, payload)),
  }).then(async (res) => {
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '操作に失敗しました');
    renderAvatar(data);
    refreshAvatarFace();
    avatarStatus.textContent = doneMessage || '';
    return true;
  }).catch((e) => {
    avatarStatus.textContent = '操作に失敗しました: ' + (e.message || e);
    return false;
  });
}

function renderAvatar(data) {
  avatarPreview.src = avatarImageUrl('');
  const items = data.items || [];
  const currentId = data.current ? data.current.id : null;
  avatarRows.innerHTML = '';
  if (!items.length) {
    const row = document.createElement('div');
    row.className = 'avatarRow';
    row.textContent = data.current ? '' : '今は既定の顔です';
    avatarRows.appendChild(row);
    return;
  }
  for (const item of items) {
    const row = document.createElement('div');
    row.className = 'avatarRow';
    const thumb = document.createElement('img');
    thumb.src = avatarImageUrl('&id=' + encodeURIComponent(item.id));
    thumb.alt = '';
    row.appendChild(thumb);
    const time = document.createElement('span');
    time.textContent = item.uploaded_at
      ? String(item.uploaded_at).replace('T', ' ').slice(0, 16) : '';
    row.appendChild(time);
    if (item.id === currentId) {
      const badge = document.createElement('span');
      badge.className = 'nowBadge';
      badge.textContent = '使用中';
      row.appendChild(badge);
    } else {
      const useBtn = document.createElement('button');
      useBtn.type = 'button';
      useBtn.textContent = '選ぶ';
      useBtn.addEventListener('click', () => {
        avatarAction({action: 'select', id: item.id},
                     'アバターを切り替えました');
      });
      row.appendChild(useBtn);
    }
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.textContent = '削除';
    delBtn.addEventListener('click', () => {
      if (!confirm('この画像を削除しますか？')) return;
      avatarAction({action: 'delete', id: item.id}, '削除しました');
    });
    row.appendChild(delBtn);
    avatarRows.appendChild(row);
  }
}

async function loadAvatar() {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/avatar' + query);
    const data = await res.json();
    renderAvatar(data);
  } catch (e) { /* 取れなければパネルは空のままにする */ }
}

avatarUploadBtn.addEventListener('click', () => avatarFileInput.click());

avatarFileInput.addEventListener('change', async () => {
  const file = avatarFileInput.files && avatarFileInput.files[0];
  avatarFileInput.value = '';
  if (!file) return;
  try {
    const data = await readAttachment(file);
    const res = await fetch('/api/avatar/upload', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign(
        {tenant: currentTenant, user: currentUser}, data)),
    });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || 'アップロードに失敗しました');
    renderAvatar(payload);
    refreshAvatarFace();
    avatarStatus.textContent = 'アバターを変更しました';
  } catch (e) {
    avatarStatus.textContent = '操作に失敗しました: ' + (e.message || e);
  }
});

avatarResetBtn.addEventListener('click', () => {
  avatarAction({action: 'reset'}, '既定の顔に戻しました');
});

// 使用量パネル。現在のテナント×ユーザーの頭脳の使用量（呼び出し
// 回数とトークン数）を合計・頭脳別・日別で表示する。読み取りは
// /api/usage を使い、パネルを開くたびに取り直す。費用への換算は
// しない（単価が頭脳ごとに異なるため、トークン数だけを見せる）
usageBtn.addEventListener('click', () => {
  usagePanel.hidden = !usagePanel.hidden;
  if (!usagePanel.hidden) loadUsage();
});

// 活動の記録パネル。現在のテナント×ユーザーの活動（道具の呼び
// 出し・承認・外への送信・CLI の起動）を新しい順に表示する。
// 読み取りは /api/activity を使い、パネルを開くたびに取り直す
activityBtn.addEventListener('click', () => {
  activityPanel.hidden = !activityPanel.hidden;
  if (!activityPanel.hidden) loadActivity();
});

const ACTIVITY_KIND_LABELS = {
  tool_call: '道具の呼び出し',
  approval: '承認',
  external_send: '外への送信',
  cli_launch: 'CLI の起動',
};

function activityText(entry) {
  // 1 件分を表示用の 1 行に組み立てる
  const parts = [];
  if (entry.kind === 'tool_call') {
    parts.push(entry.tool || '');
    if (entry.summary) parts.push(entry.summary);
    if (entry.result) parts.push('→ ' + entry.result);
  } else if (entry.kind === 'approval') {
    parts.push(entry.tool || '');
    if (entry.summary) parts.push(entry.summary);
    parts.push(entry.result === 'approved' ? '（承認されました）'
                                            : '（却下されました）');
  } else if (entry.kind === 'external_send') {
    parts.push((entry.tool || '') + ' へ送信');
    if (entry.summary) parts.push(entry.summary);
  } else if (entry.kind === 'cli_launch') {
    parts.push(entry.tool || '');
    if (entry.summary) parts.push(entry.summary);
  } else {
    if (entry.tool) parts.push(entry.tool);
    if (entry.summary) parts.push(entry.summary);
    if (entry.result) parts.push(entry.result);
  }
  return parts.filter(Boolean).join(' ');
}

async function loadActivity() {
  activityRows.innerHTML = '';
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/activity' + query);
    const data = await res.json();
    const entries = data.entries || [];
    if (!entries.length) {
      const p = document.createElement('p');
      p.textContent = '活動の記録はまだありません';
      activityRows.appendChild(p);
      return;
    }
    for (const entry of entries) {
      const row = document.createElement('div');
      row.className = 'activityRow';
      const time = document.createElement('span');
      time.className = 'activityTime';
      time.textContent = String(entry.ts || '').replace('T', ' ');
      row.appendChild(time);
      row.appendChild(document.createTextNode(' '));
      const kind = document.createElement('span');
      kind.className = 'activityKind';
      kind.textContent = ACTIVITY_KIND_LABELS[entry.kind] || entry.kind || '';
      row.appendChild(kind);
      row.appendChild(document.createTextNode(' ' + activityText(entry)));
      activityRows.appendChild(row);
    }
  } catch (e) { /* 取れなければパネルは空のままにする */ }
}

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

// 復元ボタン。書き出し ZIP を選ぶと、現在のテナント×ユーザーの
// データをその内容で置き換える（POST /api/import。ZIP は添付と
// 同じ流儀で base64 化して送る）。置き換えの前に確認を取り、
// サーバー側では復元前の控えが自動で取られる。復元した内容は
// 新しい会話から反映される（開いている会話の表示はそのまま）
importBtn.addEventListener('click', () => importFile.click());

importFile.addEventListener('change', async () => {
  const file = importFile.files[0];
  importFile.value = '';
  if (!file) return;
  if (!confirm('復元すると、現在のテナント×ユーザーのデータは ZIP の内容で置き換えられます。復元前に現在のデータの控えが自動で取られます。続けますか？')) return;
  try {
    const item = await readAttachment(file);
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/import' + query, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({archive: item.data_base64}),
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || '復元に失敗しました');
    }
    let msg = '復元しました（ファイル ' + data.restored_files
      + ' 件）。新しい会話から反映されます。';
    if (data.skipped_files) {
      msg += '\\n他のユーザー／テナントの置き場にあたる '
        + data.skipped_files + ' 件は飛ばしました。';
    }
    if (data.backup) msg += '\\n復元前の控え: ' + data.backup;
    alert(msg);
  } catch (e) {
    alert(e.message || '復元に失敗しました');
  }
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

// フィード（能動発信と用事の進み具合の時系列）。タブを開くたびに
// /api/feed で新しい順に取り直す。種別の印は小さなバッジで示す
const FEED_KIND_LABELS = {briefing: '朝のまとめ', proactive: 'お知らせ',
                          task: '用事の動き'};

async function loadFeed() {
  feedRows.innerHTML = '';
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/feed' + query);
    const data = await res.json();
    const entries = data.entries || [];
    if (!entries.length) {
      const row = document.createElement('div');
      row.className = 'feedRow';
      row.textContent = 'フィードはまだありません';
      feedRows.appendChild(row);
      return;
    }
    for (const item of entries) {
      const row = document.createElement('div');
      row.className = 'feedRow';
      row.dataset.kind = item.kind || '';
      const badge = document.createElement('span');
      badge.className = 'fkind';
      badge.textContent = FEED_KIND_LABELS[item.kind] || item.kind || '';
      row.appendChild(badge);
      const time = document.createElement('span');
      time.className = 'ftime';
      time.textContent = item.ts
        ? String(item.ts).replace('T', ' ').slice(0, 16) : '';
      row.appendChild(time);
      if (item.title) {
        const title = document.createElement('span');
        title.className = 'ftitle';
        title.textContent = item.title;
        row.appendChild(title);
      }
      const body = document.createElement('div');
      body.className = 'fbody';
      body.textContent = item.body || '';
      row.appendChild(body);
      feedRows.appendChild(row);
    }
  } catch (e) { /* 一覧が取れなければパネルは空のままにする */ }
}

// アイデア（クマからの提案カード）。タブを開くたびに /api/ideas で
// 取り直す。提案の生成は「提案をもらう」ボタンを押したときだけ
// （/api/ideas/generate）。カードの「やってみる」は用事の一覧への
// 登録、「やめておく」は一覧から外す（/api/ideas/act）
function renderIdeas(ideas) {
  ideaRows.innerHTML = '';
  if (!ideas.length) {
    const row = document.createElement('div');
    row.className = 'ideaRow';
    row.textContent = 'アイデアはまだありません';
    ideaRows.appendChild(row);
    return;
  }
  for (const item of ideas) {
    const card = document.createElement('div');
    card.className = 'ideaRow';
    const head = document.createElement('div');
    head.className = 'ihead';
    const title = document.createElement('span');
    title.className = 'ititle';
    title.textContent = item.title || '';
    head.appendChild(title);
    const time = document.createElement('span');
    time.className = 'itime';
    time.textContent = item.created_at
      ? String(item.created_at).replace('T', ' ').slice(0, 16) : '';
    head.appendChild(time);
    if (item.status === 'accepted') {
      const badge = document.createElement('span');
      badge.className = 'istatus';
      badge.textContent = '用事にしました';
      head.appendChild(badge);
    }
    card.appendChild(head);
    const body = document.createElement('div');
    body.className = 'ibody';
    body.textContent = item.body || '';
    card.appendChild(body);
    if (item.reason) {
      const reason = document.createElement('div');
      reason.className = 'ireason';
      reason.textContent = 'きっかけ: ' + item.reason;
      card.appendChild(reason);
    }
    if (item.status === 'new') {
      const actions = document.createElement('div');
      actions.className = 'iactions';
      const acceptBtn = document.createElement('button');
      acceptBtn.type = 'button';
      acceptBtn.textContent = 'やってみる';
      acceptBtn.addEventListener('click', () => actIdea(item.id, 'accept'));
      actions.appendChild(acceptBtn);
      const dismissBtn = document.createElement('button');
      dismissBtn.type = 'button';
      dismissBtn.textContent = 'やめておく';
      dismissBtn.addEventListener('click', () => actIdea(item.id, 'dismiss'));
      actions.appendChild(dismissBtn);
      card.appendChild(actions);
    }
    ideaRows.appendChild(card);
  }
}

async function loadIdeas() {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/ideas' + query);
    const data = await res.json();
    renderIdeas(data.ideas || []);
  } catch (e) { /* 一覧が取れなければパネルは空のままにする */ }
}

async function actIdea(id, action) {
  try {
    const res = await fetch('/api/ideas/act', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tenant: currentTenant, user: currentUser,
                            id: id, action: action}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '操作に失敗しました');
    renderIdeas(data.ideas || []);
    ideaStatus.textContent = action === 'accept'
      ? '用事の一覧に登録しました' : 'やめておきました';
  } catch (e) {
    ideaStatus.textContent = '操作に失敗しました: ' + (e.message || e);
  }
}

ideaGenerateBtn.addEventListener('click', async () => {
  ideaGenerateBtn.disabled = true;
  ideaStatus.textContent = '提案を考えています…';
  try {
    const res = await fetch('/api/ideas/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tenant: currentTenant, user: currentUser}),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '提案をもらえませんでした');
    renderIdeas(data.ideas || []);
    ideaStatus.textContent = data.generation
      ? (data.generation.message || '') : '';
  } catch (e) {
    ideaStatus.textContent = '提案をもらえませんでした: ' + (e.message || e);
  } finally {
    ideaGenerateBtn.disabled = false;
  }
});

// 目標（長期の目標と進捗）。タブを開くたびに /api/goals で取り直す。
// 進み具合は紐づけた用事の完了割合。追加・編集・メモの追加・用事の
// 紐づけ／解除・削除は /api/goals/act で行い、応答の一覧で画面を
// 描き直す
function goalAction(payload, doneMessage) {
  return fetch('/api/goals/act', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign(
      {tenant: currentTenant, user: currentUser}, payload)),
  }).then(async (res) => {
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '操作に失敗しました');
    renderGoals(data);
    goalStatus.textContent = doneMessage || '';
    return true;
  }).catch((e) => {
    goalStatus.textContent = '操作に失敗しました: ' + (e.message || e);
    return false;
  });
}

function renderGoals(data) {
  const goals = data.goals || [];
  const allTasks = data.tasks || [];
  goalRows.innerHTML = '';
  if (!goals.length) {
    const row = document.createElement('div');
    row.className = 'goalRow';
    row.textContent = '目標はまだありません';
    goalRows.appendChild(row);
    return;
  }
  for (const goal of goals) renderGoal(goal, allTasks);
}

function renderGoal(goal, allTasks) {
  const card = document.createElement('div');
  card.className = 'goalRow' + (goal.status === 'done' ? ' done' : '');
  const head = document.createElement('div');
  head.className = 'ghead';
  const title = document.createElement('span');
  title.className = 'gtitle';
  title.textContent = goal.title || '';
  head.appendChild(title);
  const badge = document.createElement('span');
  badge.className = 'gstatus' + (goal.status === 'done' ? ' done' : '');
  badge.textContent = goal.status === 'done' ? '達成' : '進行中';
  head.appendChild(badge);
  const time = document.createElement('span');
  time.className = 'gtime';
  time.textContent = goal.created_at
    ? String(goal.created_at).replace('T', ' ').slice(0, 16) : '';
  head.appendChild(time);
  card.appendChild(head);
  if (goal.description) {
    const desc = document.createElement('div');
    desc.className = 'gdesc';
    desc.textContent = goal.description;
    card.appendChild(desc);
  }
  // 進み具合（紐づけた用事の完了割合。用事が無ければ割合なし）
  const progress = document.createElement('div');
  progress.className = 'gprogress';
  const p = goal.progress || {total: 0, done: 0, ratio: null};
  if (p.total > 0) {
    const pct = Math.round((p.ratio || 0) * 100);
    progress.textContent = '進み具合: 用事 ' + p.total + ' 件中 '
      + p.done + ' 件が完了（' + pct + '%）';
    const bar = document.createElement('div');
    bar.className = 'gbar';
    const fill = document.createElement('div');
    fill.className = 'gbarFill';
    fill.style.width = pct + '%';
    bar.appendChild(fill);
    card.appendChild(progress);
    card.appendChild(bar);
  } else {
    progress.textContent = '進み具合: 用事がまだ紐づいていません';
    card.appendChild(progress);
  }
  // 紐づいた用事
  const tasksBox = document.createElement('div');
  tasksBox.className = 'gtasks';
  for (const task of goal.tasks || []) {
    const row = document.createElement('div');
    row.className = 'gtask';
    const tTitle = document.createElement('span');
    tTitle.textContent = task.title || '';
    row.appendChild(tTitle);
    const tState = document.createElement('span');
    tState.className = 'tstate';
    tState.textContent = task.status === 'done' ? '完了' : '進行中';
    row.appendChild(tState);
    const unBtn = document.createElement('button');
    unBtn.type = 'button';
    unBtn.textContent = '紐づけを外す';
    unBtn.addEventListener('click', () => goalAction(
      {action: 'unlink', task_id: task.id}, '紐づけを外しました'));
    row.appendChild(unBtn);
    tasksBox.appendChild(row);
  }
  // 紐づけの選択肢（この目標にまだ紐づいていない用事から選ぶ）
  const candidates = allTasks.filter((t) => t.goal_id !== goal.id);
  if (candidates.length) {
    const row = document.createElement('div');
    row.className = 'gtask';
    const select = document.createElement('select');
    for (const task of candidates) {
      const opt = document.createElement('option');
      opt.value = task.id;
      opt.textContent = task.title
        + (task.goal_id ? '（別の目標に紐づけ中）' : '');
      select.appendChild(opt);
    }
    row.appendChild(select);
    const linkBtn = document.createElement('button');
    linkBtn.type = 'button';
    linkBtn.textContent = 'この用事を紐づける';
    linkBtn.addEventListener('click', () => goalAction(
      {action: 'link', id: goal.id, task_id: select.value},
      '用事を紐づけました'));
    row.appendChild(linkBtn);
    tasksBox.appendChild(row);
  }
  card.appendChild(tasksBox);
  // 進捗メモ（新しい順）
  const notesBox = document.createElement('div');
  notesBox.className = 'gnotes';
  for (const note of goal.notes || []) {
    const row = document.createElement('div');
    row.className = 'gnote';
    const nTime = document.createElement('span');
    nTime.className = 'ntime';
    nTime.textContent = note.ts
      ? String(note.ts).replace('T', ' ').slice(0, 16) : '';
    row.appendChild(nTime);
    const nBody = document.createElement('span');
    nBody.className = 'nbody';
    nBody.textContent = note.body || '';
    row.appendChild(nBody);
    notesBox.appendChild(row);
  }
  const noteRow = document.createElement('div');
  noteRow.className = 'gnote';
  const noteInput = document.createElement('input');
  noteInput.placeholder = '進捗メモ';
  noteInput.size = 28;
  noteRow.appendChild(noteInput);
  const noteBtn = document.createElement('button');
  noteBtn.type = 'button';
  noteBtn.textContent = 'メモを残す';
  noteBtn.addEventListener('click', () => {
    if (!noteInput.value.trim()) return;
    goalAction({action: 'note', id: goal.id, body: noteInput.value},
               'メモを残しました');
  });
  noteRow.appendChild(noteBtn);
  notesBox.appendChild(noteRow);
  card.appendChild(notesBox);
  // 編集・削除
  const tools = document.createElement('div');
  tools.className = 'gtools';
  const editBox = document.createElement('div');
  editBox.className = 'gedit';
  editBox.hidden = true;
  const titleInput = document.createElement('input');
  titleInput.value = goal.title || '';
  titleInput.size = 20;
  editBox.appendChild(titleInput);
  const descInput = document.createElement('input');
  descInput.value = goal.description || '';
  descInput.placeholder = 'ひとこと説明（任意）';
  descInput.size = 20;
  editBox.appendChild(descInput);
  const statusSelect = document.createElement('select');
  for (const [value, label] of [['active', '進行中'], ['done', '達成']]) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = label;
    if (goal.status === value) opt.selected = true;
    statusSelect.appendChild(opt);
  }
  editBox.appendChild(statusSelect);
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  saveBtn.textContent = '保存する';
  saveBtn.addEventListener('click', () => goalAction(
    {action: 'update', id: goal.id, title: titleInput.value,
     description: descInput.value, status: statusSelect.value},
    '目標を更新しました'));
  editBox.appendChild(saveBtn);
  const editBtn = document.createElement('button');
  editBtn.type = 'button';
  editBtn.textContent = '編集';
  editBtn.addEventListener('click', () => { editBox.hidden = !editBox.hidden; });
  tools.appendChild(editBtn);
  const deleteBtn = document.createElement('button');
  deleteBtn.type = 'button';
  deleteBtn.textContent = '削除';
  deleteBtn.addEventListener('click', () => {
    if (!window.confirm('目標「' + (goal.title || '')
        + '」を削除します。紐づけた用事は残ります。よろしいですか？')) {
      return;
    }
    goalAction({action: 'delete', id: goal.id}, '目標を削除しました');
  });
  tools.appendChild(deleteBtn);
  card.appendChild(tools);
  card.appendChild(editBox);
  goalRows.appendChild(card);
}

async function loadGoals() {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/goals' + query);
    const data = await res.json();
    renderGoals(data);
  } catch (e) { /* 一覧が取れなければパネルは空のままにする */ }
}

goalCreateBtn.addEventListener('click', () => {
  if (!goalNewTitle.value.trim()) {
    goalStatus.textContent = 'タイトルを入力してください';
    return;
  }
  goalAction({action: 'create', title: goalNewTitle.value,
              description: goalNewDesc.value}, '目標を追加しました')
    .then((ok) => {
      if (ok) { goalNewTitle.value = ''; goalNewDesc.value = ''; }
    });
});

// 小さな画面（残しておける HTML ページ）。タブを開くたびに
// /api/spaces で目次を取り直し、開くときに本文を
// /api/spaces/content で取って隔離した枠（sandbox の iframe）に
// 入れる。作成・編集・削除は /api/spaces/act で行い、応答の
// 一覧で画面を描き直す
function spaceAction(payload, doneMessage) {
  return fetch('/api/spaces/act', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(Object.assign(
      {tenant: currentTenant, user: currentUser}, payload)),
  }).then(async (res) => {
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '操作に失敗しました');
    renderSpaces(data);
    spaceStatus.textContent = doneMessage || '';
    return true;
  }).catch((e) => {
    spaceStatus.textContent = '操作に失敗しました: ' + (e.message || e);
    return false;
  });
}

function renderSpaces(data) {
  const spaces = data.spaces || [];
  spaceRows.innerHTML = '';
  if (!spaces.length) {
    const row = document.createElement('div');
    row.className = 'spaceRow';
    row.textContent = '画面はまだありません';
    spaceRows.appendChild(row);
    return;
  }
  for (const space of spaces) {
    const card = document.createElement('div');
    card.className = 'spaceRow';
    const head = document.createElement('div');
    head.className = 'shead';
    const title = document.createElement('span');
    title.className = 'stitle';
    title.textContent = space.title || '';
    head.appendChild(title);
    const time = document.createElement('span');
    time.className = 'stime';
    time.textContent = space.updated_at
      ? '更新: ' + String(space.updated_at).replace('T', ' ').slice(0, 16)
      : '';
    head.appendChild(time);
    const openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.textContent = '開く';
    openBtn.addEventListener('click', () => openSpace(space));
    head.appendChild(openBtn);
    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.textContent = '編集';
    editBtn.addEventListener('click', () => editSpace(space));
    head.appendChild(editBtn);
    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.textContent = '削除';
    delBtn.addEventListener('click', () => {
      if (!confirm('画面「' + (space.title || '') + '」を削除しますか？')) {
        return;
      }
      spaceAction({action: 'delete', id: space.id}, '画面を削除しました')
        .then((ok) => {
          if (ok && spaceView.dataset.spaceId === space.id) {
            spaceView.hidden = true;
            spaceFrame.srcdoc = '';
            spaceView.dataset.spaceId = '';
          }
        });
    });
    head.appendChild(delBtn);
    card.appendChild(head);
    spaceRows.appendChild(card);
  }
}

async function openSpace(space) {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser)
      + '&id=' + encodeURIComponent(space.id);
    const res = await fetch('/api/spaces/content' + query);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '開けませんでした');
    spaceViewTitle.textContent = data.title || '';
    spaceFrame.srcdoc = data.html || '';
    spaceView.dataset.spaceId = space.id;
    spaceView.hidden = false;
    spaceStatus.textContent = '';
  } catch (e) {
    spaceStatus.textContent = '操作に失敗しました: ' + (e.message || e);
  }
}

async function editSpace(space) {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser)
      + '&id=' + encodeURIComponent(space.id);
    const res = await fetch('/api/spaces/content' + query);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '開けませんでした');
    spaceEditingId = space.id;
    spaceNewTitle.value = data.title || '';
    spaceNewHtml.value = data.html || '';
    spaceEditHead.textContent = '画面を編集';
    spaceEditCancelBtn.hidden = false;
    spaceStatus.textContent = '';
  } catch (e) {
    spaceStatus.textContent = '操作に失敗しました: ' + (e.message || e);
  }
}

function resetSpaceEditor() {
  spaceEditingId = null;
  spaceNewTitle.value = '';
  spaceNewHtml.value = '';
  spaceEditHead.textContent = '画面を作る';
  spaceEditCancelBtn.hidden = true;
}

spaceSaveBtn.addEventListener('click', () => {
  if (!spaceNewTitle.value.trim()) {
    spaceStatus.textContent = 'タイトルを入力してください';
    return;
  }
  if (!spaceNewHtml.value.trim()) {
    spaceStatus.textContent = 'HTML を入力してください';
    return;
  }
  const payload = spaceEditingId
    ? {action: 'update', id: spaceEditingId,
       title: spaceNewTitle.value, html: spaceNewHtml.value}
    : {action: 'create',
       title: spaceNewTitle.value, html: spaceNewHtml.value};
  spaceAction(payload, spaceEditingId ? '画面を更新しました'
                                      : '画面を作りました')
    .then((ok) => { if (ok) resetSpaceEditor(); });
});

spaceEditCancelBtn.addEventListener('click', resetSpaceEditor);

async function loadSpaces() {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/spaces' + query);
    const data = await res.json();
    renderSpaces(data);
  } catch (e) { /* 一覧が取れなければパネルは空のままにする */ }
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
      setIcon(link, 'download', 'ダウンロード');
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
    setIcon(delBtn, 'trash', '削除');
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
  if (settings.approvals_locked) {
    // 運用者ロック中は承認を切れない（値は実効値でオン固定）。
    // 切り替えを無効化し、理由を注記する
    approvalToggle.checked = true;
    approvalToggle.disabled = true;
    const lockNote = document.createElement('span');
    lockNote.textContent = '（運用者の設定で固定されています）';
    approvalLabel.appendChild(lockNote);
  }
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
  const brainLabel = document.createElement('label');
  brainLabel.appendChild(document.createTextNode('頭脳: '));
  const brainSelect = document.createElement('select');
  const brainDefaultOpt = document.createElement('option');
  brainDefaultOpt.value = '';
  brainDefaultOpt.textContent = '全体の既定';
  brainSelect.appendChild(brainDefaultOpt);
  for (const p of (settings.brain_providers || [])) {
    const opt = document.createElement('option');
    opt.value = p;
    opt.textContent = p;
    brainSelect.appendChild(opt);
  }
  brainSelect.value = settings.brain_provider || '';
  brainLabel.appendChild(brainSelect);
  const brainModel = document.createElement('input');
  brainModel.type = 'text';
  brainModel.size = 18;
  if (settings.brain_model) brainModel.value = settings.brain_model;
  brainModel.placeholder = settings.brain_effective_model
    ? '既定 ' + settings.brain_effective_model : 'モデル名';
  brainLabel.appendChild(brainModel);
  row.appendChild(brainLabel);
  const brainResetBtn = document.createElement('button');
  brainResetBtn.type = 'button';
  brainResetBtn.textContent = '頭脳を既定に戻す';
  brainResetBtn.title = 'このテナントの頭脳の指定を取り除き、全体の既定に戻す';
  brainResetBtn.addEventListener('click', async () => {
    try {
      const res = await fetch('/api/tenant_settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tenant: name, brain: null}),
      });
      if (!res.ok) throw new Error('reset failed');
      window.alert('「' + name + '」の頭脳を全体の既定に戻しました');
      loadTenantAdmin();
      loadMeta();
    } catch (e) {
      window.alert('戻すのに失敗しました');
    }
  });
  row.appendChild(brainResetBtn);
  const brainKeyLabel = document.createElement('label');
  brainKeyLabel.appendChild(document.createTextNode('API キー: '));
  const brainKey = document.createElement('input');
  brainKey.type = 'password';
  brainKey.size = 18;
  brainKey.autocomplete = 'off';
  // キーの値そのものは画面へ出さない。欄は常に空で、入力した
  // ときだけ保存される（空のまま保存しても変更されない）
  brainKey.placeholder = settings.brain_key_set
    ? '設定済み（変更する場合だけ入力）' : '全体のキーを使う';
  brainKeyLabel.appendChild(brainKey);
  const brainKeyState = document.createElement('span');
  if (settings.brain_key_invalid) {
    brainKeyState.textContent = '（キーの保存が壊れています）';
  } else if (settings.brain_key_set) {
    brainKeyState.textContent = '（設定済み'
      + (settings.brain_key_provider
         ? ': ' + settings.brain_key_provider + ' 用' : '') + '）';
  } else {
    brainKeyState.textContent = '（未設定）';
  }
  brainKeyLabel.appendChild(brainKeyState);
  row.appendChild(brainKeyLabel);
  if (settings.brain_key_set || settings.brain_key_invalid) {
    const brainKeyDeleteBtn = document.createElement('button');
    brainKeyDeleteBtn.type = 'button';
    brainKeyDeleteBtn.textContent = 'キーを消す';
    brainKeyDeleteBtn.title = 'このテナントの API キーを消し、全体のキーを使うように戻す';
    brainKeyDeleteBtn.addEventListener('click', async () => {
      try {
        const res = await fetch('/api/tenant_settings', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({tenant: name, brain_key_delete: true}),
        });
        if (!res.ok) throw new Error('delete failed');
        window.alert('「' + name + '」の API キーを消しました');
        loadTenantAdmin();
      } catch (e) {
        window.alert('キーを消すのに失敗しました');
      }
    });
    row.appendChild(brainKeyDeleteBtn);
  }
  const saveBtn = document.createElement('button');
  saveBtn.type = 'button';
  setIcon(saveBtn, 'check', '保存');
  saveBtn.addEventListener('click', async () => {
    const body = {tenant: name, proactive_enabled: toggle.checked,
                  approvals_enabled: approvalToggle.checked,
                  retention_days: days.value === '' ? null : Number(days.value),
                  brain: {provider: brainSelect.value || null,
                          model: brainModel.value.trim() || null},
                  brain_api_key: brainKey.value};
    try {
      const res = await fetch('/api/tenant_settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error('save failed');
      window.alert('「' + name + '」の設定を保存しました');
      loadTenantAdmin();
      loadMeta();
    } catch (e) {
      window.alert('保存に失敗しました');
    }
  });
  row.appendChild(saveBtn);
  const switchBtn = document.createElement('button');
  switchBtn.type = 'button';
  setIcon(switchBtn, 'swap', '切り替え');
  switchBtn.addEventListener('click', () => applyTenant(name));
  row.appendChild(switchBtn);
  // 削除は取り消せないため既定テナントには出さない。確認は
  // showTenantDeleteConfirm が担い、消えるデータの内訳を表示した
  // うえでテナント名の入力を求める（一致しない場合は進めない）。
  // 実際の削除は /api/tenants/delete が担い、サーバー側で控えを
  // 取ってから置き場を消す
  if (name !== 'default') {
    const deleteBtn = document.createElement('button');
    deleteBtn.type = 'button';
    setIcon(deleteBtn, 'trash', '削除');
    deleteBtn.addEventListener('click', () => showTenantDeleteConfirm(name));
    row.appendChild(deleteBtn);
  }
  return row;
}

// 削除の確認欄。押されたテナントについて、消えるデータの内訳
// （ユーザー・会話・用事の一覧・能動発信と保持期限の設定）を
// /api/tenants/impact で取り、パネル内に一覧表示する。確認として
// テナント名の入力も求め、一致したときだけ「削除する」で
// /api/tenants/delete を呼ぶ。キャンセルでは何も消えない
const tenantDeleteConfirm = document.getElementById('tenantDeleteConfirm');
let tenantDeleteTarget = null;

function hideTenantDeleteConfirm() {
  tenantDeleteTarget = null;
  tenantDeleteConfirm.hidden = true;
  tenantDeleteConfirm.replaceChildren();
}

async function showTenantDeleteConfirm(name) {
  tenantDeleteTarget = name;
  tenantDeleteConfirm.hidden = false;
  tenantDeleteConfirm.replaceChildren();
  const loading = document.createElement('p');
  loading.textContent = 'テナント「' + name + '」の内容を確認しています…';
  tenantDeleteConfirm.appendChild(loading);
  let impact = null;
  try {
    const res = await fetch(
      '/api/tenants/impact?tenant=' + encodeURIComponent(name));
    impact = await res.json();
    if (!res.ok) throw new Error(impact.error || '内容を確認できませんでした');
  } catch (e) {
    if (tenantDeleteTarget !== name) return;
    tenantDeleteConfirm.replaceChildren();
    const err = document.createElement('p');
    err.textContent = '内容を確認できなかったため、削除は行いません: '
      + (e.message || e);
    tenantDeleteConfirm.appendChild(err);
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    setIcon(closeBtn, 'x', '閉じる');
    closeBtn.addEventListener('click', hideTenantDeleteConfirm);
    tenantDeleteConfirm.appendChild(closeBtn);
    return;
  }
  if (tenantDeleteTarget !== name) return;
  tenantDeleteConfirm.replaceChildren();
  const title = document.createElement('h3');
  title.textContent = 'テナント「' + name + '」を削除しますか？';
  tenantDeleteConfirm.appendChild(title);
  const intro = document.createElement('p');
  intro.textContent = 'このテナントには次のデータがあります。'
    + '削除すると、すべて消えます。';
  tenantDeleteConfirm.appendChild(intro);
  const list = document.createElement('ul');
  const userNames = (impact.users || []).map(u => u.user).join('、');
  const items = [
    'ユーザー: ' + userNames + '（' + impact.user_count + '人）',
    '会話: ' + impact.conversations + '件',
    '用事の一覧: ' + impact.tasks + '件（うち未完了 '
      + impact.tasks_open + '件）',
    '能動発信: ' + (impact.proactive_enabled ? 'オン' : 'オフ'),
    'データの保持期限: ' + impact.retention_effective_days + '日',
    'データの大きさ: 約 '
      + Math.max(1, Math.round(impact.size_bytes / 1024)) + ' KB',
  ];
  for (const text of items) {
    const li = document.createElement('li');
    li.textContent = text;
    list.appendChild(li);
  }
  tenantDeleteConfirm.appendChild(list);
  const warn = document.createElement('p');
  warn.className = 'warn';
  warn.textContent = '削除は取り消せません。実行の直前に控え（ZIP）が'
    + '自動で作られますが、画面から元に戻すことはできません。';
  tenantDeleteConfirm.appendChild(warn);
  const label = document.createElement('label');
  label.textContent = '確認のため、テナント名を入力してください: ';
  const input = document.createElement('input');
  input.type = 'text';
  input.size = 14;
  label.appendChild(input);
  tenantDeleteConfirm.appendChild(label);
  const buttons = document.createElement('div');
  buttons.className = 'buttons';
  const doBtn = document.createElement('button');
  doBtn.type = 'button';
  doBtn.className = 'deleteDo';
  setIcon(doBtn, 'trash', '削除する');
  doBtn.addEventListener('click', async () => {
    if (input.value.trim() !== name) {
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
    hideTenantDeleteConfirm();
    window.alert('テナント「' + name + '」を削除しました'
      + (data.backup ? '（控え: ' + data.backup + '）' : ''));
    const wasCurrent = currentTenant === name;
    await loadTenants();
    await loadTenantAdmin();
    if (wasCurrent) applyTenant('default');
  });
  buttons.appendChild(doBtn);
  const cancelBtn = document.createElement('button');
  cancelBtn.type = 'button';
  setIcon(cancelBtn, 'x', 'キャンセル');
  cancelBtn.addEventListener('click', hideTenantDeleteConfirm);
  buttons.appendChild(cancelBtn);
  tenantDeleteConfirm.appendChild(buttons);
  input.focus();
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
  recordBtn.innerHTML = on ? ICONS.stop : ICONS.mic;
  recordBtn.title = on ? '録音を止める' : '録音';
  recordBtn.classList.toggle('recording', on);
  recordStatus.hidden = !on;
  recordStatus.textContent = on ? '録音中…' : '';
}

function showRecordError(message) {
  mediaRecorder = null;
  recordBtn.innerHTML = ICONS.mic;
  recordBtn.title = '録音';
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
  pollChoices();
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
  lastApprovals = list;
  approvalRows.innerHTML = '';
  approvalPanel.hidden = !(list.length && activeTab === 'chat');
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
  setIcon(btn, approved ? 'check' : 'x', label);
  if (!approved) btn.classList.add('deny');
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

// 質問（選択肢での聞き返し）: 進め方の選択を尋ねる質問があれば、
// 質問文と選択肢のボタンをカードで出す。承認（安全の可否）とは
// 別の仕組みで、こちらは /api/choices と /api/choices/answer で
// 扱う。ポーリングは承認と同じく状態ポーリングに相乗りする
async function pollChoices() {
  try {
    const query = '?tenant=' + encodeURIComponent(currentTenant)
      + '&user=' + encodeURIComponent(currentUser);
    const res = await fetch('/api/choices' + query);
    const data = await res.json();
    renderChoices(data.choices || []);
  } catch (e) { /* 取れなければ前回の表示のままにする */ }
}

function renderChoices(list) {
  lastChoices = list;
  choiceRows.innerHTML = '';
  choicePanel.hidden = !(list.length && activeTab === 'chat');
  for (const item of list) {
    const card = document.createElement('div');
    card.className = 'choiceCard';
    const q = document.createElement('div');
    q.className = 'cquestion';
    q.textContent = item.question;
    card.appendChild(q);
    const options = document.createElement('div');
    options.className = 'choiceOptions';
    for (const opt of item.options || []) {
      options.appendChild(choiceButton(item, opt));
    }
    card.appendChild(options);
    if (item.allow_text) {
      card.appendChild(choiceTextRow(item));
    }
    choiceRows.appendChild(card);
  }
}

function choiceButton(item, opt) {
  const btn = document.createElement('button');
  btn.type = 'button';
  const label = document.createElement('span');
  label.textContent = opt.label;
  btn.appendChild(label);
  if (opt.note) {
    const note = document.createElement('span');
    note.className = 'cnote';
    note.textContent = opt.note;
    btn.appendChild(note);
  }
  btn.addEventListener('click', () => answerChoice(item, opt.label, btn));
  return btn;
}

function choiceTextRow(item) {
  const row = document.createElement('div');
  row.className = 'choiceText';
  const textInput = document.createElement('input');
  textInput.type = 'text';
  textInput.placeholder = '文章で答える';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = '文章で答える';
  btn.addEventListener('click', () => {
    const value = textInput.value.trim();
    if (value) answerChoice(item, value, btn);
  });
  textInput.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      btn.click();
    }
  });
  row.appendChild(textInput);
  row.appendChild(btn);
  return row;
}

async function answerChoice(item, answer, btn) {
  if (btn) btn.disabled = true;
  try {
    const res = await fetch('/api/choices/answer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: item.id, answer,
                            tenant: currentTenant, user: currentUser}),
    });
    if (!res.ok) throw new Error('answer failed');
  } catch (e) {
    window.alert('回答の送信に失敗しました');
    if (btn) btn.disabled = false;
  }
  pollChoices();
}

form.addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  const files = pendingFiles;
  const quote = pendingQuote;
  const userDiv = addMsg('user', describe(text, files.map((f) => f.name)),
                         quote, text);
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
    const replyDiv = addMsg('assistant',
      data.reply || ('エラー: ' + (data.error || res.status)));
    if (data.user_rid !== undefined && data.user_rid !== null) {
      attachReactions(userDiv, data.user_rid, []);
    }
    if (data.rid !== undefined && data.rid !== null) {
      attachReactions(replyDiv, data.rid, []);
    }
  } catch (e) {
    addMsg('assistant', '通信に失敗しました');
  } finally {
    clearInterval(poller);
    showStatus('');
    pollApprovals();
    pollChoices();
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

// CLI 系の頭脳のときの注意書き（サーバー側の _CLI_PROVIDER_NOTE_HTML
// と同じ文面）。頭脳はテナントごとに変わるため、初期表示はサーバー
// が埋め込み、切り替え後は /api/meta の実効頭脳で描き直す
const CLI_PROVIDER_NOTE_TEXT = 'この頭脳では、道具の実行は外部の'
  + '道具の側で行われます。クマ側の承認確認や道具の制限は効きま'
  + 'せん（委任の型による道具の上限も効きません）';

function renderBrain(data) {
  const providerEl = document.getElementById('providerName');
  const slot = document.getElementById('cliNoteSlot');
  if (!providerEl || !slot || !data || !data.brain_provider) return;
  providerEl.textContent = 'provider: ' + data.brain_provider
    + (data.brain_model ? ' / ' + data.brain_model : '');
  slot.replaceChildren();
  if (data.brain_cli) {
    const note = document.createElement('div');
    note.className = 'provider';
    note.textContent = CLI_PROVIDER_NOTE_TEXT;
    slot.appendChild(note);
  }
}

async function loadMeta() {
  try {
    const res = await fetch('/api/meta?tenant='
      + encodeURIComponent(currentTenant) + '&user='
      + encodeURIComponent(currentUser));
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
    renderBrain(data);
  } catch (e) { /* 取れなければ従来どおり全部出す */ }
}

(async () => {
  await loadMeta();
  await loadTenants();
  await loadUsers();
  // テナント／ユーザーが確定したところで、頭脳表示をその
  // 組み合わせの実効頭脳に合わせ直す
  await loadMeta();
  await loadSessions();
  refreshAvatarFace();
  loadHistory();
})();

// アバター（クマの顔）。ヘッダーの顔は /api/avatar/image から
// 読むので、テナント×ユーザーが変わったら読み直す。キャッシュ
// を避けるため、URL に時刻を混ぜる
function avatarImageUrl(extra) {
  return '/api/avatar/image?tenant=' + encodeURIComponent(currentTenant)
    + '&user=' + encodeURIComponent(currentUser)
    + (extra || '') + '&v=' + Date.now();
}

function refreshAvatarFace() {
  const img = document.getElementById('avatarImg');
  if (img) img.src = avatarImageUrl('');
}
const themeBtn = document.getElementById("themeBtn");
const THEME_ORDER = ["light", "dark", "system", "pixel"];
const THEME_NAMES = { light: "昼", dark: "夜", system: "システム",
                      pixel: "8ビット" };
const THEME_ICONS = {
  light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12"'
    + ' r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4'
    + ' 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  dark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/></svg>',
  system: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="2"><circle cx="12" cy="12" r="9"/>'
    + '<path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor"'
    + ' stroke="none"/></svg>',
  pixel: '<svg viewBox="0 0 24 24" fill="currentColor">'
    + '<path d="M5 4h2v2H5zM17 4h2v2h-2zM7 6h2v2H7zM15 6h2v2h-2z'
    + 'M5 8h14v2H5zM3 10h4v2H3zM9 10h6v2H9zM17 10h4v2h-4z'
    + 'M1 12h22v2H1zM1 14h2v2H1zM5 14h14v2H5zM21 14h2v2h-2z'
    + 'M1 16h2v2H1zM5 16h2v2H5zM17 16h2v2h-2zM21 16h2v2h-2z'
    + 'M7 18h4v2H7zM13 18h4v2h-4z"/></svg>',
};

function paintThemeBtn() {
  const t = document.documentElement.dataset.theme || "system";
  themeBtn.innerHTML = THEME_ICONS[t];
  themeBtn.title = "テーマ: " + THEME_NAMES[t] + "（押すと切り替え）";
  themeBtn.setAttribute("aria-label",
    "テーマ: " + THEME_NAMES[t] + "（押すと切り替え）");
}

themeBtn.addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme || "system";
  const next = THEME_ORDER[
    (THEME_ORDER.indexOf(cur) + 1) % THEME_ORDER.length];
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("kuma-theme", next);
  } catch (e) { /* 保存できなくても切り替え自体は効かせる */ }
  paintThemeBtn();

});
paintThemeBtn();

// アイコンボタン用の絵柄。文字のボタンは置かず、意味は title と
// aria-label で伝える
const ICONS = {
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12.5l5 5L20 6.5"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>',
  swap: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 2l4 4-4 4M3 11v-1a4 4 0 0 1 4-4h14M7 22l-4-4 4-4M21 13v1a4 4 0 0 1-4 4H3"/></svg>',
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v12M6 10l6 6 6-6M4 20h16"/></svg>',
  quote: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6.5 5C5 6.5 4 8.4 4 11.2V18h6.2v-6.2H6.9c.1-2 .9-3.6 2.4-4.9L6.5 5zm10 0C15 6.5 14 8.4 14 11.2V18h6.2v-6.2h-3.3c.1-2 .9-3.6 2.4-4.9L16.5 5z"/></svg>',
  speaker: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5L6 9H3v6h3l5 4zM15.5 8.5a5 5 0 0 1 0 7M18.6 5.4a9 9 0 0 1 0 13.2"/></svg>',
  stop: '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>',
  mic: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0M12 19v3"/></svg>',
  smile: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8.5 14.5a5 5 0 0 0 7 0M9 9.5h.01M15 9.5h.01"/></svg>',
};

function setIcon(btn, name, label) {
  btn.innerHTML = ICONS[name];
  btn.classList.add('iconBtn', 'sm');
  btn.title = label;
  btn.setAttribute('aria-label', label);
}
input.focus();
</script>
</body>
</html>
"""


# ヘッダーのアバターの差し込み（画像の配信経路を指す <img>）。
# アバター管理で差し替えられるよう、顔は /api/avatar/image から
# 読む。avatar_svg を明示された場合（テストや代替表示）は従来
# どおり SVG を直接埋め込む
_AVATAR_IMG_HTML = ('<img id="avatarImg" class="avatarFace" '
                    'src="/api/avatar/image" alt="">')


def render_page(provider: str, avatar_svg: str | None = None,
                web_auth: bool = False) -> str:
    """チャット画面の HTML を組み立てる。

    web_auth が True（ログイン有効）のときだけ、ヘッダーに
    ログアウトボタンを出す。無効時は従来の見た目のままにする。
    """
    return (
        _PAGE_TEMPLATE
        .replace("__AVATAR__", avatar_svg if avatar_svg is not None
                 else _AVATAR_IMG_HTML)
        .replace("__PROVIDER__", provider)
        .replace("__CLI_PROVIDER_NOTE__",
                 _CLI_PROVIDER_NOTE_HTML if provider in CLI_PROVIDERS
                 else "")
        .replace("__LOGOUT_BUTTON__",
                 _LOGOUT_BUTTON_HTML if web_auth else "")
        .replace("__HONEY__", HONEY)
        .replace("__COCOA__", COCOA)
        .replace("__CREAM__", CREAM)
        .replace("__LINE__", LINE)
    )


# CLI 系の頭脳のときにヘッダーの頭脳表示の下へ差し込む注意書き。
# CLI 系では道具の実行が外部 CLI の側で行われ、kuma-agent 側の
# 承認確認や道具の制限が効かないことを、画面でも誤解のないよう
# 伝える（公開前チェックの指摘1 (a) 明示）。API 系では出さない
_CLI_PROVIDER_NOTE_HTML = (
    '\n    <div class="provider">この頭脳では、道具の実行は外部の'
    '道具の側で行われます。クマ側の承認確認や道具の制限は効きま'
    'せん（委任の型による道具の上限も効きません）</div>')


# ログイン有効時にヘッダーへ差し込むログアウトボタン。押すと
# POST /api/logout でトークンを破棄してからログイン画面へ移る
_LOGOUT_BUTTON_HTML = """\
<button id="logoutBtn" type="button" class="iconBtn sm" title="ログアウト" aria-label="ログアウト"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg></button>
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
<script>
  try {
    document.documentElement.dataset.theme =
      localStorage.getItem("kuma-theme") || "system";
  } catch (e) {
    document.documentElement.dataset.theme = "system";
  }
</script>
<style>
  * { box-sizing: border-box; }
  :root { --bg: __CREAM__; --surface: #FFFFFF; --ink: __COCOA__;
          --line: __LINE__; }
  :root[data-theme="dark"] { --bg: #201D1A; --surface: #2B2723;
          --ink: #EFE9E0; --line: #453E34; }
  @media (prefers-color-scheme: dark) {
    :root[data-theme="system"] { --bg: #201D1A; --surface: #2B2723;
          --ink: #EFE9E0; --line: #453E34; } }
  :root[data-theme="pixel"] { --bg: #030308; --surface: #0B0B18;
          --ink: #EFFFF2; --line: #00FF66; }
  :root[data-theme="pixel"] .card { border-width: 2px; border-radius: 0;
          box-shadow: 4px 4px 0 rgba(0, 255, 102, .5); }
  :root[data-theme="pixel"] .card input { border-width: 2px;
          border-radius: 0; }
  :root[data-theme="pixel"] .card button { background: #00FF66;
          color: #030308; border-radius: 0; }
  input { background: var(--surface); color: var(--ink); }
  body { margin: 0; font-family: sans-serif; background: var(--bg); color: var(--ink);
         display: flex; align-items: center; justify-content: center; min-height: 100dvh; }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: 6px;
          padding: 24px; width: min(360px, 90vw); text-align: center;
          box-shadow: 0 4px 18px rgba(58, 51, 44, .06); }
  .card .avatar { width: 64px; height: 64px; margin: 0 auto 8px; }
  .card .avatar svg { width: 100%; height: 100%; display: block; }
  .card h1 { font-size: 20px; margin: 0 0 4px; }
  .card p { font-size: 14px; margin: 0 0 16px; }
  .card .error { color: #B3402A; min-height: 1.2em; }
  .card form { display: flex; flex-direction: column; gap: 10px; }
  .card input { padding: 10px; font-size: 16px; border: 1px solid var(--line);
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
      <button type="submit" class="iconBtn go" title="ログイン" aria-label="ログイン"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4M10 17l5-5-5-5M15 12H3"/></svg></button>
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
        .replace("__LINE__", LINE)
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


class ChoiceBroker:
    """保留中の質問（選択肢での聞き返し）を保持し、回答を待つ。

    承認（ApprovalBroker）とは役割が違う: 承認は安全のための
    可否確認で、こちらは進め方の選択を利用者に尋ねるもの
    （AF-4）。会話リクエストのスレッドが request() で回答待ちに
    入り、画面側が GET /api/choices で保留一覧を見て、
    POST /api/choices/answer で answer() を呼ぶまで待つ。待ちに
    は上限（timeout 秒）があり、時間切れは「答えなし」（None）
    として返す（会話は止め続けず、モデルが文章での質問に切り
    替える）。保留はテナント×ユーザー単位で分け、一覧と回答は
    その組み合わせの中だけで効く。答えは、選択肢のラベルの
    どれか、または文章での回答を許した質問への自由な文章だけ
    を受け付ける。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}

    def request(self, tenant: str, user: str, question: str,
                options: list, allow_text: bool,
                timeout: float) -> str | None:
        """質問を 1 件登録し、回答（または時間切れ）まで待つ。"""
        entry = {
            "id": uuid.uuid4().hex,
            "tenant": tenant,
            "user": user,
            "question": question,
            "options": options,
            "allow_text": bool(allow_text),
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": threading.Event(),
            "answer": None,
        }
        with self._lock:
            self._pending[entry["id"]] = entry
        try:
            entry["event"].wait(timeout if timeout and timeout > 0 else None)
            return entry["answer"]
        finally:
            with self._lock:
                self._pending.pop(entry["id"], None)

    def list_pending(self, tenant: str, user: str) -> list[dict]:
        """そのテナント×ユーザーの保留一覧（画面に出す項目だけ）。"""
        with self._lock:
            entries = [e for e in self._pending.values()
                       if e["tenant"] == tenant and e["user"] == user]
        entries.sort(key=lambda e: e["time"])
        return [{"id": e["id"], "question": e["question"],
                 "options": e["options"], "allow_text": e["allow_text"],
                 "time": e["time"]} for e in entries]

    def answer(self, tenant: str, user: str, choice_id: str,
               answer: str) -> bool:
        """保留中の質問に回答する。対象が無ければ False。

        答えとして受け付けるのは、選択肢のラベルのどれかと、
        文章での回答を許した質問（allow_text）への空でない文章
        だけ。それ以外は False（対象なしと同じ扱い）。
        """
        answer = str(answer or "").strip()
        if not answer:
            return False
        with self._lock:
            entry = self._pending.get(choice_id)
            if (entry is None or entry["tenant"] != tenant
                    or entry["user"] != user):
                return False
            labels = [opt["label"] for opt in entry["options"]]
            if answer not in labels and not entry["allow_text"]:
                return False
            entry["answer"] = answer
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
                 approvals: ApprovalBroker | None = None,
                 choices: ChoiceBroker | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._factory = factory or (
            lambda name: Session(initial.cfg, initial.model, name))
        self.tenant = initial.tenant
        self.user = initial.user
        self._data_dir = initial.cfg.user_data_dir
        self._approvals = approvals
        self._choices = choices
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
        if self._choices is not None:
            # 応答者も差し込む: 選択肢での質問（ask_choice）の保留
            # としてブローカーに登録し、画面の回答を待つ。待ちの
            # 上限は承認と同じ設定（approval_timeout_seconds）を
            # 使う。差し込むと Session 側で ask_choice 道具が
            # 登録される
            choice_broker = self._choices

            def responder(question: str, options: list, allow_text: bool,
                          _s: Session = session) -> str | None:
                return choice_broker.request(
                    _s.tenant, _s.user, question, options, allow_text,
                    _s.cfg.approval_timeout_seconds)

            session.responder = responder

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

    def discard_all(self) -> None:
        """開いている Session をすべて破棄する（データ復元後に使う）。

        置き場の中身が置き換わった後も抱えたままにすると、古い
        履歴のまま会話が続いてしまうため、復元の完了に合わせて
        すべて落とす。次に開いたときに記録から作り直される。
        """
        with self._lock:
            self._sessions.clear()
            self._statuses.clear()

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
                 approvals: ApprovalBroker | None = None,
                 choices: ChoiceBroker | None = None) -> None:
        self.tenant = tenant
        self.default_user = default_user
        self._tenant_data_dir = tenant_data_dir
        self._make_session = make_session
        self._approvals = approvals
        self._choices = choices
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
                # 破棄後の作り直しも make_session を通す（factory を
                # 渡さないと、最初の Session の設定・モデルを共有する
                # 既定の作り方になり、頭脳の切り替えが反映されない）
                store = SessionStore(
                    initial,
                    factory=lambda session_id, _user=name:
                        self._make_session(_user, session_id),
                    approvals=self._approvals,
                    choices=self._choices)
                self._stores[name] = store
            return store

    def discard_sessions(self, user: str | None) -> None:
        """そのユーザーの開いている Session をすべて破棄する。

        ストアがまだ作られていなければ何もしない（作り直しは
        しない）。
        """
        name = normalize_user_name(user)
        with self._lock:
            store = self._stores.get(name)
        if store is not None:
            store.discard_all()

    def discard_all_sessions(self) -> None:
        """このテナントの全ユーザーの Session をすべて破棄する。

        ストアがまだ作られていないユーザーは対象外（作り直しは
        しない）。頭脳設定の保存時に TenantStore から使う。
        """
        with self._lock:
            stores = list(self._stores.values())
        for store in stores:
            store.discard_all()

    def names(self) -> list[str]:
        """開いているユーザーと、保存済みユーザーの一覧。"""
        with self._lock:
            loaded = list(self._stores)
        return sorted(set(loaded) | set(list_user_names(self._tenant_data_dir)))


class TenantStore:
    """テナント → UserStore の対応を管理する（Web UI のテナント切替用）。

    サーバーはテナントごとに UserStore を持ち、その中ではユーザー
    ごとに SessionStore を持つ。初回アクセス時にその組み合わせの
    Session を作る。新しい組み合わせの Session は、起動時の設定を
    引き継ぎつつ、そのテナントの頭脳の指定（settings.json の
    brain 項目）があれば brain.resolve_brain で解決した Config と
    モデル実体の組で作るため、データの置き場も頭脳も組み合わせ
    ごとに分かれる。session_factory は起動時テナント・起動時
    ユーザーの部屋の作り方だけを差し替える（テスト用）。
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
        # 選択肢での質問の保留も、承認と同じくサーバー全体で
        # 1 つのブローカーが持つ（役割は承認とは分ける）
        self.choices = ChoiceBroker()
        # テナントごとの頭脳（scoped Config とモデル実体の組）の
        # キャッシュ。生成に失敗したテナントはキャッシュしない
        # （設定を直せば次のアクセスで作り直せる）
        self._brain_cache: dict[str, tuple[Config, object]] = {}
        self._brain_lock = threading.Lock()
        if has_tenant_brain_override(self._cfg.data_dir,
                                      self.default_tenant,
                                      self._cfg.provider,
                                      self._cfg.allow_tenant_brain_cli):
            # 起動時の組み合わせも同じ解決を通す（起動時だけ特別
            # 扱いしない）。渡された Session は全体の既定で作られ
            # ているため、起動時テナントに頭脳の上書き（指定または
            # テナントのキー）があれば、解決した組で作り直してから
            # 抱える。生成に失敗したら
            # 全体の既定のまま抱える（そのテナントの新しい部屋は
            # 次のアクセス時に解決をやり直す）
            try:
                brain_cfg, brain_model = self._brain_for(
                    self.default_tenant)
            except Exception:
                pass
            else:
                initial = Session(
                    replace(brain_cfg, tenant=self.default_tenant,
                            user=self.default_user),
                    brain_model, initial.session_id)
        seeded = UserStore(
            self.default_tenant, initial.cfg.tenant_data_dir,
            self.default_user, self._session_maker(self.default_tenant),
            initial=SessionStore(
                initial,
                session_factory or (lambda name: self._make_session(
                    self.default_tenant, self.default_user, name)),
                approvals=self.approvals,
                choices=self.choices),
            approvals=self.approvals, choices=self.choices)
        self._user_stores: dict[str, UserStore] = {self.default_tenant: seeded}
        self._lock = threading.Lock()

    @property
    def startup_provider(self) -> str:
        """起動時の組み合わせの実効プロバイダ（ページの初期表示用）。"""
        return effective_brain(self._cfg, self.default_tenant)[0]

    def effective_brain_for(self, tenant: str | None) -> tuple[str, str, bool]:
        """テナントの実効頭脳（プロバイダ・モデル名・CLI 系か）。

        クライアントは作らない（/api/meta や設定 API の表示用）。
        """
        return effective_brain(self._cfg, tenant)

    def _brain_for(self, tenant: str) -> tuple[Config, object]:
        """テナントの (scoped Config, モデル実体) を解決して保持する。

        テナントに頭脳の指定が無ければ、全体の既定（起動時の
        Config とモデル実体）の組になる。生成に失敗した場合は
        例外を送出し、キャッシュしない（そのテナントの Session
        生成だけが失敗し、他のテナントと全体は止まらない）。
        """
        name = normalize_tenant_name(tenant)
        with self._brain_lock:
            pair = self._brain_cache.get(name)
            if pair is None:
                pair = resolve_brain(self._cfg, name,
                                     fallback_model=self._model)
                self._brain_cache[name] = pair
            return pair

    def _session_maker(self, tenant: str) -> Callable[[str, str], Session]:
        def make(user: str, session_id: str) -> Session:
            base_cfg, model = self._brain_for(tenant)
            cfg = replace(base_cfg,
                          tenant=normalize_tenant_name(tenant),
                          user=normalize_user_name(user))
            return Session(cfg, model, session_id)
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
                                  approvals=self.approvals,
                                  choices=self.choices)
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

    def discard_user_sessions(self, tenant: str | None,
                              user: str | None) -> None:
        """そのテナント×ユーザーの開いている Session をすべて破棄する。

        データ復元の後に使う。対象のストアがまだ作られていなければ
        何もしない（作り直しはしない）。
        """
        name = normalize_tenant_name(tenant)
        with self._lock:
            user_store = self._user_stores.get(name)
        if user_store is not None:
            user_store.discard_sessions(user)

    def discard_tenant_sessions(self, tenant: str | None) -> None:
        """そのテナントの開いている Session をすべて破棄する。

        頭脳設定の保存時に使う。頭脳のキャッシュも落とし、次に
        アクセスされたときに新しい頭脳で Session が作り直される
        （会話記録はファイルに残るため、文脈は復元される）。対象の
        ストアがまだ作られていなければ、キャッシュだけ落とす。
        実行中の応答は旧頭脳で完走し、次回から新しい頭脳になる。
        """
        name = normalize_tenant_name(tenant)
        with self._brain_lock:
            self._brain_cache.pop(name, None)
        with self._lock:
            user_store = self._user_stores.get(name)
        if user_store is not None:
            user_store.discard_all_sessions()

    def names(self) -> list[str]:
        """開いているテナントと、保存済みテナントの一覧。"""
        with self._lock:
            loaded = list(self._user_stores)
        return sorted(set(loaded) | set(list_tenant_names(self._cfg.data_dir)))

    @property
    def fixed_tenant(self) -> str | None:
        """公開先として固定されたテナント（正規化済み）。

        環境変数 KUMA_WEB_TENANT が設定されている間だけ、その
        テナントを返す。未設定（空）なら None＝テナント軸は
        固定しない（公開前チェックの指摘3・選択肢C）。
        """
        raw = self._cfg.web_tenant
        return normalize_tenant_name(raw) if raw else None

    @property
    def fixed_user(self) -> str | None:
        """公開先として固定されたユーザー（正規化済み）。

        KUMA_WEB_USER が設定されている間だけ、そのユーザーを
        返す。未設定（空）なら None＝ユーザー軸は固定しない。
        """
        raw = self._cfg.web_user
        return normalize_user_name(raw) if raw else None

    def tenant_allowed(self, tenant: str) -> bool:
        """そのテナントが固定の範囲内かどうか。

        テナントが固定されていなければ常に True。固定されて
        いれば、正規化して固定先と一致するときだけ True。
        """
        fixed = self.fixed_tenant
        return fixed is None or normalize_tenant_name(tenant) == fixed

    def pair_allowed(self, tenant: str, user: str) -> bool:
        """その組み合わせが固定の範囲内かどうか。

        照合するのは設定された軸だけ（テナントだけ固定なら
        ユーザーは問わない、という一貫した意味にする）。
        """
        if not self.tenant_allowed(tenant):
            return False
        fixed_user = self.fixed_user
        return fixed_user is None \
            or normalize_user_name(user) == fixed_user

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


def _activity_payload(tenants: TenantStore, tenant: str,
                      user: str, limit: object = None) -> dict:
    """活動の記録 API の応答（GET /api/activity）。

    活動の記録はユーザーごとの置き場（activity.jsonl）にあり、
    対象は tenant / user で指した組み合わせ。記録は新しい順で、
    limit は件数の上限（既定・上限は activity 側でそろえる）。
    """
    records = load_activity_records(
        user_dir(tenant_dir(tenants.data_dir, tenant), user),
        limit=limit)
    return {"tenant": tenant, "user": user, "entries": records}


def _feed_payload(tenants: TenantStore, tenant: str, user: str,
                  limit: object = None, kind: str | None = None) -> dict:
    """フィード API の応答（GET /api/feed）。

    フィードの記録はユーザーごとの置き場（feed.jsonl）にあり、
    対象は tenant / user で指した組み合わせ。エントリは新しい順で、
    limit は件数の上限（既定・上限は feed 側でそろえる）、kind は
    種別の絞り込み（briefing / proactive / task）。
    """
    entries = load_feed_entries(
        user_dir(tenant_dir(tenants.data_dir, tenant), user),
        limit=limit, kind=kind)
    return {"tenant": tenant, "user": user,
            "entries": [entry.to_dict() for entry in entries]}


def _ideas_payload(tenants: TenantStore, tenant: str, user: str) -> dict:
    """アイデア API の応答（GET / POST で共通の形）。

    カードはユーザーごとの置き場（ideas.json）にあり、対象は
    tenant / user で指した組み合わせ。やめておいた（dismissed）
    カードは出さず、未対応（new）と用事にした（accepted）カードを
    新しい順に返す。
    """
    board = IdeaBoard(user_dir(tenant_dir(tenants.data_dir, tenant), user))
    ideas = board.list(statuses=VISIBLE_STATUSES)
    return {"tenant": tenant, "user": user,
            "ideas": [idea.to_dict() for idea in ideas]}


def _goals_payload(tenants: TenantStore, tenant: str, user: str) -> dict:
    """目標 API の応答（GET / POST で共通の形）。

    目標はユーザーごとの置き場（goals.json）にあり、対象は
    tenant / user で指した組み合わせ。各目標には進み具合（紐づいた
    用事の完了割合）と紐づいた用事、進捗メモ（新しい順）を含める。
    あわせて、その利用者の用事すべて（紐づけの選択肢用に id・
    タイトル・状態・goal_id）を tasks として返す。
    """
    target_dir = user_dir(tenant_dir(tenants.data_dir, tenant), user)
    tasks = TaskBoard(target_dir).list()
    goals = GoalBoard(target_dir).list()
    return {"tenant": tenant, "user": user,
            "goals": [goal_view(goal, tasks) for goal in goals],
            "tasks": [{"id": t.id, "title": t.title, "status": t.status,
                       "goal_id": t.goal_id} for t in tasks]}


def _avatar_payload(tenants: TenantStore, tenant: str, user: str) -> dict:
    """アバター API の応答（GET / POST で共通の形）。

    アバターはユーザーごとの置き場（avatars.json + avatars/）に
    あり、対象は tenant / user で指した組み合わせ。current が
    null のときは既定の顔（assets/avatar.svg）を使っている。
    """
    store = AvatarStore(user_dir(tenant_dir(tenants.data_dir, tenant),
                                 user))
    current = store.current_item()
    return {"tenant": tenant, "user": user,
            "current": current.to_dict() if current else None,
            "items": [i.to_dict() for i in store.list()]}


def _spaces_payload(tenants: TenantStore, tenant: str, user: str) -> dict:
    """小さな画面 API の応答（GET / POST で共通の形）。

    画面はユーザーごとの置き場（spaces.json + spaces/）にあり、
    対象は tenant / user で指した組み合わせ。一覧は目次
    （タイトルと日時）だけを新しく更新した順に返し、本文の
    HTML は /api/spaces/content で別に取る。
    """
    store = SpaceStore(user_dir(tenant_dir(tenants.data_dir, tenant),
                                 user))
    return {"tenant": tenant, "user": user,
            "spaces": [s.to_dict() for s in store.list()]}


def _tenant_settings_payload(tenants: TenantStore, name: str) -> dict:
    """テナント設定 API の応答（GET / POST で共通の形）。

    retention_days はそのテナントの指定値（未設定なら None）で、
    retention_effective_days は全体の既定まで含めて解決した実効値。
    approvals_enabled は実効値（運用者ロックが掛かっている間は
    テナント設定の値によらず True）で、approvals_locked はロック
    中かどうか（画面が切り替えを無効化して注記を出すために使う）。
    brain_provider / brain_model はそのテナントの頭脳の指定値
    （未指定の項目は None）で、brain_effective_provider /
    brain_effective_model は全体の既定まで解決した実効値。
    brain_cli は実効頭脳が CLI 系かどうか（注意表示の出し分け
    用）。brain_providers はテナント指定で選べる頭脳の一覧
    （API 系のみ。運用者スイッチが有効なときだけ CLI 系も
    含む。第3段）。brain_key_set はテナントの API
    キーが設定済みか、brain_key_provider はそのキーの対象
    プロバイダ、brain_key_invalid はキーの保存が壊れているか
    （第2段）。API キーの値はどの項目にも含めない。
    """
    locked = tenants._cfg.require_approvals
    brain = brain_setting_for(tenants.data_dir, name)
    eff_provider, eff_model, eff_cli = tenants.effective_brain_for(name)
    key_status = brain_key_status(tenants.data_dir, name)
    return {
        "tenant": name,
        "proactive_enabled": proactive_enabled_for(tenants.data_dir, name),
        "approvals_enabled": (
            locked or approvals_enabled_for(tenants.data_dir, name)),
        "approvals_locked": locked,
        "retention_days": load_retention_overrides(tenants.data_dir).get(name),
        "retention_effective_days": retention_days_for(
            tenants.data_dir, name, tenants.retention_days),
        "brain_provider": brain.get("provider"),
        "brain_model": brain.get("model"),
        "brain_effective_provider": eff_provider,
        "brain_effective_model": eff_model,
        "brain_cli": eff_cli,
        "brain_providers": list(tenant_brain_providers(
            tenants._cfg.allow_tenant_brain_cli)),
        "brain_key_set": key_status["state"] == "set",
        "brain_key_provider": key_status["provider"],
        "brain_key_invalid": key_status["state"] == "invalid",
    }


def _count_tasks_file(user_data_dir: Path) -> tuple[int, int]:
    """用事の一覧ファイル（tasks.json）を直接読み、(総数, 未完了数)。

    TaskStore を作ると旧形式ファイルの取り込み（書き込み）が走る
    ため、削除前の確認のような読み取り専用の集計ではファイルだけを
    読む。ファイルが無い・壊れている場合は (0, 0)。
    """
    try:
        records = json.loads(
            (user_data_dir / "tasks.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0, 0
    if not isinstance(records, list):
        return 0, 0
    open_count = sum(
        1 for r in records
        if isinstance(r, dict) and r.get("status", "open") == "open")
    return len(records), open_count


def _dir_size_bytes(root: Path) -> int:
    """置き場以下のファイルサイズの合計（読めないものは飛ばす）。"""
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _tenant_impact_payload(tenants: TenantStore, name: str) -> dict:
    """テナント削除の確認に出す、影響範囲の集計（GET /api/tenants/impact）。

    そのテナントのユーザーごとに会話（保存済みセッション）と
    用事の一覧を数え、テナント設定（能動発信の有無・保持期限の
    実効値）と置き場の大きさを添える。読み取りだけで、置き場や
    ストアは作らない。
    """
    tdir = tenant_dir(tenants.data_dir, name)
    users = []
    conversations = 0
    tasks = 0
    tasks_open = 0
    for uname in list_user_names(tdir):
        udir = user_dir(tdir, uname)
        conv = len(list_session_ids(udir))
        t_all, t_open = _count_tasks_file(udir)
        users.append({"user": uname, "conversations": conv,
                      "tasks": t_all, "tasks_open": t_open})
        conversations += conv
        tasks += t_all
        tasks_open += t_open
    settings = _tenant_settings_payload(tenants, name)
    return {
        "tenant": name,
        "users": users,
        "user_count": len(users),
        "conversations": conversations,
        "tasks": tasks,
        "tasks_open": tasks_open,
        "proactive_enabled": settings["proactive_enabled"],
        "retention_effective_days": settings["retention_effective_days"],
        "size_bytes": _dir_size_bytes(tdir),
    }


# 公開先の固定（KUMA_WEB_TENANT / KUMA_WEB_USER）に食い違う
# テナント／ユーザーを指定されたときの 403 の文言
_FIXED_PAIR_ERROR = ("公開先が固定されているため、このテナント／"
                     "ユーザーにはアクセスできません")


def make_handler(tenants: TenantStore, page: str, auth: AuthGate,
                 line_bot: LineBot | None = None):
    """TenantStore を受け取るリクエストハンドラクラスを作る。

    auth（AuthGate）が有効なときは、ログイン画面（/login）以外の
    全経路でログインを求める: 画面はログイン画面へリダイレクトし、
    API は 401 を返す。無効なときは従来どおり素通しする。ただし
    LINE の Webhook（/line/webhook）だけは例外で、ログインゲートを
    通さない（LINE のサーバーは Cookie を持たないため。守りは
    line_bot 側の署名検証が担う）。
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

        # -------------------------------------------- 公開先の固定
        # テナント／ユーザーの解決はこの4つのヘルパーに寄せる。
        # 解決（未指定なら起動時の組み合わせ）と正規化に加えて、
        # 公開先の固定（KUMA_WEB_TENANT / KUMA_WEB_USER）に
        # 食い違えば 403 を送って None を返す。呼び出し側は
        # None ならそのまま return する。固定の判定そのものは
        # TenantStore（pair_allowed / tenant_allowed）が持つ。

        def _resolve_tenant_query(self, query: dict) -> str | None:
            """クエリの tenant を解決する（テナント軸だけの API 用）。"""
            name = normalize_tenant_name(
                _first(query.get("tenant")) or tenants.default_tenant)
            if not tenants.tenant_allowed(name):
                self._send_json(403, {"error": _FIXED_PAIR_ERROR})
                return None
            return name

        def _resolve_pair_query(
                self, query: dict) -> tuple[str, str] | None:
            """クエリの tenant / user を解決する。"""
            tenant = normalize_tenant_name(
                _first(query.get("tenant")) or tenants.default_tenant)
            user = normalize_user_name(
                _first(query.get("user")) or tenants.default_user)
            if not tenants.pair_allowed(tenant, user):
                self._send_json(403, {"error": _FIXED_PAIR_ERROR})
                return None
            return tenant, user

        def _resolve_tenant_body(self, data: dict) -> str | None:
            """POST 本文の tenant を解決する（テナント軸だけの API 用）。"""
            name = normalize_tenant_name(
                data.get("tenant") or tenants.default_tenant)
            if not tenants.tenant_allowed(name):
                self._send_json(403, {"error": _FIXED_PAIR_ERROR})
                return None
            return name

        def _resolve_pair_body(
                self, data: dict) -> tuple[str, str] | None:
            """POST 本文の tenant / user を解決する。"""
            tenant = normalize_tenant_name(
                data.get("tenant") or tenants.default_tenant)
            user = normalize_user_name(
                data.get("user") or tenants.default_user)
            if not tenants.pair_allowed(tenant, user):
                self._send_json(403, {"error": _FIXED_PAIR_ERROR})
                return None
            return tenant, user

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
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                tenant, user = pair
                brain_provider, brain_model, brain_cli = (
                    tenants.effective_brain_for(tenant))
                self._send_json(200, {
                    "mode": tenants.mode,
                    "mode_label": mode_label(tenants.mode),
                    "tenant": tenant,
                    "user": user,
                    # 解決済みの組み合わせの実効頭脳。ヘッダーの
                    # 頭脳表示と CLI 系の注意書きの出し分けに使う
                    "brain_provider": brain_provider,
                    "brain_model": brain_model,
                    "brain_cli": brain_cli})
            elif split.path == "/api/tenants":
                fixed = tenants.fixed_tenant
                if fixed is not None:
                    # 公開先の固定中は、切り替え候補は固定先だけ
                    self._send_json(200, {"tenants": [fixed],
                                          "current": fixed})
                else:
                    self._send_json(
                        200, {"tenants": tenants.names(),
                              "current": tenants.default_tenant})
            elif split.path == "/api/tenant_settings":
                name = self._resolve_tenant_query(query)
                if name is None:
                    return
                self._send_json(200, _tenant_settings_payload(tenants, name))
            elif split.path == "/api/tenants/impact":
                name = self._resolve_tenant_query(query)
                if name is None:
                    return
                if name not in list_tenant_names(tenants.data_dir):
                    self._send_json(404, {"error": "テナントが見つかりません"})
                else:
                    self._send_json(
                        200, _tenant_impact_payload(tenants, name))
            elif split.path == "/api/notify_prefs":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(
                    200, _notify_prefs_payload(tenants, *pair))
            elif split.path == "/api/users":
                tenant = self._resolve_tenant_query(query)
                if tenant is None:
                    return
                users = tenants.users(tenant)
                names = users.names()
                fixed_user = tenants.fixed_user
                if fixed_user is not None:
                    # 公開先の固定中は、切り替え候補は固定先だけ
                    names = [fixed_user]
                self._send_json(200, {
                    "users": names,
                    "current": fixed_user or users.default_user})
            elif split.path == "/api/sessions":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                store = tenants.store(*pair)
                self._send_json(200, {"sessions": store.names()})
            elif split.path == "/api/status":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                tenant, user = pair
                store = tenants.store(tenant, user)
                udir = user_dir(tenant_dir(tenants.data_dir, tenant), user)
                t_all, t_open = _count_tasks_file(udir)
                steps = WorkSteps(
                    udir, normalize_session_id(
                        _first(query.get("session")))).items()
                self._send_json(200, {
                    "status": store.status(_first(query.get("session"))),
                    "tasks": {"total": t_all, "open": t_open},
                    "steps": [{"text": s.text, "status": s.status}
                              for s in steps]})
            elif split.path == "/api/approvals":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(200, {"approvals": tenants.approvals
                                      .list_pending(*pair)})
            elif split.path == "/api/choices":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(200, {"choices": tenants.choices
                                      .list_pending(*pair)})
            elif split.path == "/api/outputs":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(
                    200, _outputs_payload(tenants, *pair))
            elif split.path == "/api/outputs/download":
                self._download_output(query)
            elif split.path == "/api/feed":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(
                    200, _feed_payload(tenants, *pair,
                                       _first(query.get("limit")),
                                       _first(query.get("kind"))))
            elif split.path == "/api/ideas":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(
                    200, _ideas_payload(tenants, *pair))
            elif split.path == "/api/goals":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(
                    200, _goals_payload(tenants, *pair))
            elif split.path == "/api/avatar":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(
                    200, _avatar_payload(tenants, *pair))
            elif split.path == "/api/avatar/image":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                tenant, user = pair
                store = AvatarStore(user_dir(
                    tenant_dir(tenants.data_dir, tenant), user))
                item_id = _first(query.get("id"))
                found = (store.read_item(item_id) if item_id
                         else store.read_current())
                if found is not None:
                    body, media_type = found
                elif item_id:
                    self._send_json(
                        404, {"error": "アバターが見つかりません"})
                    return
                else:
                    # 既定の顔（assets/avatar.svg）をそのまま流す
                    body = load_avatar_svg().encode("utf-8")
                    media_type = "image/svg+xml"
                self._send(200, body, media_type,
                           {"Cache-Control": "no-cache",
                            "Content-Security-Policy": "sandbox"})
            elif split.path == "/api/spaces":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(
                    200, _spaces_payload(tenants, *pair))
            elif split.path == "/api/spaces/content":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                tenant, user = pair
                store = SpaceStore(user_dir(
                    tenant_dir(tenants.data_dir, tenant), user))
                space_id = _first(query.get("id")) or ""
                space = store.get(space_id)
                html = store.read_html(space_id)
                if space is None or html is None:
                    self._send_json(
                        404, {"error": "画面が見つかりません"})
                else:
                    self._send_json(200, {"id": space.id,
                                          "title": space.title,
                                          "html": html})
            elif split.path == "/api/usage":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(
                    200, _usage_payload(tenants, *pair))
            elif split.path == "/api/activity":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._send_json(
                    200, _activity_payload(
                        tenants, *pair,
                        _first(query.get("limit"))))
            elif split.path == "/api/export":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                self._download_export(*pair)
            elif split.path == "/api/profile":
                self._get_profile(query)
            elif split.path == "/api/history":
                pair = self._resolve_pair_query(query)
                if pair is None:
                    return
                store = tenants.store(*pair)
                session = store.get(_first(query.get("session")))
                reactions = session.memory.load_reactions()
                agent_reactions = session.memory.load_agent_reactions()
                messages = []
                for m in session.history:
                    entry: dict = {"role": m.role, "content": m.content}
                    if m.attachments:  # 添付がある発話だけ名前一覧を足す
                        entry["attachments"] = [a.name for a in m.attachments]
                    if m.quote:  # 引用がある発話だけ引用元の本文を足す
                        entry["quote"] = m.quote
                    if m.record_index is not None:
                        # 記録された発話だけ、リアクションの宛先と
                        # 現在のリアクションを足す
                        entry["rid"] = m.record_index
                        if reactions.get(m.record_index):
                            entry["reactions"] = reactions[m.record_index]
                        if agent_reactions.get(m.record_index):
                            entry["agent_reaction"] = \
                                agent_reactions[m.record_index]
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
            pair = self._resolve_pair_query(query)
            if pair is None:
                return
            tenant, user = pair
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

        def _import_data(self) -> None:
            """書き出し ZIP からの復元（POST /api/import）。

            対象はクエリの tenant / user（無ければ起動時の組み
            合わせ）で、本文の archive（書き出し ZIP の base64）
            の内容でそのユーザー置き場を置き換える。検証エラー
            （書き出し ZIP でない・不正なパスなど）は 400、置き
            換え中の入出力エラーは 500。復元に成功したら、その
            ユーザーの開いている Session をすべて破棄する（次に
            開く会話は復元後の記録から作り直される）。ログイン
            ゲートは do_POST 側で済んでいる。
            """
            query = parse_qs(urlsplit(self.path).query)
            pair = self._resolve_pair_query(query)
            if pair is None:
                return
            tenant, user = pair
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            encoded = data.get("archive")
            if not isinstance(encoded, str) or not encoded:
                self._send_json(
                    400, {"error": "archive（base64）を指定してください"})
                return
            try:
                zip_bytes = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                self._send_json(
                    400, {"error": "archive の base64 が不正です"})
                return
            try:
                result = restore_user_data(tenants.data_dir, tenant, user,
                                           zip_bytes)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except OSError as exc:
                self._send_json(
                    500, {"error": f"復元に失敗しました: {exc}"})
                return
            tenants.discard_user_sessions(tenant, user)
            self._send_json(200, {
                "ok": True,
                "tenant": result.tenant,
                "user": result.user,
                "source_tenant": result.source_tenant,
                "source_user": result.source_user,
                "restored_files": result.restored_files,
                "skipped_files": result.skipped_files,
                "backup": (result.backup_path.name
                           if result.backup_path is not None else None),
            })

        def _read_json_body(self) -> dict | None:
            """POST の JSON 本文を辞書として読む。読めなければ None。"""
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return None
            return data if isinstance(data, dict) else None

        def _line_webhook(self) -> None:
            """LINE の Webhook（POST /line/webhook）を受ける。

            ログインゲートは通さない（LINE のサーバーは Cookie を
            持たないため）。守りは署名検証で、チャネルシークレット
            とアクセストークンが未設定（line_bot が無い）なら 404、
            署名が合わなければ 400 を返す。署名が合えば即座に 200
            を返し、会話の処理（モデルの呼び出しと返信）は裏側の
            スレッドで行う（Webhook を待たせないため）。本文は
            署名の対象なので、JSON として解釈する前に生のバイト列
            のまま読む。
            """
            if line_bot is None:
                self._send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else b""
            signature = self.headers.get("X-Line-Signature")
            if not line_bot.verify(body, signature):
                self._send_json(400, {"error": "署名が不正です"})
                return
            self._send_json(200, {})
            threading.Thread(
                target=line_bot.process_body, args=(body,),
                daemon=True, name="kuma-line-webhook").start()

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            if path == "/login":
                self._login()
                return
            if path == "/line/webhook":
                self._line_webhook()
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
            if path == "/api/import":
                self._import_data()
                return
            if path == "/api/approvals/decide":
                self._decide_approval()
                return
            if path == "/api/choices/answer":
                self._answer_choice()
                return
            if path == "/api/reactions":
                self._toggle_reaction()
                return
            if path == "/api/ideas/generate":
                self._generate_ideas()
                return
            if path == "/api/ideas/act":
                self._act_idea()
                return
            if path == "/api/goals/act":
                self._act_goal()
                return
            if path == "/api/spaces/act":
                self._act_space()
                return
            if path == "/api/avatar/upload":
                self._upload_avatar()
                return
            if path == "/api/avatar/act":
                self._act_avatar()
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
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            session = tenants.store(*pair).get(data.get("session"))
            self._chat(session, message, attachments, quote)

        def _get_profile(self, query: dict) -> None:
            """人格・記憶ファイルの本文を返す。

            対象は query の tenant / user（無ければ起動時の組み
            合わせ）の置き場にある、編集できる 4 ファイル
            （SOUL.md / IDENTITY.md / USER.md / MEMORY.md）の
            どれか。ファイルが無ければ空の本文を返す。対象外の
            名前は 400。
            """
            pair = self._resolve_pair_query(query)
            if pair is None:
                return
            tenant, user = pair
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
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            tenant, user = pair
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
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            store = tenants.store(*pair)
            session_id = normalize_session_id(data.get("session"))
            deleted = delete_session_record(store.data_dir, session_id)
            store.discard(session_id)
            self._send_json(200, {"deleted": deleted, "session": session_id})

        def _save_tenant_settings(self) -> None:
            """テナント設定（能動発信・承認・保持期間・頭脳）を保存する。

            能動発信と承認と頭脳は settings.json、保持期間は
            retention.json に書く（どちらも既存の仕組み）。
            retention_days に null を渡すとそのテナントの指定を
            取り除いて全体の既定に戻す。brain は {"provider": ...,
            "model": ...} の辞書か null で、項目ごとに null が
            「その項目の指定を取り除く」、brain 自体が null なら
            指定ごと取り除く。渡されていない項目は変更しない。
            テナントの API キー（第2段）は brain_api_key で受け取り、
            settings.json ではなく資格情報ファイル（brain/
            credentials.json）へだけ書く。空文字は「変更しない」で、
            消す場合は brain_key_delete: true を使う。キーの値は
            応答にも記録にも残さない。
            頭脳（キーだけの変更を含む）を保存したら、そのテナントの
            Session を破棄して即時反映する。保存後の最新の設定を返す。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            resolved = self._resolve_tenant_body(data)
            if resolved is None:
                return
            name = resolved
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
                if tenants._cfg.require_approvals and not enabled:
                    # 運用者ロック中は、画面（API）から承認を切る
                    # ことはできない（ロックは合成側の責務）
                    self._send_json(400, {
                        "error": "承認確認は運用者の設定で固定されているため、オフにはできません"})
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
            brain_changed = False
            if "brain" in data:
                brain_value = data["brain"]
                if brain_value is None:
                    provider = model = None
                elif isinstance(brain_value, dict):
                    provider = brain_value.get("provider") or None
                    model = brain_value.get("model") or None
                    if provider is not None and (
                            not isinstance(provider, str)
                            or provider not in ALL_PROVIDERS):
                        self._send_json(400, {
                            "error": "頭脳の種類が不正です"})
                        return
                    if provider in CLI_PROVIDERS \
                            and not tenants._cfg.allow_tenant_brain_cli:
                        # CLI 系のテナント別指定は、運用者スイッチ
                        # （KUMA_ALLOW_TENANT_BRAIN_CLI）が有効な
                        # ときだけ。画面操作だけで「承認の効かない
                        # テナント」を作れないための歯止め
                        self._send_json(400, {
                            "error": "CLI 系の頭脳は、運用者が有効化"
                                     "したときだけテナントごとに"
                                     "指定できます"})
                        return
                    if model is not None and (
                            not isinstance(model, str)
                            or len(model) > MAX_BRAIN_MODEL_CHARS):
                        self._send_json(400, {
                            "error": "モデル名は文字列で指定してください"})
                        return
                else:
                    self._send_json(400, {
                        "error": "brain は辞書か null で指定してください"})
                    return
                set_brain_setting(tenants.data_dir, name, provider, model)
                brain_changed = True
            key_saved = False
            if "brain_api_key" in data:
                key_value = data["brain_api_key"]
                if not isinstance(key_value, str) \
                        or len(key_value) > MAX_BRAIN_API_KEY_CHARS:
                    self._send_json(400, {
                        "error": "API キーが不正です"})
                    return
                key_value = key_value.strip()
                if key_value:
                    # キーは保存後の実効プロバイダ用として記録する
                    # （頭脳の指定が同じリクエストで変わる場合に、
                    # 新しいプロバイダへひも付けるため）
                    eff_provider, _eff_model, eff_cli = \
                        tenants.effective_brain_for(name)
                    if eff_cli:
                        self._send_json(400, {
                            "error": "CLI 系の頭脳には API キーは"
                                     "設定できません"})
                        return
                    save_brain_credentials(
                        tenant_dir(tenants.data_dir, name),
                        eff_provider, key_value)
                    key_saved = True
                    brain_changed = True
                # 空文字は「変更しない」（既存のキーはそのまま残す）
            if "brain_key_delete" in data:
                if not isinstance(data["brain_key_delete"], bool):
                    self._send_json(400, {
                        "error": "brain_key_delete は true / false で"
                                 "指定してください"})
                    return
                if data["brain_key_delete"] and not key_saved:
                    # 新しいキーが同時に渡されていたらそちらを優先し、
                    # 削除はしない
                    if delete_brain_credentials(
                            tenant_dir(tenants.data_dir, name)):
                        brain_changed = True
            if brain_changed:
                # 保存時に即時反映する: そのテナントの Session を
                # 破棄し、次に開いたときに新しい頭脳で作り直させる
                tenants.discard_tenant_sessions(name)
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
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            tenant, user = pair
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
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            tenant, user = pair
            if not tenants.approvals.decide(tenant, user, approval_id, approved):
                self._send_json(404, {"error": "対象の承認が見つかりません"})
                return
            self._send_json(200, {"decided": True, "approved": approved})

        def _answer_choice(self) -> None:
            """保留中の質問（選択肢での聞き返し）に回答する。

            対象は本文の tenant / user（無ければ起動時の組み合わせ）
            の保留だけ。回答すると、回答待ちで止まっていた会話
            リクエストのスレッドが先へ進む。答えとして受け付ける
            のは選択肢のラベルのどれか、または文章での回答を許した
            質問への文章だけ。対象が見つからない・答えが合わない
            場合は 404（すでに時間切れ・回答済みのものを含む）。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            choice_id = data.get("id")
            answer = data.get("answer")
            if not isinstance(choice_id, str) or not choice_id \
                    or not isinstance(answer, str) or not answer.strip():
                self._send_json(400, {
                    "error": "id と answer（空でない文字列）を指定してください"})
                return
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            tenant, user = pair
            if not tenants.choices.answer(tenant, user, choice_id, answer):
                self._send_json(404, {"error": "対象の質問が見つかりません"})
                return
            self._send_json(200, {"answered": True, "answer": answer.strip()})

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
            if not tenants.tenant_allowed(name):
                # 公開先の固定中は、固定先以外のテナントは作れない
                self._send_json(403, {"error": _FIXED_PAIR_ERROR})
                return
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
            if not tenants.tenant_allowed(name):
                # 公開先の固定中は、固定先以外のテナントは消せない
                # （固定先そのものの削除は従来どおりの判定に委ねる）
                self._send_json(403, {"error": _FIXED_PAIR_ERROR})
                return
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
            # 送った直後の発話にもリアクションを付けられるよう、
            # 記録での位置（rid）を一緒に返す
            payload: dict = {"reply": reply}
            for m in reversed(session.history):
                if "rid" not in payload and m.role == "assistant" \
                        and m.record_index is not None and m.content == reply:
                    payload["rid"] = m.record_index
                elif "user_rid" not in payload and m.role == "user" \
                        and m.record_index is not None:
                    payload["user_rid"] = m.record_index
                if "rid" in payload and "user_rid" in payload:
                    break
            self._send_json(200, payload)

        def _generate_ideas(self) -> None:
            """アイデアの提案を生成する（POST /api/ideas/generate）。

            押されたときだけ、その利用者の文脈（進行中の用事・最近の
            フィード・プロフィール）を材料に、委任の子へ提案を考え
            させてカードとして保存する。親セッションはその組み合わせ
            の main を使う。応答は一覧（_ideas_payload）に、生成の
            結果（作られた件数と利用者向けの説明）を generation と
            して添えたもの。提案が無い・失敗のときも 200 で返し、
            画面が generation.message で利用者へ伝えられるようにする。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            tenant, user = pair
            session = tenants.store(tenant, user).get(None)
            target_dir = user_dir(tenant_dir(tenants.data_dir, tenant), user)
            result = generate_ideas(session, target_dir)
            payload = _ideas_payload(tenants, tenant, user)
            payload["generation"] = {
                "outcome": result.outcome,
                "message": result.message,
                "created": len(result.ideas),
            }
            self._send_json(200, payload)

        def _act_idea(self) -> None:
            """アイデアカードへの操作（POST /api/ideas/act）。

            本文は tenant / user / id / action。action は accept
            （やってみる: 用事の一覧へ登録して accepted に、二度押し
            でも二重登録しない）か dismiss（やめておく: dismissed に
            して一覧から外す）。対象のカードが無ければ 404、それ
            以外の action は 400。応答は操作後の一覧。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            tenant, user = pair
            idea_id = data.get("id")
            action = data.get("action")
            if not isinstance(idea_id, str) or not idea_id:
                self._send_json(400, {"error": "id を指定してください"})
                return
            if action not in ("accept", "dismiss"):
                self._send_json(400, {
                    "error": "action は accept か dismiss で指定してください"})
                return
            target_dir = user_dir(tenant_dir(tenants.data_dir, tenant), user)
            handled = (accept_idea(target_dir, idea_id) if action == "accept"
                       else dismiss_idea(target_dir, idea_id))
            if handled is None:
                self._send_json(404, {"error": "アイデアが見つかりません"})
                return
            self._send_json(200, _ideas_payload(tenants, tenant, user))

        def _act_goal(self) -> None:
            """目標への操作（POST /api/goals/act）。

            本文は tenant / user / action と、操作ごとの項目。
            action は次の 6 つ:
            - create: 目標を追加（title、任意で description）
            - update: 目標を編集（id と title / description /
              status のうち変えるもの）
            - note: 進捗メモを追加（id と body）
            - link: 用事を目標へ紐づける（id と task_id）
            - unlink: 用事の紐づけを外す（task_id）
            - delete: 目標を削除（id。用事は消さず紐づけだけ外す）
            対象の目標・用事が無ければ 404、入力の不備は 400。
            応答は操作後の目標一覧（_goals_payload）。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            tenant, user = pair
            target_dir = user_dir(tenant_dir(tenants.data_dir, tenant),
                                  user)
            action = data.get("action")
            goal_id = data.get("id")
            task_id = data.get("task_id")

            def needs_goal() -> bool:
                if not isinstance(goal_id, str) or not goal_id:
                    self._send_json(400, {"error": "id を指定してください"})
                    return False
                return True

            try:
                if action == "create":
                    title = data.get("title")
                    if not isinstance(title, str) or not title.strip():
                        self._send_json(
                            400, {"error": "タイトルを指定してください"})
                        return
                    description = data.get("description")
                    GoalBoard(target_dir).add(
                        title, description if isinstance(
                            description, str) else "")
                elif action == "update":
                    if not needs_goal():
                        return
                    fields: dict = {}
                    for key in ("title", "description", "status"):
                        value = data.get(key)
                        if value is not None:
                            if not isinstance(value, str):
                                self._send_json(
                                    400, {"error": f"{key} は文字列で"
                                                   "指定してください"})
                                return
                            fields[key] = value
                    if GoalBoard(target_dir).update(
                            goal_id, **fields) is None:
                        self._send_json(
                            404, {"error": "目標が見つかりません"})
                        return
                elif action == "note":
                    if not needs_goal():
                        return
                    body = data.get("body")
                    if not isinstance(body, str) or not body.strip():
                        self._send_json(
                            400, {"error": "メモの本文を指定してください"})
                        return
                    if GoalBoard(target_dir).add_note(
                            goal_id, body) is None:
                        self._send_json(
                            404, {"error": "目標が見つかりません"})
                        return
                elif action == "link":
                    if not needs_goal():
                        return
                    if not isinstance(task_id, str) or not task_id:
                        self._send_json(
                            400, {"error": "task_id を指定してください"})
                        return
                    if link_task(target_dir, goal_id, task_id) is None:
                        self._send_json(
                            404, {"error": "目標か用事が見つかりません"})
                        return
                elif action == "unlink":
                    if not isinstance(task_id, str) or not task_id:
                        self._send_json(
                            400, {"error": "task_id を指定してください"})
                        return
                    if unlink_task(target_dir, task_id) is None:
                        self._send_json(
                            404, {"error": "用事が見つかりません"})
                        return
                elif action == "delete":
                    if not needs_goal():
                        return
                    if not delete_goal(target_dir, goal_id):
                        self._send_json(
                            404, {"error": "目標が見つかりません"})
                        return
                else:
                    self._send_json(400, {
                        "error": "action は create / update / note / link / "
                                 "unlink / delete で指定してください"})
                    return
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, _goals_payload(tenants, tenant, user))

        def _act_space(self) -> None:
            """小さな画面への操作（POST /api/spaces/act）。

            本文は tenant / user / action と、操作ごとの項目。
            action は次の 3 つ:
            - create: 画面を追加（title と html）
            - update: 画面を編集（id と、変える title / html）
            - delete: 画面を削除（id）
            対象の画面が無ければ 404、入力の不備は 400。
            応答は操作後の画面一覧（_spaces_payload）。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            tenant, user = pair
            target_dir = user_dir(tenant_dir(tenants.data_dir, tenant),
                                  user)
            store = SpaceStore(target_dir)
            action = data.get("action")
            space_id = data.get("id")

            def needs_space() -> bool:
                if not isinstance(space_id, str) or not space_id:
                    self._send_json(400, {"error": "id を指定してください"})
                    return False
                return True

            try:
                if action == "create":
                    store.create(data.get("title"), data.get("html"))
                elif action == "update":
                    if not needs_space():
                        return
                    fields: dict = {}
                    for key in ("title", "html"):
                        value = data.get(key)
                        if value is not None:
                            if not isinstance(value, str):
                                self._send_json(
                                    400, {"error": f"{key} は文字列で"
                                                   "指定してください"})
                                return
                            fields[key] = value
                    if not fields:
                        self._send_json(
                            400, {"error": "変える項目を指定してください"})
                        return
                    if store.update(space_id, **fields) is None:
                        self._send_json(
                            404, {"error": "画面が見つかりません"})
                        return
                elif action == "delete":
                    if not needs_space():
                        return
                    if store.delete(space_id) is None:
                        self._send_json(
                            404, {"error": "画面が見つかりません"})
                        return
                else:
                    self._send_json(400, {
                        "error": "action は create / update / delete で"
                                 "指定してください"})
                    return
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, _spaces_payload(tenants, tenant, user))

        def _avatar_store(
                self, data: dict) -> tuple[str, str, AvatarStore] | None:
            """アバター API 共通の対象解決（tenant / user / 置き場）。

            公開先の固定に食い違う組み合わせなら 403 を送って
            None を返す（呼び出し側はそのまま return する）。
            """
            pair = self._resolve_pair_body(data)
            if pair is None:
                return None
            tenant, user = pair
            store = AvatarStore(user_dir(
                tenant_dir(tenants.data_dir, tenant), user))
            return tenant, user, store

        def _upload_avatar(self) -> None:
            """アバターのアップロード（POST /api/avatar/upload）。

            本文は tenant / user と、添付と同じ形の画像
            （name / media_type / data_base64）。種別の判定と
            大きさの検証は AvatarStore が中身で行う（media_type
            は信用しない）。アップロードした画像がそのまま
            今のアバターになる。応答は _avatar_payload。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            resolved = self._avatar_store(data)
            if resolved is None:
                return
            tenant, user, store = resolved
            try:
                raw = base64.b64decode(str(data.get("data_base64") or ""),
                                       validate=True)
            except (binascii.Error, ValueError):
                self._send_json(400, {"error": "画像の base64 が不正です"})
                return
            try:
                store.add(raw)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, _avatar_payload(tenants, tenant, user))

        def _act_avatar(self) -> None:
            """アバターへの操作（POST /api/avatar/act）。

            本文は tenant / user / action と id。action は次の 3 つ:
            - select: 候補を今のアバターにする（id）
            - delete: 候補を削除する（id。今のアバターなら既定に戻る）
            - reset: 既定の顔に戻す（候補は残す）
            対象の候補が無ければ 404、入力の不備は 400。
            応答は操作後の _avatar_payload。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            resolved = self._avatar_store(data)
            if resolved is None:
                return
            tenant, user, store = resolved
            action = data.get("action")
            item_id = data.get("id")
            if action == "reset":
                store.reset()
            elif action in ("select", "delete"):
                if not isinstance(item_id, str) or not item_id:
                    self._send_json(400, {"error": "id を指定してください"})
                    return
                done = (store.select(item_id) if action == "select"
                        else store.delete(item_id))
                if done is None:
                    self._send_json(
                        404, {"error": "アバターが見つかりません"})
                    return
            else:
                self._send_json(400, {
                    "error": "action は select / delete / reset で"
                             "指定してください"})
                return
            self._send_json(200, _avatar_payload(tenants, tenant, user))

        def _toggle_reaction(self) -> None:
            """発話へのリアクションを付け外しする。

            本文は tenant / user / session / rid（会話記録での位置）/
            emoji。付け外しと検証（使える絵文字・宛先の発話）は
            Memory.toggle_reaction が担い、その発話の最新の並びを返す。
            応答にはクマのリアクション（リアクション返し、変化後。
            無ければ null）も含め、画面が再読み込みなしで反映できる
            ようにする。
            """
            data = self._read_json_body()
            if data is None:
                self._send_json(400, {"error": "リクエストが不正です"})
                return
            rid = data.get("rid")
            emoji = data.get("emoji")
            if not isinstance(rid, int) or isinstance(rid, bool) \
                    or not isinstance(emoji, str):
                self._send_json(400, {"error": "rid と emoji を指定してください"})
                return
            pair = self._resolve_pair_body(data)
            if pair is None:
                return
            session = tenants.store(*pair).get(data.get("session"))
            with lock:  # 記録の読み書きを直列化する
                try:
                    current = session.memory.toggle_reaction(rid, emoji)
                    agent = session.memory.load_agent_reactions().get(rid)
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)})
                    return
            self._send_json(200, {"rid": rid, "reactions": current,
                                  "agent_reaction": agent})

    return Handler


def _first(values: list[str] | None) -> str | None:
    return values[0] if values else None


# ループバック（このマシンの中だけ）と見なすバインド先。
# これ以外へバインドして起動する場合を「公開」と見なす
# （公開前チェック直しの共通定義）
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback_host(host: str) -> bool:
    """バインド先がループバックかどうかを返す。"""
    return host.strip().lower() in _LOOPBACK_HOSTS


def startup_guard_warning(host: str, cfg: Config) -> str | None:
    """起動時ガードの警告文（出す場合だけ返す）。

    Web UI をループバック以外へバインドして起動する（＝公開）うえ
    パスワード（KUMA_WEB_PASSWORD）が未設定だと、届く範囲の誰でも
    認証なしに画面と API を使えてしまう。その組み合わせのときだけ
    強い警告を返す。起動自体は止めない（判断は運用者に残す）。
    ループバック起動、またはパスワード設定済みなら None。
    """
    if _is_loopback_host(host) or cfg.web_password:
        return None
    return (
        "警告: パスワード（環境変数 KUMA_WEB_PASSWORD）が設定されて"
        f"いないのに、Web UI をこのマシンの外からも届くアドレス"
        f"（{host}）で起動します。このままでは、届く範囲のだれでも"
        "認証なしに画面と API を使えます。外に出す場合はパスワードを"
        "設定してから起動し直してください（起動はこのまま続けます）")


def cli_provider_note(cfg: Config) -> str | None:
    """CLI 系の頭脳が実際に使える状態のときの起動時注意（1行）。

    頭脳が CLI 系（claude-code / gemini-cli / codex）で、その
    コマンドがこのマシンに入っているときだけ返す。CLI 系では
    道具の実行が外部 CLI の側で行われ、kuma-agent 側の承認確認
    や道具の制限は効かない。その誤解を防ぐための明示で、挙動は
    変えない（公開前チェックの指摘1 (a)）。API 系の頭脳、または
    CLI が入っていない環境（Docker 等）では None。
    """
    if not cfg.is_cli_provider:
        return None
    command = cli_command_name(cfg.provider)
    if command is None or shutil.which(command) is None:
        return None
    return (
        f"注意: 頭脳（{cfg.provider}）では、道具の実行は外部の道具"
        "（CLI）の側で行われます。クマ側の承認確認や道具の制限は"
        "効かないため、その前提で使ってください"
        "（委任の型による道具の上限も効きません）")


def tenant_cli_brain_note(cfg: Config) -> str | None:
    """テナント別 CLI が有効なときの起動時注意（1行）。

    運用者スイッチ（環境変数 KUMA_ALLOW_TENANT_BRAIN_CLI）が
    有効な間は、テナントごとに CLI 系の頭脳を指定でき、その
    テナントでは道具の実行が外部 CLI の側で行われ、kuma-agent
    側の承認確認や道具の制限は効かない。起動時の組み合わせの
    頭脳が API 系でもこの状態は生じるため、cli_provider_note
    とは別に、スイッチが有効なこと自体を明示する（公開前
    チェックの指摘1 (a) の第3段分）。CLI 系を指定している
    テナントがあれば、その名前も挙げる。スイッチが無効なら
    None。挙動は変えない。
    """
    if not cfg.allow_tenant_brain_cli:
        return None
    note = (
        "注意: テナントごとに CLI 系の頭脳も選べる設定"
        "（環境変数 KUMA_ALLOW_TENANT_BRAIN_CLI）になっています。"
        "CLI 系のテナントでは、道具の実行は外部の道具（CLI）の"
        "側で行われ、クマ側の承認確認や道具の制限は効きません")
    names = cli_brain_tenants(cfg.data_dir)
    if names:
        note += f"。現在、CLI 系を指定しているテナント: {'、'.join(names)}"
    return note


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
    tenants = TenantStore(session, session_factory)
    # ページの初期表示の頭脳は、起動時の組み合わせの実効頭脳
    # （テナント指定があればその頭脳）。切り替え後の正確な表示は
    # /api/meta が担う
    page = render_page(tenants.startup_provider, web_auth=auth.enabled)
    # LINE の設定（チャネルシークレットとアクセストークン）が
    # そろっていれば、Webhook の処理役を用意する（LINE 用の
    # Session は承認者なしで作る。telegram の経路と同じ流儀）
    line_bot = None
    if line_enabled(session.cfg):
        line_bot = LineBot(
            session.cfg,
            HttpxLineApi(session.cfg.line_channel_access_token),
            model=session.model)
    server = ThreadingHTTPServer(
        (host, port), make_handler(tenants, page, auth, line_bot))
    # 能動発信の巡回が保持中の Session を引けるよう、サーバーに
    # TenantStore をぶら下げておく（serve が使う）
    server.tenants = tenants  # type: ignore[attr-defined]
    server.line_bot = line_bot  # type: ignore[attr-defined]
    return server


def _start_proactive_loop(tenants: TenantStore, cfg: Config) -> None:
    """能動発信の巡回を裏側のスレッドで始める。

    起動後まもなく 1 回、その後は Config.proactive_interval_seconds
    （環境変数 KUMA_PROACTIVE_INTERVAL_SECONDS、既定 300 秒）ごとに
    全テナント×全ユーザーを巡回し、言うべきことがあるユーザーに
    だけ main の会話へ発言する。投稿はサーバーが保持している
    Session を通す。Telegram / LINE が設定されていれば、対象
    ユーザーの投稿はそちらへも届ける（それぞれのモジュールの
    build_proactive_notifier を combine_notifiers でまとめる）。
    間隔が 0 以下なら巡回しない。1 回の巡回の失敗で Web サーバー
    自体は落とさない。
    """
    interval = cfg.proactive_interval_seconds
    if interval <= 0:
        return
    stopped = threading.Event()
    # Telegram / LINE が設定されていれば、投稿をそちらへも届ける
    # 通知を用意する（どちらも未設定・無効化・許可リスト空なら
    # None で従来どおり会話への投稿だけになる）
    notify = combine_notifiers(build_proactive_notifier(cfg),
                               build_line_proactive_notifier(cfg))

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
    guard_warning = startup_guard_warning(host, cfg)
    if guard_warning is not None:
        print(guard_warning)
    cli_note = cli_provider_note(cfg)
    if cli_note is not None:
        print(cli_note)
    tenant_cli_note = tenant_cli_brain_note(cfg)
    if tenant_cli_note is not None:
        print(tenant_cli_note)
    if cfg.web_password:
        print("Web UI のログインが有効です（パスワードは環境変数 KUMA_WEB_PASSWORD）")
    _start_proactive_loop(server.tenants, cfg)  # type: ignore[attr-defined]
    # Telegram のトークンが設定されていれば、裏側スレッドで受信も始める
    # （未設定なら何もしない。Telegram 用の Session は承認者なしで作る）
    if start_telegram_polling(cfg, session.model) is not None:
        print("Telegram の受信を開始しました")
    # LINE は Webhook 方式のため、受信はこのサーバーの
    # /line/webhook が担う（公開 HTTPS で LINE 側へ登録が必要）
    if line_enabled(cfg):
        print("LINE の Webhook を /line/webhook で受け付けています")
    print(f"クマの Web UI: http://{host}:{server.server_address[1]} （終了は Ctrl-C）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
        close_shared_managers()  # MCP サーバーの子プロセスを残さない
