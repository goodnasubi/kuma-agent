"""MCP（Model Context Protocol）クライアント。

データ置き場ルートの mcp.json で指定された MCP サーバーに接続し、
サーバーの道具を kuma-agent の道具として使えるようにする窓口。
トランスポートは stdio のみ（サーバーを子プロセスとして起動し、
改行区切りの JSON-RPC 2.0 で会話する）で、標準ライブラリだけで
実装する（新規依存は入れない）。

プロトコルは広く使われている従来方式（2025 世代）で話す:
initialize（protocolVersion は "2025-11-25" を送り、サーバーが
返した版を受け入れて進める）→ notifications/initialized →
tools/list → tools/call。2026-07-28 版（いわゆる v2）のサーバーも
従来方式のクライアントを受け付けるため、これで新旧両方につながる。
サーバー起点のリクエスト（sampling 等）には対応せず、メソッド
不明のエラーで返す。

設定（mcp.json）は全テナント共通で、形式は
{"servers": [{"name": ..., "command": ..., "args": [...],
              "env": {"K": "V"}}]}。
ファイルが無い・壊れている・項目が不正な場合は、そのサーバー
（または全体）を無効として静かに動く（例外で落とさない）。

サーバーは任意のコマンド実行と同義なので、設定できるのは運用者
だけという前提に立つ（利用者が画面から追加する経路は作らない）。

プロセス管理:
- サーバーは遅延起動（最初に道具一覧が必要になった時点で起動）
  し、プロセスは使い回す。接続はデータ置き場ルート単位で共有
  する（get_shared_manager）ため、Web 経路でもテナントや部屋
  ごとに複製されない。
- 呼び出しはサーバーごとに直列化し、応答待ちにはタイムアウト
  （既定 60 秒、Config の KUMA_MCP_TIMEOUT_SECONDS）を付ける。
  タイムアウトやプロセス終了時はその道具を失敗として扱い、
  接続を捨てて次回に再起動を試みる。
- 終了処理は atexit で共有マネージャを閉じる（Web サーバーの
  serve も明示的に閉じる）。
"""
from __future__ import annotations

import atexit
import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

MCP_FILENAME = "mcp.json"

# こちらから送るプロトコル版（2025 世代）。サーバーが別の版を
# 返した場合は、その版を受け入れてそのまま進める
PROTOCOL_VERSION = "2025-11-25"

DEFAULT_TIMEOUT_SECONDS = 60.0

# 道具名の前置区切り: <サーバー名>__<道具名>
PREFIX_SEPARATOR = "__"


def _client_version() -> str:
    """clientInfo に載せる版数。取れなければ "dev"。"""
    try:
        from importlib.metadata import version

        return version("kuma-agent")
    except Exception:
        return "dev"


# ---------------------------------------------------------------- 設定

@dataclass(frozen=True)
class McpServerConfig:
    """mcp.json のサーバー 1 件分の設定。"""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


def _parse_server(entry: object) -> McpServerConfig | None:
    """mcp.json の 1 項目を設定にする。形が不正なら None（読み飛ばす）。"""
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    command = entry.get("command")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(command, str) or not command.strip():
        return None
    args = entry.get("args", [])
    if (not isinstance(args, list)
            or not all(isinstance(a, str) for a in args)):
        return None
    env = entry.get("env", {})
    if (not isinstance(env, dict)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in env.items())):
        return None
    return McpServerConfig(name=name.strip(), command=command,
                           args=tuple(args), env=dict(env))


def load_server_configs(data_dir: Path) -> list[McpServerConfig]:
    """mcp.json からサーバー設定の一覧を読む。

    ファイルが無い・壊れている場合は空の一覧を返す。不正な項目は
    そのサーバーだけを読み飛ばし、同名のサーバーが重なったら
    最初の 1 件だけを使う。
    """
    try:
        raw = json.loads(
            (data_dir / MCP_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    servers = raw.get("servers")
    if not isinstance(servers, list):
        return []
    configs: list[McpServerConfig] = []
    seen: set[str] = set()
    for entry in servers:
        config = _parse_server(entry)
        if config is None or config.name in seen:
            continue
        seen.add(config.name)
        configs.append(config)
    return configs


# ---------------------------------------------------------------- 接続

class _RpcError(Exception):
    """JSON-RPC のエラー応答、またはそれに準じる接続の失敗。"""

    def __init__(self, code: object, message: str) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code
        self.message = message


def _format_call_result(result: object) -> str:
    """tools/call の結果を道具結果の文字列にする。

    content のテキストブロックを連結する。テキスト以外のブロック
    はその旨を短く記す。本文が無く structuredContent だけがある
    場合は、その JSON を本文にする。isError の結果はモデルに失敗
    と分かるよう "error:" を前置する。
    """
    if not isinstance(result, dict):
        return "error: MCP サーバーから不正な結果が返りました"
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                parts.append(
                    f"（テキスト以外の内容です: {block.get('type', '不明')}）")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"],
                                ensure_ascii=False))
    text = "\n".join(parts)
    if result.get("isError"):
        return f"error: {text}" if text else "error: MCP 道具が失敗しました"
    return text


class _Connection:
    """1 つの MCP サーバープロセスとの接続（stdio、改行区切り JSON-RPC）。

    読み取りは専用スレッドが回し、応答は id で待ち合わせる。
    サーバー起点のリクエストには対応しないため、メソッド不明の
    エラーで返し、通知は読み捨てる。
    """

    def __init__(self, config: McpServerConfig, timeout: float) -> None:
        self.config = config
        self.timeout = timeout
        self.tools: list[dict] = []  # tools/list で得た道具定義（生の形）
        self._proc: subprocess.Popen | None = None
        self._next_id = 0
        self._pending: dict[int, tuple[threading.Event, dict]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._call_lock = threading.Lock()  # tools/call の直列化

    # ------------------------------------------------ 起動と終了

    def start(self) -> None:
        """プロセスを起動し、ハンドシェイクと道具一覧の取得まで行う。

        失敗したら例外を送出し、プロセスは残さない。
        """
        env: dict[str, str] = {}
        for key in ("PATH", "HOME"):  # 最小限だけ引き継ぐ
            value = os.environ.get(key)
            if value:
                env[key] = value
        env.update(self.config.env)
        proc = subprocess.Popen(
            [self.config.command, *self.config.args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env,
            text=True, encoding="utf-8", bufsize=1)
        self._proc = proc
        threading.Thread(target=self._read_loop, daemon=True,
                         name=f"kuma-mcp-{self.config.name}").start()
        try:
            # initialize: 送った版と違う版が返っても受け入れて進める
            self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "kuma-agent",
                               "version": _client_version()},
            })
            self._notify("notifications/initialized", {})
            self.tools = self._list_tools()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """サーバープロセスを止める。何度呼んでもよい。"""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------ 読み取り

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # JSON でない行（サーバーのログ等）は読み捨てる
                if isinstance(message, dict):
                    self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            # プロセス終了: 応答待ちの呼び出しをすべて失敗させる
            with self._pending_lock:
                pending = list(self._pending.values())
                self._pending.clear()
            for event, holder in pending:
                holder["exc"] = _RpcError(
                    -32000, "MCP サーバーが終了しました")
                event.set()

    def _dispatch(self, message: dict) -> None:
        if "method" in message:
            if "id" in message:
                # サーバー起点のリクエスト（sampling 等）には対応しない
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601,
                                      "message": "Method not found"}})
            return  # 通知は読み捨てる
        with self._pending_lock:
            entry = self._pending.pop(message.get("id"), None)
        if entry is None:
            return  # 待ち合わせていない応答（タイムアウト済み等）は捨てる
        event, holder = entry
        error = message.get("error")
        if isinstance(error, dict):
            holder["exc"] = _RpcError(error.get("code"),
                                      str(error.get("message", "JSON-RPC error")))
        else:
            holder["result"] = message.get("result")
        event.set()

    # ------------------------------------------------ 書き込みと要求

    def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise _RpcError(-32000, "MCP サーバーに接続していません")
        line = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()

    def _notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict) -> object:
        """要求を送り、応答をタイムアウト付きで待つ。

        JSON-RPC のエラー応答は _RpcError、タイムアウトは
        TimeoutError、プロセス終了は _RpcError になる。
        """
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            event = threading.Event()
            holder: dict = {}
            self._pending[request_id] = (event, holder)
        try:
            self._send({"jsonrpc": "2.0", "id": request_id,
                        "method": method, "params": params})
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        if not event.wait(self.timeout):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise TimeoutError(
                f"MCP サーバー「{self.config.name}」が "
                f"{self.timeout:g} 秒以内に応答しませんでした")
        if "exc" in holder:
            raise holder["exc"]
        return holder.get("result")

    def _list_tools(self) -> list[dict]:
        """tools/list を（ページングを辿って）すべて集める。"""
        tools: list[dict] = []
        cursor: object = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self._request("tools/list", params)
            if not isinstance(result, dict):
                break
            page = result.get("tools")
            if isinstance(page, list):
                tools.extend(
                    tool for tool in page
                    if isinstance(tool, dict)
                    and isinstance(tool.get("name"), str))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, tool_name: str, args: dict) -> str:
        """tools/call を実行し、結果を文字列にする。

        呼び出しは接続ごとに直列化する。JSON-RPC のエラー応答
        （存在しない道具への -32602 を含む）は例外として送出する
        ため、呼び出し側で失敗の文字列に直すこと。
        """
        with self._call_lock:
            result = self._request(
                "tools/call", {"name": tool_name, "arguments": args})
        return _format_call_result(result)


class McpServer:
    """mcp.json の設定 1 件分のサーバー。遅延起動と再起動を管理する。"""

    def __init__(self, config: McpServerConfig, timeout: float) -> None:
        self.config = config
        self.timeout = timeout
        self._conn: _Connection | None = None
        self._lock = threading.Lock()  # 起動と張り替えの直列化

    def _ensure_connection(self) -> _Connection:
        with self._lock:
            if self._conn is not None and self._conn.alive():
                return self._conn
            conn = _Connection(self.config, self.timeout)
            conn.start()  # 失敗したら例外（接続は None のまま）
            self._conn = conn
            return conn

    def _drop(self, conn: _Connection) -> None:
        """壊れた接続を捨てる（次回の呼び出しで再起動を試みる）。"""
        with self._lock:
            if self._conn is conn:
                self._conn = None
        conn.close()

    def ensure_tools(self) -> list[dict]:
        """道具一覧を返す。起動できなければ空の一覧（例外にしない）。"""
        try:
            return self._ensure_connection().tools
        except Exception:
            return []

    def call(self, tool_name: str, args: dict) -> str:
        """道具を実行し、結果（失敗時は失敗と分かる文字列）を返す。"""
        try:
            conn = self._ensure_connection()
        except Exception as exc:
            return (f"error: MCP サーバー「{self.config.name}」に"
                    f"接続できませんでした: {exc}")
        try:
            return conn.call_tool(tool_name, args)
        except Exception as exc:
            # タイムアウト・プロセス終了・JSON-RPC エラーなど。
            # 接続は捨てて、次回の呼び出しで再起動を試みる
            self._drop(conn)
            return f"error: MCP 道具の呼び出しに失敗しました: {exc}"

    def close(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------- マネージャ

class McpManager:
    """mcp.json のサーバー群と、それらが提供する道具の窓口。

    道具名は <サーバー名>__<道具名> と前置して組み込み道具との
    衝突を避ける。説明文と入力スキーマ（inputSchema）はサーバーの
    ものをそのまま使う。
    """

    def __init__(self, data_dir: Path,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self.data_dir = data_dir
        self.timeout = (timeout_seconds if timeout_seconds > 0
                        else DEFAULT_TIMEOUT_SECONDS)
        self._servers: dict[str, McpServer] = {
            config.name: McpServer(config, self.timeout)
            for config in load_server_configs(data_dir)
        }
        # 前置名 → （サーバー, 元の道具名）。発見のたびに更新する
        self._routes: dict[str, tuple[McpServer, str]] = {}
        self._routes_lock = threading.Lock()

    def has_servers(self) -> bool:
        """mcp.json に有効なサーバー設定があるかどうか。"""
        return bool(self._servers)

    def tool_schemas(self) -> list[dict]:
        """発見した道具を、道具登録用のスキーマ一覧で返す。

        起動できないサーバーの道具は出ないだけ（例外にしない）。
        起動済みのサーバーは道具一覧を使い回し、落ちている
        サーバーはこの呼び出しで再起動を試みる。
        """
        schemas: list[dict] = []
        routes: dict[str, tuple[McpServer, str]] = {}
        for server_name, server in self._servers.items():
            for tool in server.ensure_tools():
                tool_name = tool["name"]
                prefixed = f"{server_name}{PREFIX_SEPARATOR}{tool_name}"
                routes[prefixed] = (server, tool_name)
                description = tool.get("description")
                schema = tool.get("inputSchema")
                schemas.append({
                    "name": prefixed,
                    "description": (description
                                    if isinstance(description, str) else ""),
                    "parameters": (schema if isinstance(schema, dict)
                                   else {"type": "object", "properties": {}}),
                })
        with self._routes_lock:
            self._routes.update(routes)
        return schemas

    def call(self, prefixed_name: str, args: dict) -> str:
        """前置名で道具を実行する。失敗でも例外にせず文字列で返す。"""
        with self._routes_lock:
            route = self._routes.get(prefixed_name)
        if route is None:
            return f"error: unknown tool {prefixed_name}"
        server, tool_name = route
        return server.call(tool_name, args)

    def close(self) -> None:
        """すべてのサーバープロセスを止める。"""
        for server in self._servers.values():
            server.close()


# ---------------------------------------------------------------- 共有

_shared_managers: dict[Path, McpManager] = {}
_shared_lock = threading.Lock()
_atexit_registered = False


def get_shared_manager(data_dir: Path,
                       timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
                       ) -> McpManager:
    """データ置き場ルート単位で共有するマネージャを返す。

    Web 経路でも CLI 経路でも、同じ置き場に対しては同じマネージャ
    （＝同じサーバープロセス群）を使い回す。プロセス終了時の掃除は
    atexit に登録する。タイムアウトは最初に作ったときの値が使われる。
    """
    global _atexit_registered
    key = data_dir.resolve()
    with _shared_lock:
        manager = _shared_managers.get(key)
        if manager is None:
            manager = McpManager(data_dir, timeout_seconds)
            _shared_managers[key] = manager
            if not _atexit_registered:
                atexit.register(close_shared_managers)
                _atexit_registered = True
        return manager


def close_shared_managers() -> None:
    """共有マネージャをすべて閉じる（atexit と Web の終了処理用）。"""
    with _shared_lock:
        managers = list(_shared_managers.values())
        _shared_managers.clear()
    for manager in managers:
        manager.close()
