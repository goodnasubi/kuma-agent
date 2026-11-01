"""GitHub REST API の薄いラッパ（httpx）。"""
from __future__ import annotations

import httpx

API = "https://api.github.com"


class GitHub:
    def __init__(self, token: str) -> None:
        self._client = httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=30.0,
        )

    def list_open_issues(self, repo: str) -> list[dict]:
        """PR を除くオープン Issue を古い順に返す。"""
        resp = self._client.get(
            f"/repos/{repo}/issues",
            params={"state": "open", "sort": "created", "direction": "asc", "per_page": 100},
        )
        resp.raise_for_status()
        return [i for i in resp.json() if "pull_request" not in i]

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        resp = self._client.post(f"/repos/{repo}/issues/{number}/labels", json={"labels": labels})
        resp.raise_for_status()

    def remove_label(self, repo: str, number: int, label: str) -> None:
        resp = self._client.delete(f"/repos/{repo}/issues/{number}/labels/{label}")
        resp.raise_for_status()

    def comment(self, repo: str, number: int, body: str) -> None:
        resp = self._client.post(f"/repos/{repo}/issues/{number}/comments", json={"body": body})
        resp.raise_for_status()

    def create_pull_request(self, repo: str, title: str, head: str, base: str, body: str) -> dict:
        resp = self._client.post(
            f"/repos/{repo}/pulls",
            json={"title": title, "head": head, "base": base, "body": body},
        )
        resp.raise_for_status()
        return resp.json()
