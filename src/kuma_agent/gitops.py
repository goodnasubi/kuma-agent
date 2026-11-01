"""clone / branch / commit / push の操作（git コマンドの subprocess 実行）。"""
from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def ensure_clone(repo: str, token: str, dest: Path) -> Path:
    if (dest / ".git").exists():
        run_git(["fetch", "origin"], dest)
        return dest
    url = f"https://x-access-token:{token}@github.com/{repo}.git"
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["git", "clone", url, str(dest)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git clone failed: {proc.stderr.strip()}")
    return dest


def create_branch(repo_dir: Path, branch: str, base: str = "main") -> None:
    run_git(["checkout", base], repo_dir)
    run_git(["merge", "--ff-only", f"origin/{base}"], repo_dir)
    run_git(["checkout", "-b", branch], repo_dir)


def commit_all(repo_dir: Path, message: str) -> None:
    run_git(["add", "-A"], repo_dir)
    run_git(["commit", "-m", message], repo_dir)


def push(repo_dir: Path, branch: str) -> None:
    run_git(["push", "-u", "origin", branch], repo_dir)
