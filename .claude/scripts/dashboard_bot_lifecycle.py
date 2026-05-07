"""Persona-bot PID lifecycle for the dashboard slice (PRD-8 Phase 3 / WS2).

owner Decision 1 + R3 NM1 — backs the donor Agents.tsx action buttons
(``activate`` / ``deactivate`` / ``restart``). Designed as a DELEGATION
module: this file contains ZERO process-management primitives of its
own. Every is-running check, pid read/write, and file lock routes
through ``shared.py`` (runtime-chat-owned helpers).

Slice ownership:
  * The CODEOWNERS glob ``.claude/scripts/dashboard_*.py`` covers this
    module so dashboard-owner gets review on every diff.
  * runtime-chat-owner ALSO gates this module (NM1 cross-owner sign-off)
    because it CONSUMES `shared.py` runtime helpers — any new shared.py
    primitive added by this module would be a slice violation.

Design discipline:
  * ``import shared`` (NOT ``from shared import X``) — module-attribute
    lookup so test monkeypatches of ``shared.is_pid_alive``,
    ``shared.read_pid``, ``shared.file_lock`` propagate. Same Rule 3
    pattern that runtime/registry.py uses for langfuse_setup.
  * Profile-aware paths via ``personas.services.get_bot_pid_path`` /
    ``get_bot_lock_path`` / ``get_log_dir`` — for the TARGET persona, NOT
    the dashboard's own. ``HOMIE_HOME`` save/restore around path
    resolution mirrors run_chat.sh:95-104 convention.
  * Subprocess env scrubbed via local ``_scrub_dashboard_env()`` — drops
    DASHBOARD_TOKEN/BIND/PORT/DB_PATH plus pattern-matched secret-shaped
    keys not on the bot-creds whitelist. Phase 7 prep — migrates to
    runtime/subprocess_env.py:get_scrubbed_sdk_env() when Phase 7 lands.

Anti-pattern compliance:
  * Rule 1: every public function uses ``*, grace_seconds: int | None = None``
    + ``if grace_seconds is None: grace_seconds = config.DASHBOARD_BOT_GRACE_SECONDS``
    body resolution. NO ``grace_seconds=config.X`` def-time bind.
  * Rule 2: every "is the bot running" check goes through
    ``shared.is_pid_alive(pid)`` — physical process state, NOT pid file
    presence alone.
  * Rule 3: N/A (no optional-provider SDK touched).

Public API:
  * ``activate(persona_id, *, grace_seconds=None) -> dict`` — start bot
  * ``deactivate(persona_id, *, grace_seconds=None) -> dict`` — stop bot
  * ``restart(persona_id, *, grace_seconds=None) -> dict`` — chain D→A
  * ``is_running(persona_id) -> bool`` — Rule 2 disk-state check
  * ``status(persona_id) -> dict`` — running/pid/uptime_s

What this module does NOT do:
  * Does NOT modify ``shared.py``. Read-only consumer.
  * Does NOT modify ``run_chat.sh``. Shell remains the operator CLI.
  * Does NOT add new SDK calls or new optional-provider integrations.
  * Does NOT bypass ``apply_persona_override()`` — the dashboard process
    runs ``apply_persona_override()`` once at startup; per-persona
    activate/deactivate uses an EXPLICIT ``persona_id`` argument that is
    bound into the SUBPROCESS environment via ``env["HOMIE_HOME"]``, not
    the dashboard's own ``HOMIE_HOME``.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import config

# Rule 3 module-attribute lookup pattern — `import shared` (NOT
# `from shared import ...`). Test monkeypatches of shared.is_pid_alive
# etc. propagate because this module references the helpers as
# `shared.X` at call time, not via cached function objects.
import shared

# Personas helpers — profile-aware path resolution for the TARGET persona.
# We import the services submodule directly so the dashboard can resolve a
# different persona's bot paths without touching the dashboard's own
# active profile.
from personas import services as _services
from personas.lifecycle import resolve_profile_root as _lifecycle_profile_root

__all__ = [
    "activate",
    "deactivate",
    "restart",
    "is_running",
    "status",
]


# ── Subprocess env scrubbing (Phase 7 seed) ──────────────────────────────
#
# When the dashboard spawns a persona's bot, the child MUST NOT inherit
# dashboard-only env vars (token, bind, port, db_path) — those are the
# operator's HTTP control plane, not bot config. Pattern-matched secret-
# shaped keys are also scrubbed unless they belong to the bot-creds
# whitelist (TELEGRAM_*, ANTHROPIC_*, OPENAI_*, etc.).
#
# Phase 7 follow-up: runtime/subprocess_env.py:get_scrubbed_sdk_env()
# will absorb this implementation; the contract above is preserved.

_DASHBOARD_ONLY_KEYS: frozenset[str] = frozenset({
    "DASHBOARD_TOKEN",
    "DASHBOARD_BIND",
    "DASHBOARD_PORT",
    "DASHBOARD_DB_PATH",
    "DASHBOARD_DEV_MODE_NO_AUTH",
})

# Bot-creds whitelist — env var prefixes that the BOT subprocess SHOULD
# inherit (these are the platform credentials the bot needs to connect).
# Anything else that looks secret-shaped (matches _SECRET_SHAPED_RE) gets
# scrubbed so the bot never sees, e.g., AWS or Stripe keys that belong
# to the dashboard process only.
_BOT_CREDS_PREFIXES: tuple[str, ...] = (
    "TELEGRAM_",
    "ANTHROPIC_",
    "OPENAI_",
    "GEMINI_",
    "OPENROUTER_",
    "KIMI_",
    "LANGFUSE_",
    "DISCORD_",
    "SLACK_",
    "WHATSAPP_",
    "ELEVENLABS_",
)

# Secret-shaped key heuristic — env var names that suggest credential
# material. Compared case-insensitively. Anything matching this AND not
# matching a _BOT_CREDS_PREFIXES prefix gets scrubbed.
_SECRET_SHAPED_RE = re.compile(
    r"(?:_TOKEN|_KEY|_SECRET|_PASSWORD|_PASSWD|_PWD|_API|_CREDENTIALS?|_CERT)$",
    re.IGNORECASE,
)


def _is_bot_creds_key(name: str) -> bool:
    """True iff *name* belongs to the bot-creds whitelist (prefix match)."""
    upper = name.upper()
    return any(upper.startswith(p) for p in _BOT_CREDS_PREFIXES)


def _scrub_dashboard_env(
    parent_env: dict[str, str] | None = None,
    profile_root: Path | None = None,
) -> dict[str, str]:
    """Return a sanitized env dict for a persona-bot subprocess.

    Drops dashboard-only keys + pattern-matched secret-shaped keys not
    on the bot-creds whitelist. Forces ``HOMIE_HOME`` to *profile_root*
    so the child boot resolves the TARGET persona's paths.

    Rule 1 — both args are None-sentineled and resolved in body:
      * ``parent_env=None`` → ``os.environ.copy()`` at call time
      * ``profile_root=None`` → ``ValueError`` (caller MUST pass an
        explicit target — never silently inherit dashboard's HOMIE_HOME)
    """
    if parent_env is None:
        parent_env = os.environ.copy()
    if profile_root is None:
        raise ValueError(
            "_scrub_dashboard_env: profile_root MUST be passed explicitly "
            "(do NOT silently inherit dashboard's HOMIE_HOME)"
        )

    out: dict[str, str] = {}
    for key, value in parent_env.items():
        if key in _DASHBOARD_ONLY_KEYS:
            continue
        if _SECRET_SHAPED_RE.search(key) and not _is_bot_creds_key(key):
            # Looks secret-shaped + not on the bot-creds whitelist → drop.
            continue
        out[key] = value

    # Force HOMIE_HOME to the target persona's root.
    out["HOMIE_HOME"] = str(profile_root)
    return out


# ── Profile-aware path resolution ────────────────────────────────────────


@contextmanager
def _persona_paths_context(persona_id: str) -> Iterator[tuple[Path, Path, Path, Path]]:
    """Yield (pid_file, lock_file, log_dir, profile_root) for *persona_id*.

    Save/restore HOMIE_HOME so the dashboard's own profile is never
    corrupted. Mirrors run_chat.sh:95-104 — the bot launcher uses the
    same pattern (set HOMIE_HOME → resolve paths via personas.services
    → restore on exit).
    """
    if not persona_id:
        raise ValueError("persona_id must be non-empty")

    # Resolve target profile root. ``_profile_root`` is the same private
    # helper that lifecycle.delete_profile uses (single-source-of-truth
    # for the named-profile root).
    if persona_id == "default":
        # Default profile keeps the install-dir layout; resolve via
        # personas.services helpers WITHOUT swapping HOMIE_HOME.
        prev_homie_home = os.environ.get("HOMIE_HOME")
        if "HOMIE_HOME" in os.environ:
            del os.environ["HOMIE_HOME"]
        try:
            pid_file = _services.get_bot_pid_path()
            lock_file = _services.get_bot_lock_path()
            log_dir = _services.get_log_dir()
            # Default profile root is the install-dir parent of memory/.
            from personas.core import get_default_paths

            default_paths = get_default_paths()
            profile_root = default_paths["memory"].parent.parent
            yield pid_file, lock_file, log_dir, profile_root
        finally:
            if prev_homie_home is not None:
                os.environ["HOMIE_HOME"] = prev_homie_home
        return

    # Named profile — flip HOMIE_HOME to the target profile root for
    # path resolution, then restore.
    target_root = _lifecycle_profile_root(persona_id)
    prev_homie_home = os.environ.get("HOMIE_HOME")
    os.environ["HOMIE_HOME"] = str(target_root)
    try:
        pid_file = _services.get_bot_pid_path()
        lock_file = _services.get_bot_lock_path()
        log_dir = _services.get_log_dir()
        yield pid_file, lock_file, log_dir, target_root
    finally:
        if prev_homie_home is None:
            os.environ.pop("HOMIE_HOME", None)
        else:
            os.environ["HOMIE_HOME"] = prev_homie_home


# ── Public API ───────────────────────────────────────────────────────────


def is_running(persona_id: str) -> bool:
    """Return True iff the persona's bot has a live process.

    Rule 2 — reads pid file, verifies via ``shared.is_pid_alive()``. A
    stale pid file pointing to a dead PID returns False (it does NOT
    raise; cleanup is the caller's responsibility).
    """
    with _persona_paths_context(persona_id) as (pid_file, _lock, _log, _root):
        pid = shared.read_pid(pid_file)
        if pid is None:
            return False
        return shared.is_pid_alive(pid)


def status(persona_id: str) -> dict:
    """Return ``{"running", "pid", "uptime_s"}`` for *persona_id*'s bot.

    Best-effort uptime via psutil; ``None`` on failure or when the
    process is not running. Used by the AgentDetail.tsx tasks tab.
    """
    with _persona_paths_context(persona_id) as (pid_file, _lock, _log, _root):
        pid = shared.read_pid(pid_file)
        if pid is None or not shared.is_pid_alive(pid):
            return {"running": False, "pid": None, "uptime_s": None}

        uptime_s: int | None = None
        try:
            import psutil  # noqa: PLC0415 — best-effort, lazy import
            proc = psutil.Process(pid)
            uptime_s = int(time.time() - proc.create_time())
        except Exception:
            # psutil missing or process disappeared between read_pid and
            # the create_time read — fail soft, return None.
            uptime_s = None
        return {"running": True, "pid": pid, "uptime_s": uptime_s}


def activate(
    persona_id: str,
    *,
    grace_seconds: int | None = None,
) -> dict:
    """Start the persona's bot subprocess. Idempotent.

    Returns ``{persona_id, pid, status}`` where status is ``"running"``
    on a fresh start or ``"already_running"`` when an existing live pid
    is detected.

    Rule 1 — ``grace_seconds=None`` sentinel resolved in body so test
    monkeypatches of ``config.DASHBOARD_BOT_GRACE_SECONDS`` propagate.
    The grace window is currently unused on activate but kept on the
    signature for symmetry with deactivate/restart and future expansion.
    """
    if grace_seconds is None:
        grace_seconds = config.DASHBOARD_BOT_GRACE_SECONDS  # noqa: F841

    with _persona_paths_context(persona_id) as (pid_file, lock_file, log_dir, profile_root):
        # Serialize concurrent same-persona activations via shared.file_lock().
        with shared.file_lock(lock_file):
            existing_pid = shared.read_pid(pid_file)
            if existing_pid is not None and shared.is_pid_alive(existing_pid):
                # Rule 2 — verified live process; idempotent path.
                return {
                    "persona_id": persona_id,
                    "pid": existing_pid,
                    "status": "already_running",
                }

            # Stale pid file with dead PID — clean before spawning.
            if existing_pid is not None:
                try:
                    pid_file.unlink(missing_ok=True)
                except OSError:
                    pass

            # Build the subprocess command. We invoke the bot via the
            # same Python that started the dashboard process. Bot entry
            # point: chat/main.py.
            scripts_dir = Path(config.SCRIPTS_DIR)
            bot_main = scripts_dir.parent / "chat" / "main.py"
            if not bot_main.is_file():
                raise FileNotFoundError(
                    f"Bot entry point missing: {bot_main}"
                )

            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "bot.log"

            scrubbed = _scrub_dashboard_env(profile_root=profile_root)

            # Launch detached so the dashboard process can return quickly.
            # On Windows: CREATE_NEW_PROCESS_GROUP so signal.SIGTERM
            # (CTRL_BREAK_EVENT) can be delivered to the child without
            # killing the dashboard.
            popen_kwargs: dict = {
                "env": scrubbed,
                "stdout": open(log_path, "ab"),  # noqa: SIM115
                "stderr": subprocess.STDOUT,
                "stdin": subprocess.DEVNULL,
                "cwd": str(scripts_dir),
            }
            if sys.platform == "win32":
                popen_kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                )
            else:
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(
                [sys.executable, str(bot_main)],
                **popen_kwargs,
            )
            new_pid = proc.pid
            # Persist pid in the canonical location (matches shared.write_pid
            # contract — the path is the target persona's pid file, NOT
            # the dashboard's).
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(new_pid), encoding="utf-8")

            return {
                "persona_id": persona_id,
                "pid": new_pid,
                "status": "running",
            }


def deactivate(
    persona_id: str,
    *,
    grace_seconds: int | None = None,
) -> dict:
    """Stop the persona's bot. Idempotent.

    Returns ``{persona_id, status}`` where status is ``"stopped"`` on a
    successful kill, ``"already_stopped"`` if the bot was not running
    (pid file missing OR pid file present but process dead — Rule 2
    physical-state check via ``shared.is_pid_alive``).

    Cross-platform termination:
      * Windows: ``subprocess.run(["taskkill", "/F", "/PID", pid])`` —
        same pattern as ``shared.cleanup_stale_pid:468-475``.
      * Unix: ``signal.SIGTERM``, wait *grace_seconds* (default 5s from
        ``config.DASHBOARD_BOT_GRACE_SECONDS`` — Rule 1 None sentinel),
        escalate to ``signal.SIGKILL``.

    Final verify via ``shared.is_pid_alive`` — raises ``RuntimeError`` if
    the process refuses to die after escalation.
    """
    if grace_seconds is None:
        grace_seconds = config.DASHBOARD_BOT_GRACE_SECONDS

    with _persona_paths_context(persona_id) as (pid_file, lock_file, _log, _root):
        with shared.file_lock(lock_file):
            pid = shared.read_pid(pid_file)
            if pid is None:
                # No pid file — definitely not running.
                return {"persona_id": persona_id, "status": "already_stopped"}

            # Rule 2 — physical state. Stale pid (file present, process
            # dead) is NOT an error; clean and return already_stopped.
            if not shared.is_pid_alive(pid):
                try:
                    pid_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return {"persona_id": persona_id, "status": "already_stopped"}

            # Active pid — terminate.
            if sys.platform == "win32":
                # taskkill /F /PID — mirrors shared.cleanup_stale_pid.
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                        timeout=10,
                    )
                except Exception:  # noqa: BLE001 — best-effort, verify below
                    pass
                # Give Windows a moment to reap the process.
                deadline = time.monotonic() + max(grace_seconds, 1)
                while time.monotonic() < deadline:
                    if not shared.is_pid_alive(pid):
                        break
                    time.sleep(0.1)
            else:
                # Unix: SIGTERM, wait, escalate to SIGKILL.
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    # Race: process died between is_pid_alive and kill.
                    pid_file.unlink(missing_ok=True)
                    return {"persona_id": persona_id, "status": "already_stopped"}

                deadline = time.monotonic() + grace_seconds
                while time.monotonic() < deadline:
                    if not shared.is_pid_alive(pid):
                        break
                    time.sleep(0.1)

                if shared.is_pid_alive(pid):
                    # Escalate.
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    deadline = time.monotonic() + max(grace_seconds, 2)
                    while time.monotonic() < deadline:
                        if not shared.is_pid_alive(pid):
                            break
                        time.sleep(0.1)

            # Rule 2 final verify.
            if shared.is_pid_alive(pid):
                raise RuntimeError(
                    f"Bot for persona {persona_id!r} (pid {pid}) refused "
                    f"to die after escalation."
                )

            # Clean pid file.
            try:
                pid_file.unlink(missing_ok=True)
            except OSError:
                pass
            return {"persona_id": persona_id, "status": "stopped"}


def restart(
    persona_id: str,
    *,
    grace_seconds: int | None = None,
) -> dict:
    """Chain deactivate → activate. If deactivate raises, activate is NOT called.

    Returns ``{persona_id, old_pid, new_pid, status}`` where status is
    ``"restarted"`` on success.

    Rule 1 — ``grace_seconds=None`` sentinel resolved in body and
    forwarded to deactivate (activate also accepts the kwarg for
    symmetry; currently unused by activate).
    """
    if grace_seconds is None:
        grace_seconds = config.DASHBOARD_BOT_GRACE_SECONDS

    with _persona_paths_context(persona_id) as (pid_file, _lock, _log, _root):
        old_pid = shared.read_pid(pid_file)

    deactivate(persona_id, grace_seconds=grace_seconds)
    started = activate(persona_id, grace_seconds=grace_seconds)
    return {
        "persona_id": persona_id,
        "old_pid": old_pid,
        "new_pid": started["pid"],
        "status": "restarted",
    }
