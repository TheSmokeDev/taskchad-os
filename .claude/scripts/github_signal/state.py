"""GitHub signal state — the shared substrate between the cron engine and the bot.

Both processes mutate exactly one file (`github-signal-state.json`), always
under ``shared.file_lock``. The lifecycle map is SPARSE (Rule 2): a repo with
no entry is fresh/eligible; only deviations (`used` / `snoozed` / `surfaced`)
are stored. The engine never holds the lock across the LLM call — it snapshots
at start and merges at end via :func:`finalize_run`, which never downgrades an
operator-set ``used``/``snoozed`` status.
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from shared import file_lock, load_state, save_state  # noqa: E402

from github_signal.config import GITHUB_SIGNAL_STATE_FILE  # noqa: E402

_FULL_NAME_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
# Public alias — eval_runner and the /stars handler validate against this.
FULL_NAME_RE = _FULL_NAME_RE


def load() -> dict[str, Any]:
    """Load state (fail-open to ``{}`` on missing/corrupt file)."""
    return load_state(GITHUB_SIGNAL_STATE_FILE)


def _resolve_name(state: dict[str, Any], name: str) -> str | None:
    """Resolve operator input to a canonical full_name.

    Exact ``owner/repo`` input is accepted as-is (shape-validated). A bare
    name suffix-matches against ``last_picks`` then existing ``repos``
    entries — phone-typing convenience for `/stars used uv`.
    """
    name = name.strip()
    if not name:
        return None
    if "/" in name:
        return name if _FULL_NAME_RE.fullmatch(name) else None
    suffix = f"/{name.lower()}"
    for pick in state.get("last_picks", []):
        full = str(pick.get("full_name", ""))
        if full.lower().endswith(suffix):
            return full
    for full in state.get("repos", {}):
        if full.lower().endswith(suffix):
            return full
    return None


def resolve_name(name: str) -> str | None:
    """Public read-only resolver: exact owner/repo or bare-name suffix match."""
    return _resolve_name(load(), name)


def record_eval(full_name: str, recommendation: str) -> None:
    """Merge eval facts into the repo's entry under lock. ADDITIVE ONLY:
    existing keys (including used/snoozed status) are preserved; a repo with
    no entry gains one with NO status key, which eligible_backlog treats as
    eligible — evaluation never changes lifecycle."""
    with file_lock(GITHUB_SIGNAL_STATE_FILE, timeout=5.0):
        state = load()
        repos = state.setdefault("repos", {})
        entry = repos.setdefault(full_name, {})
        entry["evaluated_at"] = date.today().isoformat()
        entry["eval_recommendation"] = recommendation
        save_state(state, GITHUB_SIGNAL_STATE_FILE)


def mark_used(name: str) -> str | None:
    """Mark a repo used. Returns the resolved full_name, or None if unknown."""
    with file_lock(GITHUB_SIGNAL_STATE_FILE, timeout=5.0):
        state = load()
        full = _resolve_name(state, name)
        if full is None:
            return None
        repos = state.setdefault("repos", {})
        repos[full] = {"status": "used", "used_at": date.today().isoformat()}
        save_state(state, GITHUB_SIGNAL_STATE_FILE)
        return full


def mark_snoozed(name: str, weeks: int) -> str | None:
    """Snooze a repo for N weeks. Returns the resolved full_name, or None."""
    with file_lock(GITHUB_SIGNAL_STATE_FILE, timeout=5.0):
        state = load()
        full = _resolve_name(state, name)
        if full is None:
            return None
        today = date.today()
        repos = state.setdefault("repos", {})
        repos[full] = {
            "status": "snoozed",
            "snoozed_at": today.isoformat(),
            "snooze_until": (today + timedelta(weeks=weeks)).isoformat(),
        }
        save_state(state, GITHUB_SIGNAL_STATE_FILE)
        return full


def eligible_backlog(
    state: dict[str, Any],
    inventory: list[dict[str, Any]],
    cooldown_weeks: int,
) -> list[dict[str, Any]]:
    """Pure eligibility filter over the fetched inventory.

    Excludes: ``used`` (forever), ``snoozed`` until ``snooze_until`` passes,
    and ``surfaced`` within the cooldown window. Absence of an entry — or an
    expired snooze / stale surfacing — means eligible.
    """
    repos = state.get("repos", {})
    today = date.today()
    cutoff = today - timedelta(weeks=cooldown_weeks)
    out: list[dict[str, Any]] = []
    for item in inventory:
        entry = repos.get(item.get("full_name"))
        if not entry:
            out.append(item)
            continue
        status = entry.get("status")
        if status == "used":
            continue
        if status == "snoozed":
            until = _parse_date(entry.get("snooze_until"))
            if until is not None and until >= today:
                continue
            out.append(item)
            continue
        if status == "surfaced":
            surfaced = _parse_date(entry.get("surfaced_at"))
            if surfaced is not None and surfaced > cutoff:
                continue
            out.append(item)
            continue
        out.append(item)
    return out


def finalize_run(
    *,
    result: str,
    watermark: str | None = None,
    inventory_names: set[str] | None = None,
    inventory_count: int | None = None,
    new_stars_count: int | None = None,
    picked: list[dict[str, Any]] | None = None,
    trending: list[dict[str, Any]] | None = None,
    run_time: str | None = None,
) -> None:
    """Merge run results into state under lock (never downgrades operator flags).

    Re-loads the file so a `/stars used` that landed mid-run (during the LLM
    call, outside our lock) survives: picked repos only flip to ``surfaced``
    when they have no ``used``/``snoozed`` entry. Entries for repos no longer
    in the inventory (unstarred) are pruned when ``inventory_names`` is given.
    Watermark is only advanced when a value is passed — a failed run leaves it
    untouched so the next run re-scans the same window.
    """
    with file_lock(GITHUB_SIGNAL_STATE_FILE, timeout=5.0):
        state = load()
        if run_time is not None:
            state["last_run"] = run_time
        state["last_result"] = result
        if watermark is not None:
            state["starred_watermark"] = watermark
        if inventory_count is not None:
            state["inventory_count"] = inventory_count
        if new_stars_count is not None:
            state["new_stars_last_run"] = new_stars_count

        repos = state.setdefault("repos", {})
        today = date.today().isoformat()
        if picked is not None:
            state["last_picks"] = [
                {"full_name": p.get("full_name"), "why_now": p.get("why_now", "")}
                for p in picked
            ]
            for pick in picked:
                full = pick.get("full_name")
                if not full:
                    continue
                current = repos.setdefault(full, {})
                if current.get("status") in ("used", "snoozed"):
                    continue
                # Merge, don't replace — additive keys (evaluated_at, ...) survive.
                current.update({"status": "surfaced", "surfaced_at": today})
        if trending is not None:
            state["last_trending"] = trending

        if inventory_names is not None:
            for full in [k for k in repos if k not in inventory_names]:
                del repos[full]

        save_state(state, GITHUB_SIGNAL_STATE_FILE)


def _parse_date(raw: Any) -> date | None:
    try:
        return date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
