"""
Persona Learning Tick — Scheduled fan-out for persona reflection pipelines.

Enumerates learning-enabled personas via call-time config reads and spawns
per-persona reflection (memory_reflect.py -p <name>) as subprocesses on
cheap background model tiers. One cron/scheduler entry for ALL personas.

CRITICAL: config.py:40 binds paths at import time. The tick itself runs as
the DEFAULT profile and NEVER loops profiles in-process — each persona
pipeline runs as a subprocess with HOMIE_HOME set by build_capability_scoped_env.

Usage:
    uv run python persona_learning_tick.py           # Run learning tick
    uv run python persona_learning_tick.py --test    # Dry run (no subprocess spawn)
    uv run python persona_learning_tick.py --once    # Single persona (first eligible)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override

apply_persona_override()

from config import (  # noqa: E402
    STATE_DIR,
    get_background_models,
    get_persona_learning_settings,
)
from personas import get_default_paths  # noqa: E402
from personas.capabilities import build_capability_scoped_env  # noqa: E402
from personas.lifecycle import list_profiles  # noqa: E402
from personas.services import is_active_default_profile, load_persona_config  # noqa: E402
from shared import load_state, safe_exc_text, save_state  # noqa: E402

# Inject .claude/chat for session store access
_CHAT_DIR = Path(__file__).resolve().parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from session import get_session_store  # noqa: E402
from cognition.proactive_brief import normalize_physical_timestamp  # noqa: E402

_SCRIPTS_DIR = Path(__file__).resolve().parent


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _persona_state_file(persona_name: str) -> Path:
    return STATE_DIR / f"persona-learning-{persona_name}-state.json"


def _resolve_since_boundary(
    since_iso: str | None,
    *,
    silent_skip_window_hours: float,
) -> datetime:
    """The ONE effective-boundary owner for both halves of the gate.

    ``since_iso`` (the persona's ``last_run`` stamp) wins when present and
    parsable, else ``now - silent_skip_window_hours``. Always returned as a
    NAIVE LOCAL datetime via the canonical ``normalize_physical_timestamp``
    owner, so chat-row ``updated_at`` values and note-file mtimes — which are
    both naive local — compare against the same instant. Extracted so the
    rows half and the notes half can never drift onto different boundaries.
    """
    since_dt = normalize_physical_timestamp(since_iso)
    if since_dt is None:
        boundary = datetime.now(timezone.utc) - timedelta(
            hours=silent_skip_window_hours
        )
        since_dt = normalize_physical_timestamp(boundary)
    # normalize_physical_timestamp on a real aware datetime never returns
    # None; the fallback keeps the return type honest for a type checker.
    return since_dt or datetime.now()


def _count_fresh_notes_since(
    persona_id: str,
    since_iso: str | None,
    profile_root: Path,
    *,
    silent_skip_window_hours: float,
) -> int:
    """Count the persona's OWN note files modified after the boundary.

    The second half of the composed gate (architecture Q3: *chat rows OR
    fresh notes*). A persona that does WORK — worktick assignments, crypto
    market rounds, operator ingests — leaves deterministic notes under
    ``PERSONA_NOTE_DIRS`` even when it never held a chat turn, and before
    this half existed such a persona was silent-skipped forever with a rich
    corpus sitting unread on disk.

    Freshness is note-file mtime vs the SAME boundary the row count uses
    (never a string compare — mtimes are naive local, ``last_run`` is
    aware-UTC). Reads the profile root as a physical directory (Rule 2); no
    roster or config claim is consulted. Returns 0 on any error (fail-open),
    exactly like the row count.
    """
    try:
        from personas.experience import count_fresh_notes

        boundary = _resolve_since_boundary(
            since_iso, silent_skip_window_hours=silent_skip_window_hours
        )
        return count_fresh_notes(Path(profile_root) / "memory", boundary)
    except Exception as exc:
        print(
            f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_id}]: "
            f"note-count error ({safe_exc_text(exc)}), treating as 0 (fail-open)"
        )
        return 0


def is_learning_eligible(cfg: dict) -> bool:
    """Whether a persona's config.yaml makes it eligible for the tick.

    Single source of truth for the tick's admission check (issue #422) — the
    same absent/malformed-``learning``-means-ineligible logic every creation
    door's regression test must prove a newborn clears, called here instead
    of re-derived so a future change to the check can't drift from what the
    tests assert.
    """
    learning = cfg.get("learning", {})
    if not isinstance(learning, dict):
        return False
    return bool(learning.get("enabled", False))


def _count_attributed_rows_since(
    persona_id: str,
    since_iso: str | None,
    chat_db_path: Path,
    *,
    silent_skip_window_hours: float,
) -> int:
    """Count sessions with this persona_id updated after the effective boundary.

    Uses the EXPLICIT install-DB path (the R1 keystone) so parent and child
    agree on the data source. The boundary is `since_iso` (the persona's
    `last_run` stamp) when present and parsable, else `now -
    silent_skip_window_hours` (PERSONA_LEARNING_SILENT_SKIP_WINDOW) — cold
    start (and a corrupted stamp) is bounded by the same window instead of
    scanning every session ever. Both the boundary and each session's
    `updated_at` are normalized through the canonical
    `normalize_physical_timestamp` owner (naive local) before comparing as
    datetimes, never as strings — `last_run` is stamped aware-UTC while
    `updated_at` is naive-local (SQLite) or aware (Postgres), and a raw
    string compare is wrong in both directions depending on the box's UTC
    offset: it silently undercounts (misses real rows) on a UTC-negative box
    and could overcount (spurious triggers) on a UTC-positive one. Returns 0
    on any error (fail-open).
    """
    try:
        store = get_session_store(chat_db_path=chat_db_path)
        sessions = store.list_active(persona_id=persona_id)

        since_dt = _resolve_since_boundary(
            since_iso, silent_skip_window_hours=silent_skip_window_hours
        )

        count = 0
        for s in sessions:
            updated_dt = normalize_physical_timestamp(s.updated_at)
            if updated_dt is not None and updated_dt > since_dt:
                count += 1
        return count
    except Exception as exc:
        print(
            f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_id}]: "
            f"row-count error ({safe_exc_text(exc)}), treating as 0 (fail-open)"
        )
        return 0


def _spawn_persona_pipeline(
    persona_name: str,
    profile_root: Path,
    *,
    test_mode: bool = False,
    notes_since: str | None = None,
) -> tuple[bool, str]:
    """Spawn memory_reflect.py -p <persona> as a subprocess.

    ``notes_since`` is the effective gate boundary, passed EXPLICITLY because
    parent and child do not share a ``STATE_DIR``: the child re-roots under
    the profile via the boot shim, so it cannot read the parent's
    ``persona-learning-<name>-state.json`` stamp. Without this the child
    would fall back to its own window and distil notes the gate never
    counted. Serialized as a naive-local ISO string — the same clock base
    ``normalize_physical_timestamp`` hands back on both sides.

    Returns (success, message).
    """
    try:
        env = build_capability_scoped_env(persona_name, profile_root=profile_root)
    except Exception as exc:
        return False, f"env build failed: {exc}"

    cmd = [
        sys.executable,
        str(_SCRIPTS_DIR / "memory_reflect.py"),
        "-p", persona_name,
    ]
    if notes_since:
        cmd.extend(["--notes-since", notes_since])
    if test_mode:
        cmd.append("--test")

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(_SCRIPTS_DIR),
        )
        if result.returncode == 0:
            return True, "success"
        stderr_tail = (result.stderr or "")[-500:]
        return False, f"exit {result.returncode}: {stderr_tail}"
    except subprocess.TimeoutExpired:
        return False, "timeout (300s)"
    except Exception as exc:
        return False, f"spawn error: {exc}"


def run_tick(*, test_mode: bool = False, once: bool = False) -> None:
    """Main tick: enumerate learning-enabled personas, spawn pipelines."""
    settings = get_persona_learning_settings()
    if not settings.enabled:
        print(f"[{_now_iso()}] [persona-learning] disabled via PERSONA_LEARNING_ENABLED")
        return

    if not is_active_default_profile():
        print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: must run under default profile, skipping")
        return

    install_db = get_default_paths()["data"] / "chat.db"

    profiles = list_profiles()
    named_profiles = [p for p in profiles if not p.is_default]

    if not named_profiles:
        print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: no named profiles found, exiting")
        return

    bg_models = get_background_models()
    os.environ["SECOND_BRAIN_BACKGROUND_QUALITY_MODEL"] = bg_models["quality"]

    eligible: list[tuple[str, Path]] = []
    for profile in named_profiles:
        try:
            cfg = load_persona_config(profile.name)
        except Exception as exc:
            print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{profile.name}]: config error ({exc}), skip")
            continue

        if not is_learning_eligible(cfg):
            continue

        eligible.append((profile.name, profile.path))

    if not eligible:
        print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: no learning-enabled personas, exiting")
        return

    print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: {len(eligible)} learning-enabled persona(s)")

    for persona_name, profile_root in eligible:
        state_file = _persona_state_file(persona_name)
        state = load_state(state_file)
        last_run = state.get("last_run")

        # Recency guard (PERSONA_LEARNING_TICK_INTERVAL): skip a persona that
        # ran more recently than the interval. Fail-open — an absent or
        # unparseable stamp never blocks a run.
        #
        # Reads `last_attempt`, NOT `last_run`. The two were one field until the
        # watermark bug: `last_run` is the FRESHNESS BOUNDARY (what the gate
        # counts notes and rows against) and must not move when the child
        # failed, or unprocessed notes age out of the window forever. But a
        # frozen boundary would also freeze this throttle and re-spawn the child
        # on every tick. So `last_attempt` records that a spawn happened
        # (always) and `last_run` records that one SUCCEEDED (only then). A
        # legacy state file has no `last_attempt`, so fall back to `last_run` —
        # the exact pre-split behavior for one tick, then it self-heals.
        last_attempt = state.get("last_attempt") or last_run
        if last_attempt and settings.tick_interval_hours > 0:
            try:
                last_dt = datetime.fromisoformat(last_attempt)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                hours_since = (
                    datetime.now(timezone.utc) - last_dt
                ).total_seconds() / 3600.0
                if hours_since < settings.tick_interval_hours:
                    print(
                        f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: "
                        f"recency guard ({hours_since:.1f}h < "
                        f"{settings.tick_interval_hours}h), skipping"
                    )
                    if once:
                        break
                    continue
            except Exception:
                pass  # fail-open: a bad stamp never blocks the run

        # The composed gate (architecture Q3): chat rows OR fresh notes.
        # Both halves resolve the SAME boundary via _resolve_since_boundary,
        # and that boundary is handed to the child as --notes-since so the
        # distiller reads exactly the notes this gate counted.
        # This owner is called OUTSIDE either counter's fail-open, so anything
        # it raises used to abort the WHOLE fan-out — every later persona
        # skipped because of one bad env value (NaN/inf reached `timedelta`).
        # Contain it here: this persona degrades to "skip, retry next tick"
        # and the rest of the roster still runs. The watermark is untouched,
        # so the skip consumes nothing.
        try:
            boundary = _resolve_since_boundary(
                last_run, silent_skip_window_hours=settings.silent_skip_window_hours
            )
        except Exception as exc:
            print(
                f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: "
                f"boundary resolution failed ({safe_exc_text(exc)}), skipping "
                "this persona (watermark untouched, retried next tick)"
            )
            if once:
                break
            continue
        notes_since = boundary.isoformat()

        # Both counters receive the ALREADY-RESOLVED boundary (notes_since),
        # never the raw last_run stamp. Each counter still calls
        # _resolve_since_boundary internally, but a fully-resolved ISO string
        # always parses cleanly — so both calls return the exact same
        # instant instead of each independently falling back to its own
        # datetime.now() and drifting apart on a cold start or corrupt stamp.
        row_count = _count_attributed_rows_since(
            persona_name,
            notes_since,
            install_db,
            silent_skip_window_hours=settings.silent_skip_window_hours,
        )
        note_count = _count_fresh_notes_since(
            persona_name,
            notes_since,
            profile_root,
            silent_skip_window_hours=settings.silent_skip_window_hours,
        )
        if row_count == 0 and note_count == 0:
            if last_run and normalize_physical_timestamp(last_run) is None:
                boundary_desc = (
                    f"corrupted stamp '{last_run}', fell back to "
                    f"{settings.silent_skip_window_hours}h window"
                )
            else:
                boundary_desc = f"{last_run or 'never'}"
            print(
                f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: "
                f"PERSONA_REFLECT_SILENT (0 new rows, 0 fresh notes since "
                f"{boundary_desc})"
            )
            if once:
                break
            continue

        print(
            f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: START "
            f"({row_count} attributed rows, {note_count} fresh notes)"
        )

        if test_mode:
            # Dry run: report what WOULD happen but never advance the
            # production watermark. Writing last_run here would make a
            # subsequent REAL tick treat this test timestamp as the last
            # real run — the notes/rows this dry run "saw" would then be
            # silently skipped forever once a real tick finally runs.
            print(
                f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: "
                f"--test mode, skipping spawn ({row_count} attributed rows, "
                f"{note_count} fresh notes, last_run NOT advanced)"
            )
            if once:
                break
            continue

        # The scan's UPPER bound, captured BEFORE the child starts.
        #
        # Stamping the boundary with the COMPLETION time silently drops every
        # note written WHILE the reflection was running: the child enumerates at
        # 10:05, the persona appends to today's market note at 10:06, the child
        # exits at 10:10, and the parent stores 10:10 — so the next tick asks
        # `mtime 10:06 > last_run 10:10`, gets False, and that append is
        # invisible forever. Stamping the pre-spawn instant can only ever
        # RE-offer a note that was already distilled, and the ledger's dedupe
        # key drops an identical re-proposal. Re-processing is recoverable; a
        # lost note is not.
        scan_upper_bound = datetime.now(timezone.utc).isoformat()

        success, message = _spawn_persona_pipeline(
            persona_name,
            profile_root,
            test_mode=test_mode,
            notes_since=notes_since,
        )

        # `last_attempt` always advances (it throttles the retry); `last_run`
        # — the freshness BOUNDARY both counters and the child's --notes-since
        # resolve from — advances ONLY on success, and only to the pre-spawn
        # upper bound above.
        #
        # The failure this closes: the child used to swallow every reasoning
        # failure (kill switch, provider outage) and exit 0, and this line
        # stamped the boundary regardless. One kill-switched night moved the
        # boundary past a persona's fresh notes; their mtimes were then older
        # than the watermark, so they were never fresh again and the lessons in
        # them were lost permanently. Leaving the boundary put means the next
        # tick after recovery counts exactly the same notes and retries them.
        state["last_attempt"] = datetime.now(timezone.utc).isoformat()
        if success:
            state["last_run"] = scan_upper_bound
        state["result"] = "success" if success else "failed"
        state["rows_found"] = row_count
        state["notes_found"] = note_count
        state["message"] = message
        save_state(state, state_file)

        if success:
            print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: SUCCESS")
        else:
            print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: FAILED — {message}")

        if once:
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Persona Learning Tick")
    parser.add_argument("--test", action="store_true", help="Dry run")
    parser.add_argument("--once", action="store_true", help="Process first eligible persona only")
    args = parser.parse_args()
    run_tick(test_mode=args.test, once=args.once)
