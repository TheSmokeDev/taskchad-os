"""Update-availability check for the taskchad-os CLI — gh/npm/brew-style.

Compares the installed version (``.claude/scripts/pyproject.toml``) against
the latest non-prerelease GitHub release. Every check is TTL-cached
(``UPDATE_CHECK_MIN_INTERVAL_HOURS``) so the network is hit at most once per
interval, and every failure mode (network, timeout, bad JSON, missing file)
resolves to ``None`` — this must never break a CLI invocation.
"""

from __future__ import annotations

import json
import tomllib
import urllib.error
import urllib.request
from datetime import datetime

from config import (
    SCRIPTS_DIR,
    UPDATE_CHECK_MIN_INTERVAL_HOURS,
    UPDATE_CHECK_REPO,
    UPDATE_CHECK_STATE_FILE,
    now_local,
)
from shared import file_lock, load_state, save_state

_PYPROJECT_PATH = SCRIPTS_DIR / "pyproject.toml"


def get_current_version() -> str:
    """Read the installed version from pyproject.toml. Never raises — '0.0.0' on failure."""
    try:
        with open(_PYPROJECT_PATH, "rb") as f:
            data = tomllib.load(f)
        return str(data["project"]["version"])
    except Exception:
        return "0.0.0"


def _version_tuple(version: str) -> tuple[int, int, int]:
    """Parse 'X.Y.Z' (optional leading 'v') into a comparable int tuple. Malformed -> (0, 0, 0)."""
    cleaned = version.strip().lstrip("vV")
    parts = cleaned.split(".")[:3]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return (0, 0, 0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def get_latest_release_version(timeout: float = 2.0) -> str | None:
    """Fetch the latest non-prerelease GitHub release tag. Returns None on any failure."""
    url = f"https://api.github.com/repos/{UPDATE_CHECK_REPO}/releases/latest"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "TheHomie-UpdateCheck/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        tag = data.get("tag_name")
        if not tag:
            return None
        return str(tag).lstrip("vV")
    except Exception:
        return None


def check_for_update() -> tuple[str, str] | None:
    """TTL-cached update check. Returns (current, latest) if behind, else None. Never raises."""
    try:
        current = get_current_version()
        state = load_state(UPDATE_CHECK_STATE_FILE)
        latest = state.get("latest_version")

        needs_refresh = True
        last_checked = state.get("last_checked")
        if last_checked:
            try:
                last = datetime.fromisoformat(last_checked)
                elapsed_h = (now_local() - last).total_seconds() / 3600
                needs_refresh = elapsed_h >= UPDATE_CHECK_MIN_INTERVAL_HOURS
            except (ValueError, TypeError):
                needs_refresh = True

        if needs_refresh:
            fetched = get_latest_release_version()
            if fetched is not None:
                latest = fetched
            try:
                with file_lock(UPDATE_CHECK_STATE_FILE, timeout=1.0):
                    save_state(
                        {"last_checked": now_local().isoformat(), "latest_version": latest},
                        UPDATE_CHECK_STATE_FILE,
                    )
            except TimeoutError:
                pass

        if not latest:
            return None
        if _version_tuple(latest) > _version_tuple(current):
            return (current, latest)
        return None
    except Exception:
        return None
