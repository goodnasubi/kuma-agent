# AGENTS.md（kuma-agent 開発用）

- 言語は Python 3.11+。依存は最小限（httpx のみ）に保つ
- テストは `python -m pytest`。型注釈を付け、`from __future__ import annotations` を使う
- API キー・トークンは環境変数からのみ読む。コードやログに出さない
- コミット末尾にトレーラーを付ける（`Agent: <名前>` / `Agent-Env: <環境>`）
