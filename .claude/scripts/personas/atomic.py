"""Atomic primitives for the profile lifecycle layer.

Phase 2 / PRP-7b Workstream 1 (lifecycle-core). Provides the load-bearing
process / lock primitives that ``personas.lifecycle.delete_profile`` relies
on to safely tear down a profile:

    is_pid_alive(pid)                -> cross-platform alive probe (no psutil)
    _acquire_delete_lock(profile)    -> sentinel-file mutual-exclusion guard
    _try_acquire_state_lock(path)    -> mirror of ``shared.file_lock`` byte-0
                                         probe (R3 NNB1 — Windows-correct)
    _check_state_file_lockholders    -> rglob ``*.lock`` + per-file probe
    quiesce_profile(name, root)      -> Steps 1, 2, 4 of the delete ladder

This module is the SOLE place in the codebase that does cross-platform
process / OS-lock probing for Phase 2. Phase 3 will own the bot-pid /
mutex consolidation that lives in ``shared.py:329`` / ``main.py:117-160``;
Phase 2 only READS those existing surfaces via ``get_persona_paths(name)["run"] / "bot.pid"``.

Anti-pattern compliance:
    - Rule 1: ``on_progress=None`` is a None-sentinel for an optional callback;
      we resolve it inside the body, never bind to a default ``config.X``.
    - Rule 2: every alive / lockholder check reads PHYSICAL state — OS process
      table via ``os.kill(pid, 0)`` / ``OpenProcess``, real OS-level
      non-blocking lock acquisition mirroring ``shared.file_lock``. No
      sidecar PID-text parsing, no meta cache.
    - Rule 3: N/A — no Langfuse / observability calls here.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional


@dataclass
class QuiesceResult:
    """Outcome of ``quiesce_profile`` — each step either completed, was a
    no-op, or surfaced an error that aborts the whole delete sequence.

    Field semantics:
        - ``bot_terminated``: True iff the bot was alive when we found the
          PID file AND we sent SIGTERM (and possibly SIGKILL).
        - ``bot_was_dead``: True iff the bot PID file existed but the PID
          was stale (process not running). Dead-PID shortcut — no wait.
        - ``scheduled_units_disabled``: list of unit / plist labels that
          were disabled (best-effort; never raises).
        - ``delete_lock_acquired``: kept for symmetry with the original
          design even though the caller now owns Step 3. Always False from
          ``quiesce_profile`` itself.
        - ``state_locks_active``: paths of ``.lock`` files in the profile's
          state dir that are currently held by a live writer.
        - ``aborted_at``: step name where the ladder stopped, or ``None``
          on success.
    """

    bot_terminated: bool
    bot_was_dead: bool
    scheduled_units_disabled: list[str] = field(default_factory=list)
    delete_lock_acquired: bool = False
    state_locks_active: list[str] = field(default_factory=list)
    aborted_at: Optional[str] = None


def is_pid_alive(pid: int) -> bool:
    """Return ``True`` iff *pid* corresponds to a live process.

    Cross-platform — no psutil dependency (psutil is not a hard dep in this
    repo; ctypes ships with Python).

    POSIX:
        ``os.kill(pid, 0)`` raises ``ProcessLookupError`` if the process is
        dead. ``PermissionError`` means the process exists but we don't own
        it — still alive, return True. Anything else: return False.

    Windows:
        ``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, ...)`` returns 0 if
        the process is dead OR the caller lacks permission. ``GetLastError``
        with ``ERROR_INVALID_PARAMETER`` means "no such process"; any other
        non-zero last-error means "exists but inaccessible". For Phase 2 we
        treat handle == 0 as dead — defensive enough because every relevant
        process in The Homie's profile dir runs under the same user.

        When we DO get a handle, we further check ``GetExitCodeProcess`` for
        ``STILL_ACTIVE`` (259) — distinguishes "alive" from "exited with
        code 259" (vanishingly rare false positive, but still preferred to
        a bare handle-non-zero check).

    Returns False for ``pid <= 0``.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not h:
            # Handle == 0 -> process does not exist (or insufficient
            # privilege; treated as "dead" for our use case — the bot
            # daemons we probe always run as the same user that owns the
            # profile dir, so a permission-denied here would itself be
            # anomalous).
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
            if not ok:
                # Couldn't query exit code — fall back to "handle non-zero
                # means alive" semantics. Defensive default.
                return True
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(h)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists, just not ours.
            return True
        except OSError:
            return False


@contextmanager
def _acquire_delete_lock(profile_dir: Path) -> Iterator[Path]:
    """Sentinel-file mutual-exclusion guard for the profile dir during delete.

    R1 B1 + R4 minor — this is a SENTINEL FILE guard, NOT an advisory OS
    lock on the file descriptor. The file's EXISTENCE is the lock; another
    process trying to enter this context observes ``FileExistsError`` from
    ``open(path, 'x')`` and aborts.

    Implementation details:
        - ``open(lock_path, 'x')`` uses Python's O_EXCL semantics: opens
          the file iff it does not already exist; raises ``FileExistsError``
          otherwise. Atomic on every filesystem we ship to.
        - The fd is closed immediately after writing the holder's PID
          (debugging aid). The file's continued existence — not the open
          fd — is the guard.
        - On context exit (success OR exception), the sentinel is unlinked.
          ``FileNotFoundError`` is suppressed (someone else cleaned it up
          mid-race; benign).

    Caller MUST enter via ``with _acquire_delete_lock(profile_dir): ...``.
    The lock file lands at ``<profile_dir>/.delete.lock`` — caller is
    responsible for ensuring ``profile_dir`` already exists AND has been
    containment-checked (symlink-rejected) BEFORE entering this context.

    Raises ``LifecycleError`` (lazy import from ``.lifecycle`` to avoid a
    circular at module load — ``lifecycle.py`` imports this module) when
    another process already holds the sentinel.
    """
    lock_path = profile_dir / ".delete.lock"
    try:
        # 'x' mode: open for exclusive creation, failing if file exists.
        # Equivalent to O_CREAT | O_EXCL | O_WRONLY at the OS level.
        lock_fd = open(lock_path, "x", encoding="utf-8")
        try:
            lock_fd.write(str(os.getpid()))
        finally:
            lock_fd.close()
    except FileExistsError:
        # Lazy import to avoid circular: lifecycle imports atomic, atomic
        # imports lifecycle.LifecycleError. Inline-import inside the raise
        # path keeps module load order clean.
        from .lifecycle import LifecycleError

        raise LifecycleError(
            f"Profile '{profile_dir.name}' is being deleted by another "
            f"process. Remove {lock_path} if stale."
        )
    try:
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            # Already cleaned up by another path (e.g. rmtree of profile_dir
            # during normal flow took the lock file with it). Benign.
            pass
        except OSError:
            # Permission / device error during cleanup — don't mask the
            # original exception (if any). Best-effort cleanup only.
            pass


def _try_acquire_state_lock(lock_path: Path) -> bool:
    """Probe whether *lock_path* (an already-suffixed ``.lock`` file) is held.

    Returns True iff the file exists AND we successfully acquired the
    OS-level non-blocking exclusive lock (then released immediately).
    Returns False if the lock is held, the file is missing, or any error
    occurred — the caller fails CLOSED on False.

    R3 NNB1 LOAD-BEARING NOTE — Windows byte-0 vs EOF semantics:

    ``shared.file_lock(lock_path)`` (`.claude/scripts/shared.py:277-322`)
    opens the lock file with mode ``"w"`` (truncates -> file pointer at byte
    0) and on Windows calls ``msvcrt.locking(fd, LK_NBLCK, 1)`` which locks
    1 byte starting at the CURRENT file pointer — i.e. byte 0.

    If our probe opened with ``"a"`` (append — FP at EOF), ``msvcrt.locking``
    would attempt to lock the EOF byte. With a non-empty lock file (some
    holder left content behind), byte 0 != EOF -> no contention -> probe
    sees "free" while the real holder owns byte 0 -> false negative ->
    delete proceeds while a real writer is active.

    Probe semantics (mirror the real primitive EXACTLY on Windows):
        1. *lock_path* IS the already-suffixed ``.lock`` target. The caller
           ``rglob``s ``*.lock``, so each match is the suffixed file —
           do NOT re-suffix it.
        2. Open with mode ``"r+"`` (read+write, NO truncate, FP at byte 0).
           ``"w"`` would destructively truncate any stale content; ``"a"``
           would put FP at EOF and break Windows byte-0 locking.
        3. ``"r+"`` requires the file to exist; if it raises
           ``FileNotFoundError`` (race: deleted between rglob and open),
           fail CLOSED.
        4. Windows: ``msvcrt.locking(fd, LK_NBLCK, 1)`` locks byte 0.
           POSIX:   ``fcntl.flock(fd, LOCK_EX | LOCK_NB)`` whole-file.
        5. On acquire -> release immediately and return True (free).
        6. On ``OSError``/``BlockingIOError`` during lock attempt -> return
           False (held).
        7. Any other open / permission error -> return False (caller fails
           CLOSED — refuse delete on ambiguous state).
    """
    f = None
    try:
        # R3 NNB1: open with "r+" — NOT "w" (destructive), NOT "a" (FP=EOF
        # would defeat byte-0 locking on Windows). "r+" leaves FP at byte 0
        # and does NOT truncate.
        try:
            f = open(lock_path, "r+", encoding="utf-8")
        except FileNotFoundError:
            # Race: file vanished between rglob and open. Fail CLOSED.
            return False
        if sys.platform == "win32":
            import msvcrt

            try:
                # Locks 1 byte at current FP (=byte 0 because "r+" did not
                # seek). Matches `shared.file_lock`'s byte-0 lock target.
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                # Held by another process (EAGAIN-equivalent on Windows).
                return False
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            return True
        else:
            import fcntl

            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                return False
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            return True
    except OSError:
        # Permission denied, device error, etc. — fail CLOSED.
        return False
    finally:
        if f is not None:
            try:
                f.close()
            except OSError:
                pass


def _check_state_file_lockholders(state_dir: Path) -> list[Path]:
    """Return paths of ``*.lock`` files in *state_dir* currently held by a
    LIVE OS lock.

    R2 NB1 + R3 NNB1 design (Rule 2 — physical state, not meta):

    Previous (broken) approaches and why they were wrong:
      - R1 pass-1: read ``.lock`` files for PID text. Vacuous —
        ``shared.file_lock`` writes nothing into the lock file.
      - R2 pass-2: open with ``"a"`` and ``msvcrt.locking(LK_NBLCK, 1)``.
        Wrong on Windows because ``"a"`` puts FP at EOF and msvcrt locks
        from current FP -> false "free" reading on any non-empty stale
        lock file (R3 NNB1).

    Current (correct) approach:
      - ``rglob("*.lock")`` enumerates already-suffixed targets (these
        ARE the files ``shared.file_lock`` opens — do NOT re-suffix).
      - For each, call ``_try_acquire_state_lock(lock_path)``. The probe
        opens with ``"r+"`` (no truncate, FP at byte 0), then attempts the
        SAME non-blocking lock the real primitive uses, and releases
        immediately on success.
      - True -> free; not held.
      - False -> held OR unexpected error -> caller fails CLOSED.

    Fail-closed invariants:
      - ``state_dir.rglob`` raising ``OSError`` returns
        ``[state_dir / "<scan-failed>"]`` so the caller's ``if held:``
        branch refuses delete.
      - Per-file exceptions append the path to ``held`` (R2 minor).
      - Documented rationale: ambiguous lock state on a destructive
        operation must default to refuse, not approve.

    Caller passes the REAL state directory
    (``get_persona_paths(name)["state"]``), NOT a derived
    ``<root>/state`` guess.
    """
    if not state_dir.is_dir():
        return []
    held: list[Path] = []
    try:
        candidates = list(state_dir.rglob("*.lock"))
    except OSError:
        # Couldn't even iterate the directory — fail CLOSED with a
        # sentinel that the caller's `if held:` check treats as truthy.
        return [state_dir / "<scan-failed>"]
    for lock_path in candidates:
        try:
            free = _try_acquire_state_lock(lock_path)
        except Exception:
            # Anything unexpected -> fail CLOSED.
            held.append(lock_path)
            continue
        if not free:
            held.append(lock_path)
    return held


def quiesce_profile(
    name: str,
    profile_root: Path,
    *,
    on_progress: Optional[Callable[[str], None]] = None,
) -> QuiesceResult:
    """Run the quiesce ladder BEFORE rmtree.

    R1 B1 contract:
      - ``profile_root`` is an EXPLICIT parameter (computed by the caller
        via ``_profile_root(name)``); quiesce no longer guesses the root
        from ``workspace`` (which is ``<root>/workspace``, not ``<root>``
        itself).
      - Quiesce performs the state-lock check + bot kill + unit uninstall.
        The delete-lock acquisition (Step 4) and rmtree (Step 9) are owned
        by the CALLER (``delete_profile``), so the delete-lock context
        manager wraps the entire destructive sequence — quiesce checks AND
        rmtree run INSIDE the same ``with _acquire_delete_lock(...):`` block.
        This is the load-bearing Rule 2 invariant.
      - State-file lockholder scan reads from
        ``get_persona_paths(name)["state"]`` directly (the real state
        dir, not ``profile_root / "state"`` which would also be correct
        for named profiles but is consistent with how Phase 1 exposes
        per-profile paths).

    Step ordering (PRD §9.1 state-machine table, PRP-7b lines 1982-1995):
        5. CHECK STATE-FILE LOCKHOLDERS FIRST. If any are held, raise
           ``LifecycleError`` BEFORE any side effect — the bot stays alive
           and scheduled units stay installed (no half-quiesced profile).
        6. TERM the bot if alive (dead-PID shortcut: skip wait if dead).
        7. Disable scheduled units (systemd / launchd — best-effort;
           cron is out of scope for Phase 2).
        (Step 4 acquire delete-lock, Step 8 remove_wrapper, and Step 9
         rmtree live in the caller.)

    R-post-build F1 fix: Step 5 used to run AFTER Steps 6+7, which meant a
    profile with held state locks would get its bot killed and its launchd /
    systemd unit uninstalled, and only THEN refuse the delete — a
    half-quiesced profile. Now Step 5 runs first; nothing destructive
    happens until the lock probe says "free".

    On success returns the populated ``QuiesceResult``. On state-lock
    contention raises ``LifecycleError`` (the caller's
    ``with _acquire_delete_lock(...):`` releases the delete-lock on
    propagation).

    *on_progress* is a None-sentinel-defaulted callback invoked with a
    short status string before each potentially-blocking step (Rule 1 —
    optional callback resolved at call time).
    """
    # Lazy import: cross-package within personas/. Same-package private
    # imports from siblings (.core, .wrappers) are part of Phase 2's
    # documented carve-out from the public-API guard (PRP-7b §1948).
    from .core import get_persona_paths

    result = QuiesceResult(
        bot_terminated=False,
        bot_was_dead=False,
        scheduled_units_disabled=[],
        delete_lock_acquired=False,
        state_locks_active=[],
    )

    paths = get_persona_paths(name)

    # --- Step 5 (FIRST — load-bearing F1 fix): state-file lock holders ---
    # Held locks abort BEFORE the bot is terminated and BEFORE scheduled
    # units are uninstalled. Otherwise we'd leave a half-quiesced profile
    # (bot dead, launchd unloaded) but still on disk because the lock
    # holder refused removal. PRP-7b state-machine table line 1982 plus
    # the "Step ordering invariant (TESTED)" block at line 1994 both
    # require this ordering.
    held = _check_state_file_lockholders(paths["state"])
    if held:
        result.state_locks_active = [str(p) for p in held]
        result.aborted_at = "step5_state_locks"
        # Lazy import — atomic.py is loaded before lifecycle.py in the
        # delete_profile call chain, so this is the legitimate cross-module
        # forward reference (LifecycleError is defined in lifecycle.py).
        from .lifecycle import LifecycleError

        raise LifecycleError(
            f"Profile '{name}' state locks held by {len(held)} writer"
            f"{'s' if len(held) != 1 else ''}; cannot delete safely. "
            f"Held: {[str(p) for p in held]}"
        )

    # --- Step 6: bot quiesce (dead-PID shortcut) -------------------------
    bot_pid_path = paths["run"] / "bot.pid"
    if bot_pid_path.exists():
        try:
            pid_text = bot_pid_path.read_text(encoding="utf-8").strip()
            pid = int(pid_text.split()[0]) if pid_text else 0
            if pid > 0:
                if is_pid_alive(pid):
                    if on_progress is not None:
                        on_progress("Stopping bot daemon...")
                    if sys.platform == "win32":
                        # GenerateConsoleCtrlEvent is the closest analog to
                        # SIGTERM for console processes; bot.py's signal
                        # handlers map this to a clean shutdown. If the
                        # process is detached / no console, this returns
                        # 0 — that's OK because we still escalate to
                        # TerminateProcess after the 2s deadline.
                        import ctypes

                        try:
                            ctypes.windll.kernel32.GenerateConsoleCtrlEvent(
                                0, pid
                            )
                        except OSError:
                            pass
                    else:
                        try:
                            os.kill(pid, 15)  # SIGTERM
                        except OSError:
                            pass
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        if not is_pid_alive(pid):
                            break
                        time.sleep(0.1)
                    if is_pid_alive(pid):
                        # Still alive after 2s — escalate.
                        if sys.platform == "win32":
                            import ctypes

                            PROCESS_TERMINATE = 0x0001
                            kernel32 = ctypes.windll.kernel32
                            h = kernel32.OpenProcess(
                                PROCESS_TERMINATE, False, pid
                            )
                            if h:
                                try:
                                    kernel32.TerminateProcess(h, 1)
                                finally:
                                    kernel32.CloseHandle(h)
                        else:
                            try:
                                os.kill(pid, 9)  # SIGKILL
                            except OSError:
                                pass
                    result.bot_terminated = True
                else:
                    result.bot_was_dead = True
        except (ValueError, OSError):
            # Corrupt pid file or permission issue — proceed anyway.
            # The state-file lock check below is the load-bearing guard.
            pass

    # --- Step 7: disable scheduled units (best-effort) -------------------
    # Lazy import — wrappers module may not be loaded yet (WS3 ships in
    # parallel with WS1; the integration point is at-call-site). On
    # ImportError fall through silently (Phase 3 may extend this; for
    # Phase 2 the unit cleanup is best-effort).
    try:
        from . import wrappers as _wrappers
    except ImportError:
        _wrappers = None  # type: ignore[assignment]

    if _wrappers is not None:
        if sys.platform == "darwin":
            try:
                if _wrappers._uninstall_launchd_plist(name):
                    result.scheduled_units_disabled.append(
                        f"launchd:com.smokedev.homie.{name}"
                    )
            except Exception:
                # Best-effort — never block delete on unit cleanup failure.
                pass
        elif sys.platform == "linux":
            try:
                if _wrappers._uninstall_systemd_unit(name):
                    result.scheduled_units_disabled.append(
                        f"systemd:homie-{name}"
                    )
            except Exception:
                pass
    # Cron is OUT OF SCOPE for Phase 2 (Phase 3 owns full cron isolation).

    return result
