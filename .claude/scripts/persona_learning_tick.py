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
from dataclasses import dataclass
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
from shared import file_lock, load_state, safe_exc_text, save_state  # noqa: E402

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


_FAILURE_TAIL_LINES = 6
_FAILURE_TAIL_MAX_CHARS = 900

# A scheduled slot that lands within this many hours of the interval counts as
# due. The throttle stamp is taken during the tick, seconds to minutes after
# the scheduler fired it, so a 12h guard on a 12h cadence compared 11.9x h
# against 12.0 h and skipped — every slot. Observed 2026-09-02: crypto failed
# at 09:31, the 21:30 slot logged "recency guard (11.97h < 12.0h), skipping",
# and the retry landed at 09:30 the next day. A failed persona was degraded to
# one retry per day.
RECENCY_GUARD_JITTER_HOURS = 0.25

# How long a second tick waits for the process-wide lock before giving up.
# A child runs for minutes, so waiting longer never helps; the loser exits
# without spawning and the next scheduled slot retries.
TICK_LOCK_TIMEOUT_SECONDS = 2.0


def _attempt_sort_key(persona_name: str) -> str:
    """Oldest attempt first; never-attempted and unreadable stamps first of all."""
    try:
        state = load_state(_persona_state_file(persona_name))
        return str(state.get("last_attempt") or state.get("last_run") or "")
    except Exception:
        return ""


def _as_text(value: object) -> str:
    """``TimeoutExpired`` carries whatever was captured: str, bytes, or None."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value or ""


def _child_failure_tail(stdout: str | None, stderr: str | None) -> str:
    """The child's own explanation of a non-zero exit, for the receipt.

    ``memory_reflect.py`` prints every reason it exits 1 on STDOUT — the
    fail-honest notes leg ("Work-note distillation did not complete"), the
    distillation call's own error line, the kill-switch refusal — and its
    stderr is empty on all of them. Keeping only a stderr tail produced the
    receipt ``exit 1: `` for every failed persona: the cause was captured and
    thrown away, and the operator had to re-run the child by hand to read it.
    The last few non-empty stdout lines carry the cause; a stderr tail is
    appended when one exists (a traceback still lands there).
    """
    parts: list[str] = []
    out_lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    if out_lines:
        parts.append("stdout: " + " | ".join(out_lines[-_FAILURE_TAIL_LINES:]))
    err = (stderr or "").strip()
    if err:
        parts.append("stderr: " + err[-500:])
    if not parts:
        return "(child produced no output)"
    return " ; ".join(parts)[-_FAILURE_TAIL_MAX_CHARS:]


def _spawn_persona_pipeline(
    persona_name: str,
    profile_root: Path,
    *,
    test_mode: bool = False,
    notes_since: str | None = None,
    timeout_seconds: float | None = None,
) -> tuple[bool, str]:
    """Spawn memory_reflect.py -p <persona> as a subprocess.

    ``timeout_seconds`` is ``PERSONA_LEARNING_TIMEOUT`` (Rule 1: resolved at
    call time when not passed). It used to be a hard-coded 300 — which killed
    crypto's first-ever successful distillation on 2026-09-02 while the
    contradiction judge was still running, and threw away the child's output.

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

    if timeout_seconds is None:
        timeout_seconds = get_persona_learning_settings().timeout_seconds

    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(_SCRIPTS_DIR),
        )
        if result.returncode == 0:
            return True, "success"
        return False, (
            f"exit {result.returncode}: "
            f"{_child_failure_tail(result.stdout, result.stderr)}"
        )
    except subprocess.TimeoutExpired as exc:
        # The partial output is the only evidence of how far the child got.
        return False, (
            f"timeout ({timeout_seconds:.0f}s): "
            f"{_child_failure_tail(_as_text(exc.stdout), _as_text(exc.stderr))}"
        )
    except Exception as exc:
        return False, f"spawn error: {exc}"


@dataclass(frozen=True)
class TickOutcome:
    """What one tick did, so the entrypoint can exit honestly.

    ``failed`` names every persona whose spawned child did not succeed this
    tick. The parent used to swallow these: it printed ``FAILED`` into its own
    log, stamped the receipt, and exited 0 — so Task Scheduler showed a green
    ``Last Result: 0`` for a night on which a persona learned nothing.
    """

    spawned: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def run_tick(*, test_mode: bool = False, once: bool = False) -> TickOutcome:
    """Main tick: enumerate learning-enabled personas, spawn pipelines.

    One tick at a time. The scheduled task is IgnoreNew, but a manual run
    beside it used to pass the same recency guard and spawn the same persona
    twice — two model calls, two reflections mutating one profile, one receipt
    overwriting the other. The lock is the process-wide lease; the pre-spawn
    claim inside ``_run_tick_locked`` is the per-persona one.
    """
    try:
        with file_lock(
            STATE_DIR / "persona-learning-tick.json", timeout=TICK_LOCK_TIMEOUT_SECONDS
        ):
            outcome = _run_tick_locked(test_mode=test_mode, once=once)
        try:
            from personas.learning import worker as learning_worker
            learning_worker.run_pending_profiles(test_mode=test_mode, once=once)
        except Exception as exc:
            print(f"[{_now_iso()}] Learning queue wake failed (non-blocking): {safe_exc_text(exc)}")
        return outcome
    except TimeoutError:
        print(
            f"[{_now_iso()}] PERSONA_LEARNING_TICK: another tick holds the lock; "
            "exiting without spawning (nothing consumed, retried next slot)"
        )
        return TickOutcome()


def _run_tick_locked(*, test_mode: bool, once: bool) -> TickOutcome:
    settings = get_persona_learning_settings()
    tick_started = datetime.now(timezone.utc).isoformat()
    if not settings.enabled:
        print(f"[{_now_iso()}] [persona-learning] disabled via PERSONA_LEARNING_ENABLED")
        return TickOutcome()

    if not is_active_default_profile():
        print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: must run under default profile, skipping")
        return TickOutcome()

    install_db = get_default_paths()["data"] / "chat.db"

    profiles = list_profiles()
    named_profiles = [p for p in profiles if not p.is_default]

    if not named_profiles:
        print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: no named profiles found, exiting")
        return TickOutcome()

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
        return TickOutcome()

    print(f"[{_now_iso()}] PERSONA_LEARNING_TICK: {len(eligible)} learning-enabled persona(s)")

    # Overdue-first. Every attempted persona is stamped with the same tick
    # start, so a roster whose head keeps timing out inside the task's
    # one-hour execution limit would re-run that same head every slot and
    # never reach the tail. Sorting by the oldest attempt rotates the tail
    # forward; a never-attempted persona (or one whose stamp cannot be read)
    # sorts first, so it is retried rather than skipped.
    eligible.sort(key=lambda item: _attempt_sort_key(item[0]))

    spawned: list[str] = []
    failed: list[str] = []
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
                if hours_since < settings.tick_interval_hours - RECENCY_GUARD_JITTER_HOURS:
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

        # Claim the attempt BEFORE the child starts. A second tick that reads
        # this receipt while the child is running sees the attempt and skips,
        # instead of spawning the same persona twice. Stamped with the TICK's
        # start, not this persona's spawn instant: the throttle measures slot
        # to slot, and a stamp taken minutes into the roster made the next
        # 12h slot read 11.9x h and skip (see RECENCY_GUARD_JITTER_HOURS).
        state["last_attempt"] = tick_started
        save_state(state, state_file)

        success, message = _spawn_persona_pipeline(
            persona_name,
            profile_root,
            test_mode=test_mode,
            notes_since=notes_since,
            timeout_seconds=settings.timeout_seconds,
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
        if success:
            state["last_run"] = scan_upper_bound
        state["result"] = "success" if success else "failed"
        state["rows_found"] = row_count
        state["notes_found"] = note_count
        state["message"] = message
        save_state(state, state_file)
        spawned.append(persona_name)
        if not success:
            failed.append(persona_name)

        if success:
            print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: SUCCESS")
        else:
            print(f"[{_now_iso()}] PERSONA_LEARNING_TICK [{persona_name}]: FAILED — {message}")

        if once:
            break

    if failed:
        print(
            f"[{_now_iso()}] PERSONA_LEARNING_TICK: {len(failed)} persona(s) FAILED "
            f"this tick: {', '.join(failed)}"
        )
    return TickOutcome(spawned=tuple(spawned), failed=tuple(failed))


def main(argv: list[str] | None = None) -> int:
    """Entrypoint. Non-zero when any persona's child failed this tick.

    The wrappers (``run_persona_learning.bat`` / ``.sh``) forward this code to
    Task Scheduler, so a failed persona is a red ``Last Result`` instead of a
    line buried in the tick's own log.
    """
    parser = argparse.ArgumentParser(description="Persona Learning Tick")
    parser.add_argument("--test", action="store_true", help="Dry run")
    parser.add_argument("--once", action="store_true", help="Process first eligible persona only")
    args = parser.parse_args(argv)
    outcome = run_tick(test_mode=args.test, once=args.once)
    return 1 if outcome.failed else 0


if __name__ == "__main__":
    sys.exit(main())
