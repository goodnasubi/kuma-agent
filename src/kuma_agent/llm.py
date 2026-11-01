"""Meta Model API（Muse）のクライアントとツール実行ループ。

TODO: 関数呼び出しの形式は Meta Model API の仕様に合わせて実装する。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolResult:
    name: str
    output: str
    is_error: bool = False


class ModelClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def run_task(self, system_prompt: str, user_prompt: str, work_dir: str) -> str:
        """作業ディレクトリ内でツールを実行しながらタスクを進め、
        最終報告のテキストを返す。未実装（設計フェーズ）。"""
        raise NotImplementedError("Meta Model API のツール実行ループは未実装")
