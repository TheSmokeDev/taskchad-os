"""Co-founder per-machine state file (US-004).

``cofounder-state.json`` under the STATE_DIR convention holds bookkeeping the
project files must not carry: per-project reply cursor (steering ingest),
completion-check fail streak, wall-clock start, and last dispatch timestamp.

Rule 2: this file is derived bookkeeping, NEVER source of truth for project
status — the project file's frontmatter on disk is. Losing this file costs
cursors and streaks, not project state, which is why a missing or corrupt
file degrades to empty state instead of stopping a pass.

Writes are atomic (tmp + ``os.replace``) under ``shared.file_lock`` — the
same lock the pass-level re-entrancy gate (US-005) takes, so a concurrent
pass can never interleave a read-modify-write. The lock is NOT re-entrant:
``save_state`` acquires it, while :func:`_write_state` assumes the caller
already holds it (``update_project_state`` locks once around load + write).

Per-project entries keep an open schema: future stories add keys (worktree
mtime snapshots, notify sent-markers) without a migration; unknown keys are
preserved on update.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_S = 5.0

STATE_FILENAME = "cofounder-state.json"

# Canonical per-project fields. wall_clock_start / last_dispatch_at are ISO
# strings stamped by the pass (US-011); None until first dispatch.
PROJECT_STATE_DEFAULTS: dict[str, Any] = {
    "reply_cursor": 0,
    "fail_streak": 0,
    "wall_clock_start": None,
    "last_dispatch_at": None,
}


def _resolve_state_file(state_file: Path | str | None) -> Path:
    """None derives STATE_DIR/cofounder-state.json at call time (Rule 1).

    Derived from ``config.STATE_DIR`` per call rather than shipped as a
    ``COFOUNDER_*`` module constant — US-001's Rule-1 lock test forbids any
    module-level COFOUNDER capture in config.
    """
    if state_file is not None:
        return Path(state_file)
    import config

    return Path(config.STATE_DIR) / STATE_FILENAME


def load_state(state_file: Path | str | None = None) -> dict[str, Any]:
    """Load the state mapping; missing file is a clean empty state, while a
    corrupt/unreadable/non-mapping file degrades to empty with a warning."""
    path = _resolve_state_file(state_file)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "cofounder: state file %s unreadable (%s); degrading to empty state",
            path,
            exc,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "cofounder: state file %s is not a JSON object; degrading to empty state",
            path,
        )
        return {}
    return data


def _write_state(state: dict[str, Any], path: Path) -> None:
    """Atomic tmp + os.replace write. The CALLER must hold ``file_lock`` —
    the lock is not re-entrant, so this never acquires it itself."""
    payload = json.dumps(state, indent=2, default=str)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def save_state(state: dict[str, Any], state_file: Path | str | None = None) -> None:
    """Persist the full state mapping atomically under ``shared.file_lock``."""
    path = _resolve_state_file(state_file)
    from shared import file_lock  # local import (living_memory precedent)

    with file_lock(path, timeout=_LOCK_TIMEOUT_S):
        _write_state(state, path)


def get_project_state(state: dict[str, Any], slug: str) -> dict[str, Any]:
    """One project's entry with canonical defaults filled in.

    Returns a copy — mutate-and-save goes through :func:`update_project_state`
    so every write happens under the lock.
    """
    entry = dict(PROJECT_STATE_DEFAULTS)
    projects = state.get("projects")
    if isinstance(projects, dict):
        stored = projects.get(slug)
        if isinstance(stored, dict):
            entry.update(stored)
    return entry


def update_project_state(
    slug: str,
    state_file: Path | str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Merge ``fields`` into one project's entry (read-modify-write under a
    single lock hold). Other projects and unknown keys are preserved.
    Returns the project's new entry."""
    path = _resolve_state_file(state_file)
    from shared import file_lock

    with file_lock(path, timeout=_LOCK_TIMEOUT_S):
        state = load_state(path)
        projects = state.get("projects")
        if not isinstance(projects, dict):
            projects = {}
            state["projects"] = projects
        entry = dict(PROJECT_STATE_DEFAULTS)
        stored = projects.get(slug)
        if isinstance(stored, dict):
            entry.update(stored)
        entry.update(fields)
        projects[slug] = entry
        _write_state(state, path)
    return dict(entry)
