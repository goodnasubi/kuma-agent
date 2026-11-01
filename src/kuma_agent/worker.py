"""1 Issue 分の作業手順：発見→ロック→準備→実装→検証→PR→報告。"""
from __future__ import annotations

import re
from pathlib import Path

from . import gitops, prompts
from .config import Config
from .github import GitHub
from .llm import ModelClient


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:40] or "task"


def pick_issue(issues: list[dict], cfg: Config) -> dict | None:
    """担当可能（ロックなし・blocked でない）な最古の Issue を返す。"""
    for issue in issues:
        labels = {l["name"] for l in issue.get("labels", [])}
        if cfg.blocked_label in labels:
            continue
        if any(l.startswith("agent:") for l in labels):
            continue
        return issue
    return None


def work_on_issue(cfg: Config, gh: GitHub, model: ModelClient, issue: dict) -> str:
    number = issue["number"]
    branch = f"{cfg.agent_name}/{number}-{_slug(issue['title'])}"
    gh.add_labels(cfg.repo, number, [cfg.lock_label])
    gh.comment(cfg.repo, number, f"着手: `{branch}`")

    repo_dir = gitops.ensure_clone(cfg.repo, cfg.github_token, Path(cfg.work_dir) / cfg.name)
    gitops.create_branch(repo_dir, branch)
    report = model.run_task(prompts.SYSTEM_PROMPT, prompts.issue_prompt(issue, branch), str(repo_dir))

    message = f"{issue['title']} (#{number})\n\nAgent: {cfg.agent_name}\nAgent-Env: local"
    gitops.commit_all(repo_dir, message)
    gitops.push(repo_dir, branch)
    pr = gh.create_pull_request(
        cfg.repo,
        title=issue["title"],
        head=branch,
        base="main",
        body=f"Closes #{number}\n\n{report}\n\n実行環境: local（kuma-agent）",
    )
    gh.comment(cfg.repo, number, f"PR を作成しました: {pr['html_url']}")
    return pr["html_url"]
