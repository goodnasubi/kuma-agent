"""テスト用の小さな偽 MCP サーバー（このファイル自体はテストではない）。

stdio で改行区切りの JSON-RPC 2.0 を読み、initialize / tools/list /
tools/call に答える。道具は 3 つ:
- add: 2 つの数を足してテキストで返す
- fail: isError の結果を返す
- marker: 環境変数 FAKE_MCP_MARKER の値を返す（env の受け渡し確認用）
存在しない道具への tools/call には、v2 世代の形式である JSON-RPC
エラー（-32602）を返す。

tests/test_mcp.py から子プロセスとして起動される。pytest の収集
対象にならないよう、ファイル名は test_ で始めない。
"""
import json
import os
import sys

TOOLS = [
    {
        "name": "add",
        "description": "2 つの数を足す",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "fail",
        "description": "必ず失敗する道具",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "marker",
        "description": "環境変数 FAKE_MCP_MARKER の値を返す",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(request_id: object, result: object) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: object, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id,
           "error": {"code": code, "message": message}})


def _call_tool(params: dict) -> tuple[object, bool]:
    """tools/call の結果（または JSON-RPC エラー）を返す。"""
    name = params.get("name")
    args = params.get("arguments") or {}
    if name == "add":
        total = args.get("a", 0) + args.get("b", 0)
        return {"content": [{"type": "text", "text": str(total)}]}, False
    if name == "fail":
        return {"content": [{"type": "text", "text": "失敗しました"}],
                "isError": True}, False
    if name == "marker":
        return {"content": [{"type": "text",
                             "text": os.environ.get("FAKE_MCP_MARKER", "(unset)")}]}, False
    return None, True


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        if method is None:
            continue  # 応答や通知への応答はしない
        request_id = message.get("id")
        if method == "initialize":
            _result(request_id, {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-mcp", "version": "0"},
            })
        elif method == "tools/list":
            _result(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            result, is_error = _call_tool(message.get("params") or {})
            if is_error:
                _error(request_id, -32602,
                       f"Unknown tool: {(message.get('params') or {}).get('name')}")
            else:
                _result(request_id, result)
        elif request_id is not None:
            _error(request_id, -32601, f"Method not found: {method}")
        # 通知（id なし）は読み捨てる


if __name__ == "__main__":
    main()
