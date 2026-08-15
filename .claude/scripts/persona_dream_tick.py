"""
Persona Dream Tick — nightly fan-out of the FULL dream cycle, one per persona.

The equality doctrine made real: a persona is not a sub-homie, so it gets the
same 5-phase dream (Orient / Gather / Consolidate / Prune / Belief-Evolve) the
main homie gets, scoped to its own vault.

NO dream internals are ported. ``memory_dream.py`` already carries the
``apply_persona_override()`` boot-shim ABOVE its config import, so every path
constant it uses (MEMORY_DIR, STATE_DIR, DREAM_STATE_FILE, SELF_FILE,
AMENDMENT_LEDGER_FILE, BELIEF_EVOLVE_DECISION_DIR) re-roots into the profile
tree under ``-p <name>``. This module is the fan-out and the receipt, nothing
more.

CRITICAL: config.py binds paths at import time. The tick itself runs as the
DEFAULT profile and NEVER loops profiles in-process — each persona's dream runs
as a subprocess with HOMIE_HOME set by build_capability_scoped_env plus an
explicit ``-p <name>`` (rank-1 selection, which also strips the flag before the
child's argparse sees it).

Sibling of ``persona_learning_tick.py``, deliberately NOT an extension of it
(architecture Q5): the cadences differ (nightly dream vs 12h-interval reflect)
and coupling them in one scheduler entry makes both harder to reason about.

Two deliberate divergences from the learning tick, both doctrine-driven:

  1. NO per-persona ``learning.enabled`` filter. "I want the full dream cycle on
     everybody… there shouldn't be no off button." Every named profile is
     enumerated; the only switch is the framework-wide PERSONA_DREAM_ENABLED
     fire-extinguisher. This is affordable because the child's own DREAM_SILENT
     fast path means a persona with no fresh signal costs ZERO LLM calls — the
     nightly bill is bounded by signal, not by roster size.
  2. The parent reads the child's dream-state.json back off DISK (Rule 2) and
     CLASSIFIES what it finds against ONE table — ``RECEIPT_CONTRACT`` below.
     An assumed success is not a receipt, and neither is a file that merely
     exists: a receipt counts only when it carries the nonce this spawn handed
     the child. Every other shape (missing / unreadable / unrecognised /
     future-dated / stale / child-failed / kill-switched) has its own row, and
     each row states all three consequences — what gets stamped, whether the
     persona's recency budget is spent, and whether the scheduler is told.

Exit code: non-zero when any persona's dream FAILED — a failed spawn, a child
that recorded ``failed``, a receipt that cannot be trusted, a refusal, or the
parent's own stamp I/O failing. Silent skips (recency guard, kill switch, a
child that skipped on its own guard) are not failures. The scheduler wrappers
key their FAILED branch off this. One persona's failure is contained to that
persona: the roster keeps moving and the exit code carries the news.

Usage:
    uv run python persona_dream_tick.py               # Nightly fan-out
    uv run python persona_dream_tick.py --test        # Dry run (no spawn)
    uv run python persona_dream_tick.py --child-test  # Real spawn, child --test --no-llm
    uv run python persona_dream_tick.py --once        # First eligible persona only
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from uuid import uuid4

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override

apply_persona_override()

from config import (  # noqa: E402
    DREAM_STATE_FILE,
    STATE_DIR,
    get_persona_dream_settings,
)
from personas import get_persona_paths  # noqa: E402
from personas.capabilities import build_capability_scoped_env  # noqa: E402
from personas.lifecycle import list_profiles  # noqa: E402
from personas.services import is_active_default_profile  # noqa: E402
from shared import load_state, save_state  # noqa: E402

# Inject .claude/chat for the canonical timestamp normalizer.
_CHAT_DIR = Path(__file__).resolve().parent.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from cognition.proactive_brief import normalize_physical_timestamp  # noqa: E402

_SCRIPTS_DIR = Path(__file__).resolve().parent

DREAM_STATE_FILENAME = "dream-state.json"

# =============================================================================
# THE RECEIPT CONTRACT — one table, three consequences
# =============================================================================
# Every way a persona's turn can end resolves to exactly ONE status below, and
# the status alone decides all three downstream answers: what the parent stamps,
# whether the persona's recency budget is spent, and whether the scheduler hears
# about it. Nothing downstream re-derives those from the status string.
#
# That single-table shape is the point. Rounds 1-3 each fixed an individual path
# that answered one of the three questions differently from the doctrine — a
# present-but-stale file called "success", a probe consuming the night's budget,
# a caught failure exiting 0. Those were the same bug wearing different clothes:
# the answer lived at the call site, so every new path got to invent its own. A
# new status now cannot ship without stating all three answers here, and a path
# that forgets to classify itself raises rather than defaulting to success.
#
# The BUDGET column is the load-bearing one. ``last_run`` is the field the
# parent's recency guard reads, so writing it is what SPENDS this persona's
# night — the tick will not come back for ~20h. It is written if and only if
# THIS spawn produced a trustworthy receipt saying the child actually ran.
# Everything else records ``last_attempt`` instead (full evidence, no budget)
# and is retried on the next tick. A persona must never lose its dream to a
# receipt nobody can vouch for.

# --- statuses: what the physical evidence established -----------------------
STATUS_CONSOLIDATED = "consolidated"   # child dreamed and said so
STATUS_SILENT = "silent"               # honest "nothing to consolidate"
STATUS_NO_LOGS = "no_logs"             # honest "nothing to scan"
STATUS_KILLSWITCH = "killswitch"       # child refused: operator intent
STATUS_CHILD_FAILED = "child_failed"   # child recorded its own failure
STATUS_SPAWN_FAILED = "spawn_failed"   # nonzero exit / timeout / env build
STATUS_MISSING = "missing"             # clean exit, no state file at all
STATUS_UNREADABLE = "unreadable"       # present but not parseable JSON
STATUS_INVALID = "invalid"             # parsed, but nothing believable in it
STATUS_FUTURE = "future_dated"         # last_run in the future — corrupt clock
STATUS_STALE = "stale"                 # a real receipt, but not from THIS spawn
STATUS_COLLISION = "refused_collision"  # would have clobbered the main state
STATUS_PATH_ERROR = "state_path_error"  # could not even resolve the address
STATUS_STAMP_ERROR = "stamp_io_error"  # the parent's own bookkeeping failed


class Outcome(NamedTuple):
    """The three answers a status owes. See RECEIPT_CONTRACT."""

    result: str            # value stamped into the parent's per-persona file
    consumes_budget: bool  # writes last_run — spends this persona's night
    is_failure: bool       # counts toward the tick's non-zero exit code
    summary: str           # operator-facing one-liner


RECEIPT_CONTRACT: dict[str, Outcome] = {
    # --- the child ran and told us so: budget spent, nobody paged ------------
    STATUS_CONSOLIDATED: Outcome(
        "success", True, False, "dreamed and consolidated"
    ),
    STATUS_SILENT: Outcome(
        "success_silent", True, False, "ran; no signal worth consolidating"
    ),
    STATUS_NO_LOGS: Outcome(
        "success_no_logs", True, False, "ran; no daily logs in the window"
    ),
    STATUS_KILLSWITCH: Outcome(
        "skipped_killswitch", True, False, "child refused — operator kill switch"
    ),
    # --- something broke: no budget spent, scheduler told ---------------------
    STATUS_SPAWN_FAILED: Outcome(
        "failed", False, True, "spawn failed"
    ),
    STATUS_CHILD_FAILED: Outcome(
        "child_failed", False, True, "child recorded result=failed"
    ),
    STATUS_MISSING: Outcome(
        "no_receipt", False, True, "clean exit but left no receipt"
    ),
    STATUS_UNREADABLE: Outcome(
        "invalid_receipt", False, True, "receipt is not readable JSON"
    ),
    STATUS_INVALID: Outcome(
        "invalid_receipt", False, True, "receipt has no believable result"
    ),
    STATUS_FUTURE: Outcome(
        "corrupt_receipt", False, True, "receipt is dated in the future"
    ),
    STATUS_COLLISION: Outcome(
        "refused_state_collision", False, True, "would collide with main state"
    ),
    STATUS_PATH_ERROR: Outcome(
        "state_path_error", False, True, "child state path did not resolve"
    ),
    STATUS_STAMP_ERROR: Outcome(
        "stamp_io_error", False, True, "parent could not read/write its stamp"
    ),
    # --- the child legitimately declined to run: no budget, not a fault ------
    # Its own recency guard or dream-state lock said no. Not spending the
    # parent's budget is what lets the next tick pick it straight back up.
    STATUS_STALE: Outcome(
        "stale_receipt", False, False, "child skipped on its own guard/lock"
    ),
}

# What memory_dream can legitimately write in ``result``, and the status each
# maps to. Single source: anything absent here is a receipt this tick refuses
# to interpret (STATUS_INVALID) rather than guess about.
CHILD_RESULT_STATUS: dict[str, str] = {
    "consolidated": STATUS_CONSOLIDATED,
    "DREAM_SILENT": STATUS_SILENT,
    "DREAM_SKIPPED": STATUS_NO_LOGS,
    "failed": STATUS_CHILD_FAILED,
    "skipped_killswitch": STATUS_KILLSWITCH,
}

# Env var carrying the parent's per-spawn nonce to the child, which echoes it
# into its own dream-state.json. This is the CHANGE PROOF: a receipt is from
# this spawn iff it carries this spawn's id. Timestamps cannot establish that —
# a clock can roll backwards or forwards, and an untouched file keeps whatever
# it already said. Set after the capability scrub so the matrix cannot drop it.
SPAWN_ID_ENV = "HOMIE_DREAM_SPAWN_ID"

# Tolerance for a receipt dated slightly ahead of the parent's clock (the child
# stamps naive-local from its own process). Beyond it, the future is corrupt.
_RECEIPT_CLOCK_SLACK = timedelta(seconds=2)


class TickOutcome(NamedTuple):
    """What the fan-out physically did. The scheduler's exit code comes from here.

    A tick that catches every per-persona failure so the roster keeps moving
    still has to TELL the scheduler that failures happened — otherwise
    "all 28 quiet" and "all 28 broken" are the same exit 0.
    """

    attempted: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    truncated: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(persona_name: str | None, message: str) -> None:
    tag = f" [{persona_name}]" if persona_name else ""
    print(f"[{_now_iso()}] PERSONA_DREAM_TICK{tag}: {message}")


def _persona_state_file(persona_name: str) -> Path:
    """Fan-out stamp for one persona — lives in the MAIN STATE_DIR.

    The parent's bookkeeping (last attempt, spawn outcome, read-back receipt)
    is the DEFAULT profile's business, exactly like the learning tick's stamp.
    The persona's OWN dream-state.json is a different file in a different tree
    (see ``_child_dream_state_file``) and the two must never collide.
    """
    return STATE_DIR / f"persona-dream-{persona_name}-state.json"


def _child_dream_state_file(persona_name: str) -> Path:
    """Where the CHILD's dream cycle will write its dream-state.json.

    Resolved through the canonical persona path resolver, which is the same
    function the child's own ``config.py`` calls after the boot-shim maps
    ``-p <name>`` to a profile root. This is the address the read-back checks.
    """
    return get_persona_paths(persona_name)["state"] / DREAM_STATE_FILENAME


def _hours_since(stamp: str | None) -> float | None:
    """Hours between ``stamp`` and now, or None when it can't be read.

    Both sides go through the canonical ``normalize_physical_timestamp`` owner
    (naive local) before subtraction — stamps are written aware-UTC while other
    physical timestamps in the framework are naive-local, and comparing those
    raw is wrong in both directions depending on the box's UTC offset.
    """
    stamp_dt = normalize_physical_timestamp(stamp)
    if stamp_dt is None:
        return None
    now_dt = normalize_physical_timestamp(datetime.now(timezone.utc))
    if now_dt is None:  # pragma: no cover - normalizer never fails on a datetime
        return None
    return (now_dt - stamp_dt).total_seconds() / 3600.0


def _new_spawn_id() -> str:
    """A nonce identifying ONE spawn. Seam so tests can pin it."""
    return uuid4().hex


def _read_persona_stamp(state_file: Path) -> tuple[dict[str, object], str | None]:
    """Read one persona's fan-out stamp, containing every failure to THAT persona.

    ``load_state`` only catches ``JSONDecodeError``; a permission-denied file, a
    directory where a file belongs, or any other ``OSError`` escapes it. This
    ran inside the roster's sort key, so one persona's broken stamp took every
    other persona's dream down with it before the loop even started — the exact
    opposite of the serial containment the fan-out promises.
    """
    try:
        return load_state(state_file), None
    except Exception as exc:  # noqa: BLE001 — one bad stamp is one bad persona
        return {}, f"{type(exc).__name__}: {exc}"


def _write_persona_stamp(state: dict[str, object], state_file: Path) -> str | None:
    """Write one persona's fan-out stamp. Returns an error string, never raises."""
    try:
        save_state(state, state_file)
        return None
    except Exception as exc:  # noqa: BLE001 — see _read_persona_stamp
        return f"{type(exc).__name__}: {exc}"


def read_child_dream_receipt(
    persona_name: str,
    *,
    spawn_id: str,
) -> dict[str, object]:
    """Read the persona's own dream-state.json and classify it (Rule 2).

    Exit code proves the process ended well. Presence proves some dream ran
    once. Neither proves THIS spawn produced this file, and that is the only
    question the parent actually needs answered before it spends the persona's
    night. So the receipt is trusted iff it carries ``spawn_id`` — the nonce
    this parent handed this child through the environment moments ago.

    Why a nonce and not a timestamp window: a timestamp can only ever say the
    file *claims* a recent moment. An untouched file keeps whatever it already
    said, and a future-dated one (clock rollback, a bad manual repair) sails
    past any lower bound forever — which is precisely how a 2099 receipt got
    laundered into "fresh" and suppressed a persona's dream indefinitely while
    every nightly run reported success. The nonce cannot be forged by a file
    that nobody wrote during this spawn.

    The timestamp is still read, for the one thing it is good for: a receipt
    dated in the future is evidence of a broken clock, so it is CORRUPT even
    when the nonce matches — never fresh.

    Returns a receipt dict always carrying ``path``, ``present`` and ``status``
    (a key of ``RECEIPT_CONTRACT``). Fail-closed: any read error classifies as
    unreadable, never raises, and never lands on a status that spends budget.
    """
    receipt: dict[str, object] = {
        "path": "",
        "present": False,
        "status": STATUS_MISSING,
    }
    try:
        state_file = _child_dream_state_file(persona_name)
        receipt["path"] = str(state_file)
        if not state_file.exists():
            return receipt
        receipt["present"] = True

        # Parsed here rather than through load_state(), which fail-opens a
        # corrupt file to {} — that would launder "unreadable" into "empty".
        try:
            child_state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            receipt["status"] = STATUS_UNREADABLE
            receipt["error"] = f"unreadable child state: {exc}"
            return receipt
        if not isinstance(child_state, dict):
            receipt["status"] = STATUS_UNREADABLE
            receipt["error"] = "child state is not a JSON object"
            return receipt

        result = str(child_state.get("result", ""))
        last_run = child_state.get("last_run", "")
        receipt["result"] = result
        receipt["last_run"] = last_run
        receipt["spawn_id"] = child_state.get("spawn_id", "")
        receipt["phases_completed"] = child_state.get("phases_completed", [])
        belief = child_state.get("belief_evolve")
        if isinstance(belief, dict):
            receipt["belief_evolve_result"] = belief.get("result", "")
            receipt["belief_adopted"] = belief.get("adopted", 0)
            receipt["belief_rejected"] = belief.get("rejected", 0)

        if result not in CHILD_RESULT_STATUS:
            receipt["status"] = STATUS_INVALID
            receipt["error"] = f"unrecognised child result: {result!r}"
            return receipt

        last_run_dt = normalize_physical_timestamp(last_run)
        if last_run_dt is None:
            receipt["status"] = STATUS_INVALID
            receipt["error"] = f"unusable child last_run: {last_run!r}"
            return receipt

        now = normalize_physical_timestamp(datetime.now(timezone.utc)) or datetime.now()
        if last_run_dt > now + _RECEIPT_CLOCK_SLACK:
            receipt["status"] = STATUS_FUTURE
            receipt["error"] = f"child last_run is in the future: {last_run!r}"
            return receipt

        wrote_this_spawn = bool(spawn_id) and receipt["spawn_id"] == spawn_id
        receipt["wrote_this_spawn"] = wrote_this_spawn
        if not wrote_this_spawn:
            receipt["status"] = STATUS_STALE
            return receipt

        receipt["status"] = CHILD_RESULT_STATUS[result]
    except Exception as exc:  # noqa: BLE001 — a receipt read never fails a tick
        receipt["status"] = STATUS_UNREADABLE
        receipt["error"] = f"read-back error: {exc}"
    return receipt


def _spawn_persona_dream(
    persona_name: str,
    profile_root: Path,
    *,
    days: int,
    timeout_seconds: float,
    child_test: bool = False,
    spawn_id: str = "",
) -> tuple[bool, str]:
    """Spawn ``memory_dream.py -p <persona>`` as a subprocess.

    Returns (success, message). ``child_test`` forwards ``--test --no-llm`` to
    the child. ``--test`` is side-effect-free by contract (``memory_dream.py``
    usage doc): no state file, no daily-log line, no decision artifact, no
    lock. ``--no-llm`` makes it free as well as harmless — a probe fans out
    across the WHOLE roster, and a plain ``--test`` still calls the LLM twice
    per signal-bearing persona, so proving the plumbing would cost a real
    night's tokens. Together they mean a child test leaves NO receipt to read
    back, so the caller records it under ``last_test_*`` only.
    """
    try:
        env = build_capability_scoped_env(persona_name, profile_root=profile_root)
    except Exception as exc:
        return False, f"env build failed: {exc}"

    # Set AFTER the capability scrub, deliberately: the nonce is the parent's
    # own change-proof channel, not a delegated capability, so no matrix entry
    # can drop it and no profile can supply its own.
    if spawn_id:
        env[SPAWN_ID_ENV] = spawn_id

    cmd = [
        sys.executable,
        str(_SCRIPTS_DIR / "memory_dream.py"),
        "-p", persona_name,
        "--days", str(days),
    ]
    if child_test:
        cmd.extend(["--test", "--no-llm"])

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
        stderr_tail = (result.stderr or "")[-500:]
        return False, f"exit {result.returncode}: {stderr_tail}"
    except subprocess.TimeoutExpired:
        return False, f"timeout ({timeout_seconds:.0f}s)"
    except Exception as exc:
        return False, f"spawn error: {exc}"


def _stamp_outcome(
    persona_name: str,
    state: dict[str, object],
    state_file: Path,
    status: str,
    *,
    message: str = "",
    receipt: dict[str, object] | None = None,
) -> bool:
    """Apply the contract row for ``status``. Returns True iff this counts failed.

    The ONLY place a per-persona result is written, so the three answers can
    never disagree across paths. ``last_attempt`` always records that the tick
    got here; ``last_run`` — the field the recency guard reads — is written
    only when the contract says this outcome earned the budget.
    """
    outcome = RECEIPT_CONTRACT[status]
    now = datetime.now(timezone.utc).isoformat()

    state["last_attempt"] = now
    state["status"] = status
    state["result"] = outcome.result
    state["message"] = message
    state["child_test"] = False
    if receipt is not None:
        state["dream_state"] = receipt
    if outcome.consumes_budget:
        state["last_run"] = now

    write_error = _write_persona_stamp(state, state_file)
    if write_error is not None:
        # The outcome happened; we just cannot record it. Report the I/O
        # failure rather than the outcome — an unrecorded success would be
        # re-run tomorrow anyway, and silence here is what hides a full disk.
        _log(
            persona_name,
            f"FAILED ({RECEIPT_CONTRACT[STATUS_STAMP_ERROR].result}) — "
            f"could not write fan-out stamp: {write_error}",
        )
        return True

    detail = f" | {message}" if message else ""
    if receipt is not None:
        detail += (
            f" | child: {receipt.get('result', 'MISSING')}"
            f" ({receipt.get('error') or receipt.get('status')})"
        )
    budget = "budget spent" if outcome.consumes_budget else "budget kept — retries next tick"
    level = "FAILED" if outcome.is_failure else "OK"
    _log(
        persona_name,
        f"{level} [{status}] {outcome.summary} — {budget}{detail}",
    )
    return outcome.is_failure


def run_tick(
    *,
    test_mode: bool = False,
    child_test: bool = False,
    once: bool = False,
) -> TickOutcome:
    """Main tick: enumerate every named persona, spawn its full dream cycle.

    Returns a ``TickOutcome`` whose ``exit_code`` the entrypoint exits with, so
    a night where every persona failed is distinguishable from a quiet one.
    """
    settings = get_persona_dream_settings()
    if not settings.enabled:
        _log(None, "disabled via PERSONA_DREAM_ENABLED")
        return TickOutcome()

    if not is_active_default_profile():
        _log(None, "must run under default profile, skipping")
        return TickOutcome()

    named_profiles = [p for p in list_profiles() if not p.is_default]
    if not named_profiles:
        _log(None, "no named profiles found, exiting")
        return TickOutcome()

    # Read every fan-out stamp ONCE, up front, with each read's failure
    # contained to its own persona. This used to happen inside the sort key,
    # where an unreadable stamp raised before the loop existed and cost the
    # whole roster its night.
    stamps: dict[str, dict[str, object]] = {}
    stamp_errors: dict[str, str] = {}
    for profile in named_profiles:
        stamp, error = _read_persona_stamp(_persona_state_file(profile.name))
        stamps[profile.name] = stamp
        if error is not None:
            stamp_errors[profile.name] = error

    # Oldest-attempted first. Under a wall-clock budget a fixed alphabetical
    # order would starve the same tail every single night; ordering by the
    # previous attempt makes the skipped set rotate instead.
    def _sort_key(profile: object) -> datetime:
        try:
            stamp = stamps.get(profile.name, {}).get("last_run")
            return normalize_physical_timestamp(stamp) or datetime.min
        except Exception:  # noqa: BLE001 — a junk stamp sorts first, never raises
            return datetime.min

    named_profiles.sort(key=_sort_key)

    _log(
        None,
        f"{len(named_profiles)} named persona(s) — full dream cycle each "
        f"(days={settings.days}, DREAM_SILENT gates all LLM spend)",
    )

    started = time.monotonic()
    attempted: list[str] = []
    failed: list[str] = []
    truncated: list[str] = []
    for index, profile in enumerate(named_profiles):
        persona_name = profile.name

        elapsed = time.monotonic() - started
        if (
            settings.max_wall_clock_seconds > 0
            and elapsed >= settings.max_wall_clock_seconds
            and not test_mode
        ):
            # The default is unlimited precisely so the nightly roster always
            # completes; reaching here means an operator opted into a cap, and
            # a cap that truncates the roster must SAY SO, loudly and by name.
            truncated = [p.name for p in named_profiles[index:]]
            _log(
                None,
                f"WARNING: operator wall-clock cap "
                f"(PERSONA_DREAM_MAX_WALL_CLOCK={settings.max_wall_clock_seconds:.0f}s) "
                f"exhausted after {elapsed:.0f}s — {len(truncated)} persona(s) got NO "
                f"dream this run (retried oldest-first tomorrow): "
                f"{', '.join(truncated)}",
            )
            break

        state_file = _persona_state_file(persona_name)

        # A stamp we cannot read is contained here: this persona is skipped and
        # counted failed, and every persona after it still gets its dream. We
        # do NOT spawn on an unreadable stamp — without it there is no way to
        # know whether this persona already ran tonight, and no way to record
        # the result afterwards.
        if persona_name in stamp_errors:
            _log(
                persona_name,
                f"FAILED [{STATUS_STAMP_ERROR}] — fan-out stamp unreadable "
                f"({stamp_errors[persona_name]}); skipping this persona only",
            )
            failed.append(persona_name)
            if once:
                break
            continue

        state = stamps.get(persona_name, {})
        last_run = state.get("last_run")

        # Recency guard. Fail-open — an absent or unparseable stamp (hours_since
        # is None) never blocks a run.
        if settings.tick_interval_hours > 0:
            hours_since = _hours_since(last_run)
            if hours_since is not None and hours_since < settings.tick_interval_hours:
                _log(
                    persona_name,
                    f"recency guard ({hours_since:.1f}h < "
                    f"{settings.tick_interval_hours}h), skipping",
                )
                if once:
                    break
                continue

        # Shared-state collision guard. The whole design rests on the child's
        # dream-state.json living in the PROFILE tree; if this persona's state
        # file ever resolved onto the MAIN one, spawning would let a persona's
        # dream clobber the default profile's recency guard and belief receipt.
        # Physical path comparison, checked BEFORE the spawn (Rule 2).
        child_state_file = _child_dream_state_file(persona_name)
        try:
            collides = child_state_file.resolve() == DREAM_STATE_FILE.resolve()
        except (OSError, RuntimeError) as exc:
            if _stamp_outcome(
                persona_name, state, state_file, STATUS_PATH_ERROR, message=str(exc)
            ):
                failed.append(persona_name)
            if once:
                break
            continue
        if collides:
            if _stamp_outcome(
                persona_name,
                state,
                state_file,
                STATUS_COLLISION,
                message=(
                    f"REFUSING to spawn — child dream-state would collide with "
                    f"the main profile's at {child_state_file}"
                ),
            ):
                failed.append(persona_name)
            if once:
                break
            continue

        attempted.append(persona_name)

        if test_mode:
            _log(persona_name, "--test mode, skipping spawn")
            # Bookkeeping only — NEVER touch last_run/result, the SAME fields
            # the recency guard reads above. A --test preview run must not
            # suppress that night's REAL dream for tick_interval_hours; it
            # is not a completed run, just an operator proving the fan-out
            # would have picked this persona up.
            state["last_test_run"] = datetime.now(timezone.utc).isoformat()
            state["last_test_result"] = "test_skip"
            if _write_persona_stamp(state, state_file) is not None:
                failed.append(persona_name)
            if once:
                break
            continue

        _log(persona_name, f"START (child --test={child_test})")
        spawn_id = _new_spawn_id()
        success, message = _spawn_persona_dream(
            persona_name,
            profile.path,
            days=settings.days,
            timeout_seconds=settings.timeout_seconds,
            child_test=child_test,
            spawn_id=spawn_id,
        )

        if child_test:
            # A child --test writes NOTHING (no state, no daily log, no
            # artifact, no lock), so there is no receipt to read back and no
            # completed run to record. Bookkeeping lands in last_test_* for the
            # SAME reason --test's does: a probe an operator runs at noon must
            # never suppress that night's REAL dream via the recency guard,
            # which reads last_run/result and nothing else.
            state["last_test_run"] = datetime.now(timezone.utc).isoformat()
            state["last_test_result"] = (
                "child_test_success" if success else "child_test_failed"
            )
            state["last_test_message"] = message
            state["last_test_child_test"] = True
            if _write_persona_stamp(state, state_file) is not None or not success:
                failed.append(persona_name)
            if success:
                _log(
                    persona_name,
                    "CHILD-TEST OK — spawn clean; child wrote nothing, so no receipt",
                )
            else:
                _log(persona_name, f"CHILD-TEST FAILED — {message}")
            if once:
                break
            continue

        # Physical read-back — what the profile tree actually says (Rule 2).
        # Runs even on a failed spawn: a child that crashed in Phase 3 still
        # advanced its own state to result="failed", and that is exactly the
        # detail an operator needs.
        receipt = read_child_dream_receipt(persona_name, spawn_id=spawn_id)

        # A failed spawn is the strongest fact available — it outranks whatever
        # the (possibly leftover) state file says. Otherwise the receipt's own
        # status decides, and the contract turns that status into all three
        # answers at once.
        status = (
            STATUS_SPAWN_FAILED
            if not success
            else str(receipt.get("status", STATUS_MISSING))
        )
        if status not in RECEIPT_CONTRACT:  # pragma: no cover - defensive
            status = STATUS_INVALID
        if _stamp_outcome(
            persona_name, state, state_file, status, message=message, receipt=receipt
        ):
            failed.append(persona_name)

        if once:
            break

    if not test_mode and attempted:
        summary = (
            f"fan-out complete — {len(attempted)} persona(s) attempted, "
            f"{len(failed)} failed"
        )
        if truncated:
            summary += f", {len(truncated)} never attempted (operator wall-clock cap)"
        _log(None, summary)

    return TickOutcome(tuple(attempted), tuple(failed), tuple(truncated))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Persona Dream Tick")
    parser.add_argument("--test", action="store_true", help="Dry run (no spawn)")
    parser.add_argument(
        "--child-test",
        action="store_true",
        help="Really spawn, but pass --test --no-llm to each child dream "
        "(writes nothing, costs nothing)",
    )
    parser.add_argument(
        "--once", action="store_true", help="Process first eligible persona only"
    )
    args = parser.parse_args()
    outcome = run_tick(
        test_mode=args.test, child_test=args.child_test, once=args.once
    )
    # Non-zero on real failures only. The scheduler wrappers key their FAILED
    # branch off this: without it, a night where every child crashed exits 0
    # and reads exactly like a night where every persona was quiet.
    sys.exit(outcome.exit_code)
