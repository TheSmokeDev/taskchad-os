"""Starred-repo inventory fetch — stdlib urllib, mirrors watchers/watch_github.py.

Read-only GitHub access; no capability registration needed (watch_github
precedent — the integration gate is for mutating actions).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

GITHUB_API_BASE = "https://api.github.com"


class FetchError(Exception):
    """Starred fetch failed — message carries HTTP status / cause."""


def _headers() -> dict[str, str]:
    headers = {
        # star+json variant adds starred_at alongside each repo object.
        "Accept": "application/vnd.github.star+json",
        "User-Agent": "TheHomie-Watcher/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# Public alias — eval_runner reuses the exact auth/header contract.
def api_headers() -> dict[str, str]:
    return _headers()


def _flatten(item: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one /user/starred entry to the fields the pipeline uses.

    Handles both the star+json wrapper ({starred_at, repo}) and a plain repo
    object (defensive — some proxies drop the Accept variant).
    """
    repo = item.get("repo") if isinstance(item.get("repo"), dict) else item
    full_name = repo.get("full_name")
    if not full_name:
        return None
    return {
        "full_name": full_name,
        "description": (repo.get("description") or "").strip(),
        "language": repo.get("language") or "",
        "topics": repo.get("topics") or [],
        "html_url": repo.get("html_url") or f"https://github.com/{full_name}",
        "starred_at": item.get("starred_at") or "",
        "pushed_at": repo.get("pushed_at") or "",
        "stargazers_count": repo.get("stargazers_count") or 0,
    }


def fetch_starred(
    timeout: float = 30.0,
    per_page: int = 100,
    max_pages: int = 10,
) -> list[dict[str, Any]]:
    """Fetch the authenticated user's full starred inventory (newest first).

    Paginates while pages come back full. Raises :class:`FetchError` on any
    HTTP / network / decode failure — the caller decides run outcome.
    """
    items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        url = f"{GITHUB_API_BASE}/user/starred?per_page={per_page}&page={page}"
        req = urllib.request.Request(url)
        for k, v in _headers().items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise FetchError(
                    "GitHub API returned 401 — set GITHUB_TOKEN (classic PAT, "
                    "no extra scopes needed for public repos) in "
                    ".claude/scripts/.env"
                ) from e
            raise FetchError(f"GitHub API HTTP {e.code} on page {page}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise FetchError(f"GitHub API network error: {e}") from e

        try:
            batch = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise FetchError(f"GitHub API returned invalid JSON: {e}") from e
        if not isinstance(batch, list):
            raise FetchError(
                f"GitHub API returned {type(batch).__name__}, expected list"
            )

        items.extend(f for i in batch if isinstance(i, dict) and (f := _flatten(i)))
        if len(batch) < per_page:
            break
    return items
