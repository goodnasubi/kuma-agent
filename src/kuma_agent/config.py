"""設定。環境変数と CLI 引数から組み立てる。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    repo: str  # "owner/name"
    agent_name: str = "kuma"
    lock_label: str = "agent:kuma"
    blocked_label: str = "blocked"
    needs_human_label: str = "needs-human"
    poll_interval_sec: int = 300
    max_fix_attempts: int = 3
    verify_commands: list[str] = field(default_factory=list)
    work_dir: str = "work"
    model: str = "muse-wonder"
    github_token: str = field(default_factory=lambda: os.environ.get("GITHUB_TOKEN", ""))
    model_api_key: str = field(default_factory=lambda: os.environ.get("META_MODEL_API_KEY", ""))

    @property
    def owner(self) -> str:
        return self.repo.split("/", 1)[0]

    @property
    def name(self) -> str:
        return self.repo.split("/", 1)[1]
