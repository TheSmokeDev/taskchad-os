"""
Shared utilities for The Homie scripts.

Centralizes code that was duplicated across heartbeat.py, memory_reflect.py,
and memory_flush.py: security patterns, state management, daily log helpers,
and file locking.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from config import STATE_DIR, get_today_log_path, now_local

if TYPE_CHECKING:
    from claude_agent_sdk import HookContext, HookInput
    from claude_agent_sdk.types import SyncHookJSONOutput


# =============================================================================
# DANGEROUS COMMAND PATTERNS - Block these in PreToolUse hook
# =============================================================================

DANGEROUS_BASH_PATTERNS: list[str] = [
    # Destructive file operations
    "rm -rf /",
    "rm -rf ~",
    "rm -rf /*",
    "rm -rf .",
    "rm -rf *",
    # Disk operations
    "> /dev/sda",
    "> /dev/hda",
    "dd if=/dev/zero",
    "dd if=/dev/random",
    "mkfs.",
    # Fork bombs and system attacks
    ":(){:|:&};:",
    ":(){ :|:& };:",
    # Dangerous downloads and execution
    "curl | sh",
    "curl | bash",
    "wget | sh",
    "wget | bash",
    # Permission disasters
    "chmod -R 777 /",
    "chmod -R 000 /",
    "chown -R",
    # History and credential theft
    "history -c",
    # Network attacks
    "> /dev/tcp",
    # Data destruction
    "truncate -s 0",
    "shred",
]

# Extra patterns to block in SSH remote commands (on top of DANGEROUS_BASH_PATTERNS)
DANGEROUS_SSH_PATTERNS: list[str] = [
    "DROP TABLE",
    "DROP DATABASE",
    "TRUNCATE TABLE",
    "DELETE FROM",
    "killall",
    "pkill -9",
    "systemctl stop",
    "systemctl disable",
    "shutdown",
    "reboot",
    "init 0",
    "init 6",
    "iptables -F",
    "ufw disable",
    "passwd",
    "userdel",
    "groupdel",
]


async def validate_bash_command(
    input_data: HookInput,
    tool_use_id: str | None,
    context: HookContext,
) -> SyncHookJSONOutput:
    """PreToolUse hook to validate bash commands and block dangerous ones.

    Checks both local commands and remote commands inside ssh invocations.
    """
    tool_input = input_data.get("tool_input")
    command: str = tool_input.get("command", "") if isinstance(tool_input, dict) else ""

    # Normalize: collapse whitespace
    normalized = " ".join(command.split())

    # Also check inside subshell constructs
    commands_to_check = [normalized]
    # Extract $(...) content
    subshells = re.findall(r'\$\(([^)]+)\)', normalized)
    commands_to_check.extend(subshells)
    # Extract backtick content
    backticks = re.findall(r'`([^`]+)`', normalized)
    commands_to_check.extend(backticks)

    # Check for SSH remote commands — extract the remote command part
    ssh_remote_cmds: list[str] = []
    # Match: ssh [options] host "command" or ssh [options] host 'command'
    ssh_quoted = re.findall(r'\bssh\b[^"\']*["\'](.+?)["\']', normalized)
    ssh_remote_cmds.extend(ssh_quoted)
    # Match: ssh host command (unquoted, after the host)
    ssh_unquoted = re.match(r'\bssh\b\s+(?:-\S+\s+)*\S+\s+(.+)', normalized)
    if ssh_unquoted and not ssh_quoted:
        ssh_remote_cmds.append(ssh_unquoted.group(1))

    for cmd in commands_to_check:
        # Strip common binary path prefixes
        stripped = re.sub(r'(?:/usr)?/s?bin/', '', cmd)

        for pattern in DANGEROUS_BASH_PATTERNS:
            if pattern in stripped:
                print(f"[SECURITY] Blocked dangerous command: {pattern}")
                return {"decision": "block", "reason": f"Blocked dangerous command pattern: {pattern}"}

    # Extra checks for SSH remote commands
    for remote_cmd in ssh_remote_cmds:
        stripped = re.sub(r'(?:/usr)?/s?bin/', '', remote_cmd)
        # Check all base patterns against the remote command too
        for pattern in DANGEROUS_BASH_PATTERNS:
            if pattern in stripped:
                print(f"[SECURITY] Blocked dangerous SSH remote command: {pattern}")
                return {"decision": "block", "reason": f"Blocked dangerous remote command: {pattern}"}
        # Check SSH-specific dangerous patterns (case-insensitive)
        for pattern in DANGEROUS_SSH_PATTERNS:
            if pattern.lower() in stripped.lower():
                print(f"[SECURITY] Blocked dangerous SSH command: {pattern}")
                return {"decision": "block", "reason": f"Blocked dangerous SSH command: {pattern}"}

    return {}


# =============================================================================
# STATE MANAGEMENT
# =============================================================================


def load_state(state_file: Path) -> dict[str, Any]:
    """Load state from a JSON file with error handling."""
    if state_file.exists():
        try:
            data: dict[str, Any] = json.loads(state_file.read_text(encoding="utf-8"))
            return data
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict[str, Any], state_file: Path) -> None:
    """Save state to a JSON file atomically (tmp + os.replace).

    Serialization happens before any file is touched, and the payload lands
    in a sibling tmp file that replaces the target in a single os.replace()
    step — an interrupted or failed save can never leave partial/corrupt JSON
    behind for load_state() to fail-open into ``{}`` (which would erase alert
    history and blocker counters). Behavior contract unchanged: same
    signature, same target path, same JSON shape.
    """
    payload = json.dumps(state, indent=2, default=str)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = state_file.with_suffix(state_file.suffix + ".tmp")
    tmp_file.write_text(payload, encoding="utf-8")
    os.replace(tmp_file, state_file)


# =============================================================================
# RETRY UTILITY
# =============================================================================


def with_retry(
    func: Any,
    max_retries: int = 3,
    backoff: float = 1.0,
) -> Any:
    """Call func(), retry on transient errors with exponential backoff.

    Retries on: ConnectionError, TimeoutError, HTTP 429/500/502/503.
    """
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            # Check for retryable HTTP errors
            retryable = isinstance(e, (ConnectionError, TimeoutError))
            if hasattr(e, "resp") and hasattr(e.resp, "status"):
                retryable = e.resp.status in (429, 500, 502, 503)
            if hasattr(e, "status_code"):
                retryable = e.status_code in (429, 500, 502, 503)
            if not retryable:
                raise
            time.sleep(backoff * (2 ** attempt))


# =============================================================================
# DAILY LOG HELPERS
# =============================================================================


def _create_daily_log(log_path: Path) -> None:
    """Create a new daily log with standardized sections."""
    from config import DAILY_LOG_SECTIONS

    header = f"# Daily Log: {now_local().strftime('%Y-%m-%d')}\n\n"
    for section in DAILY_LOG_SECTIONS:
        header += f"## {section}\n\n"
    log_path.write_text(header, encoding="utf-8")


def append_to_daily_log(content: str, section_name: str = "Entry") -> None:
    """Append content to today's daily log under a named section."""
    log_path = get_today_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with file_lock(log_path, timeout=5.0):
        timestamp = now_local().strftime("%H:%M")

        if not log_path.exists():
            _create_daily_log(log_path)

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"### {section_name} ({timestamp})\n\n{content}\n\n")


# =============================================================================
# HOOK EXECUTION LOGGING
# =============================================================================

HOOK_LOG_FILE = STATE_DIR / "hook-execution.log"
HOOK_LOG_MAX_LINES = 1000
HOOK_LOG_KEEP_LINES = 500


def log_hook_execution(
    hook_name: str,
    trigger: str,
    status: str,
    duration_s: float,
    detail: str = "",
) -> None:
    """Append a line to the hook execution log with simple rotation."""
    timestamp = now_local().isoformat()
    line = f"{timestamp} | {hook_name} | {trigger} | {status} | {duration_s:.1f}s"
    if detail:
        line += f" | {detail}"

    try:
        HOOK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Rotate if too large
        if HOOK_LOG_FILE.exists():
            lines = HOOK_LOG_FILE.read_text(encoding="utf-8").splitlines()
            if len(lines) >= HOOK_LOG_MAX_LINES:
                HOOK_LOG_FILE.write_text(
                    "\n".join(lines[-HOOK_LOG_KEEP_LINES:]) + "\n",
                    encoding="utf-8",
                )

        with open(HOOK_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass  # Hook logging must never crash the hook itself


# =============================================================================
# FILE LOCKING (cross-platform)
# =============================================================================


@contextlib.contextmanager
def file_lock(lock_path: Path, timeout: float = 30.0) -> Iterator[None]:
    """Cross-platform file lock using a .lock file.

    Uses msvcrt on Windows, fcntl on Unix.
    Raises TimeoutError if the lock cannot be acquired within timeout seconds.
    """
    lock_file = lock_path.with_suffix(lock_path.suffix + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_file, "w", encoding="utf-8")  # noqa: SIM115
    acquired = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Could not acquire lock on {lock_file} within {timeout}s"
                    )
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            if sys.platform == "win32":
                import msvcrt

                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


@contextlib.contextmanager
def browser_write_lock(timeout: float = 600.0) -> Iterator[None]:
    """Serialize visible-Chrome WRITE drives across processes.

    The CDP browser is ONE logged-in session — concurrent drives interleave
    tabs and keystrokes. Every browser-write ingress (Browser Homie runner,
    cadence cron dispatch, per-action chat writes) must hold this lock for
    the whole drive. Raises TimeoutError when another write holds it past
    *timeout* (default 10 min — longer than any sane drive).
    """
    import config

    with file_lock(Path(config.DATA_DIR) / "browser-write", timeout=timeout):
        yield


def atomic_write_text(path: Path, content: str) -> int:
    """Write ``content`` to ``path`` atomically via tmp + os.replace.

    Canonical framework atomic-write primitive (same family as ``file_lock``
    above). Consolidates the ``_atomic_write`` clones that grew independently
    in episodes.py, living_memory.py, and the cofounder slice — the identity
    payload precedent applied to filesystem primitives.

    Writes bytes (UTF-8, no platform newline translation — LF everywhere, the
    behavior episodes/living_memory always had) and creates parent dirs.
    Returns bytes written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = content.encode("utf-8")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return len(data)


# =============================================================================
# PID FILE MANAGEMENT
# =============================================================================
#
# PRP-7c Phase 3 (lifecycle-surfaces workstream): the bot pid file path is now
# profile-aware via ``personas.services.get_bot_pid_path()``. The legacy module
# constant ``BOT_PID_FILE`` is preserved as a lazy ``__getattr__`` resolver so
# external callers that historically did ``from shared import BOT_PID_FILE``
# keep working — the lookup happens AT ATTRIBUTE ACCESS TIME, not at import
# time, so monkeypatching ``personas.services.get_bot_pid_path`` (or swapping
# ``HOMIE_HOME`` between profiles in the same process) takes effect
# immediately.
#
# All consumer functions below use the ``None`` sentinel pattern (Anti-pattern
# Rule 1 — never bind a tunable config value as a function default arg). Tests
# enforce this via an AST scan (``test_persona_port_allocation.py``).

# Rule 3 / monkeypatch propagation: import the module so tests that patch
# ``personas.services.X`` propagate. Direct ``from .services import X``
# would cache the function object and short-circuit the patch.
from personas import services as _services  # noqa: E402


def __getattr__(name: str) -> Any:
    """Lazy module-level attribute resolver (PEP 562).

    Routes the legacy ``BOT_PID_FILE`` name through the profile-aware
    ``personas.services.get_bot_pid_path()`` resolver. Resolution happens
    at every attribute access (no def-time bind), so a profile swap mid-
    process or a monkeypatch in tests takes effect immediately.
    """
    if name == "BOT_PID_FILE":
        return _services.get_bot_pid_path()
    raise AttributeError(f"module 'shared' has no attribute {name!r}")


def write_pid(pid_file: Path | None = None) -> None:
    """Write current process PID to file.

    Writes the canonical pid path FIRST (atomic via tempfile + os.replace),
    then best-effort writes the historical compat-shadow pid path
    (``<install>/.claude/chat/bot.pid``) — but ONLY when the active profile
    is the default profile. Named profiles never write the shadow because
    doing so would corrupt the default's compat file.

    The shadow write is wrapped in try/except — bot startup must not be
    blocked by a shadow write failure (R4-NM1: shadow is best-effort,
    fail-open).
    """
    if pid_file is None:
        pid_file = _services.get_bot_pid_path()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_text = str(os.getpid())
    # Canonical write — atomic.
    _services._atomic_write_text(pid_file, pid_text)
    # Best-effort compatibility shadow write (default profile only).
    if _services._should_write_compat_shadow():
        try:
            shadow = _services._compat_shadow_pid_path()
            shadow.parent.mkdir(parents=True, exist_ok=True)
            _services._atomic_write_text(shadow, pid_text)
        except Exception:
            # R4-NM1: never block bot startup on shadow write failure.
            pass


def read_pid(pid_file: Path | None = None) -> int | None:
    """Read PID from file, return None if missing/invalid.

    Reads the CANONICAL pid path only — the compat shadow is write-only and
    must never be a read source.
    """
    if pid_file is None:
        pid_file = _services.get_bot_pid_path()
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is still running (cross-platform).

    WARNING: os.kill(pid, 0) is BROKEN on Windows — returns True for recently
    dead processes. Verified on Python 3.12.11 + Windows 11. Use ctypes instead.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        process_query_limited = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return exit_code.value == still_active
            return False
        finally:
            kernel32.CloseHandle(handle)
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _remove_compat_shadow_if_default() -> None:
    """R4-NM2: single-source compat-shadow removal helper.

    Removes ``<install>/.claude/chat/bot.pid`` when (and only when) the
    active profile is the default profile. Called by ``remove_pid``,
    ``cleanup_stale_pid``, AND ``cleanup_all_bot_processes`` so all three
    paths share one removal site.
    """
    if not _services._should_write_compat_shadow():
        return
    try:
        shadow = _services._compat_shadow_pid_path()
        shadow.unlink(missing_ok=True)
    except Exception:
        # Best-effort — never block on compat-shadow removal.
        pass


def cleanup_stale_pid(pid_file: Path | None = None) -> int | None:
    """Check PID file, kill stale process if alive, remove PID file.

    Returns the stale PID if one was found and killed, else None.
    """
    if pid_file is None:
        pid_file = _services.get_bot_pid_path()
    pid = read_pid(pid_file)
    if pid is None:
        return None
    if is_pid_alive(pid):
        try:
            if sys.platform == "win32":
                import subprocess

                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
        pid_file.unlink(missing_ok=True)
        _remove_compat_shadow_if_default()
        return pid
    # PID file exists but process is dead — clean up
    pid_file.unlink(missing_ok=True)
    _remove_compat_shadow_if_default()
    return None


def spawn_detached(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    log_path: Path | None = None,
    cwd: str | Path | None = None,
) -> int:
    """Spawn *cmd* as a fully detached child that survives this process's exit.

    Cross-platform detachment (mirrors the proven dashboard_bot_lifecycle
    pattern): Windows ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS``; POSIX
    ``start_new_session=True``. When *log_path* is given, stdout+stderr append to
    it (merged); otherwise both go to ``DEVNULL``. stdin is always ``DEVNULL``.
    Returns the child PID.

    Used for bot self-restart (``chat/relaunch.py``) and any background relaunch
    that must outlive its spawner. The child's std handles are dup'd by the OS,
    so the parent closes its log handle immediately after spawn.
    """
    popen_kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL}
    if env is not None:
        popen_kwargs["env"] = env
    if cwd is not None:
        popen_kwargs["cwd"] = str(cwd)
    log_handle = None
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "ab")  # noqa: SIM115 — inherited by child
        popen_kwargs["stdout"] = log_handle
        popen_kwargs["stderr"] = subprocess.STDOUT
    else:
        popen_kwargs["stdout"] = subprocess.DEVNULL
        popen_kwargs["stderr"] = subprocess.DEVNULL
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    finally:
        if log_handle is not None:
            log_handle.close()
    return proc.pid


def cleanup_all_bot_processes(pid_file: Path | None = None) -> list[int]:
    """Kill bot-related processes belonging to THIS profile.

    Unlike ``cleanup_stale_pid`` (which only kills the PID in bot.pid), this
    scans all running Python processes by command line to catch ``service.py``
    wrappers that would respawn the bot after a simple PID kill.

    R1 B1 — profile-aware filtering: only processes whose ``HOMIE_HOME`` env
    var matches the current profile's ``HOMIE_HOME`` (or where neither is set,
    indicating both are default-profile) are killed. This prevents one
    profile's startup from killing another profile's running bot.

    Uses ``psutil.Process(pid).environ()`` to read the target's HOMIE_HOME.
    psutil is a hard dependency declared in ``pyproject.toml``. The
    ``ImportError`` fallback is belt-and-suspenders only — if psutil is
    somehow unavailable, the cmdline-fallback path REFUSES to kill any
    process (safer than killing across profiles).

    For the legacy "kill all repo bots" behavior used by the operator-driven
    ``bot-status.sh --kill-all-homies`` flag, see
    ``cleanup_all_homie_bots_in_repo()``.

    Returns list of killed PIDs.
    """
    if pid_file is None:
        pid_file = _services.get_bot_pid_path()
    my_pid = os.getpid()
    my_ppid = os.getppid()
    my_homie_home = os.environ.get("HOMIE_HOME", "").strip()

    if sys.platform == "win32":
        killed = _scan_and_kill_windows(
            my_pid, my_ppid, my_homie_home, profile_aware=True
        )
    else:
        killed = _scan_and_kill_unix(
            my_pid, my_ppid, my_homie_home, profile_aware=True
        )

    # Clean up PID file regardless
    pid_file.unlink(missing_ok=True)
    _remove_compat_shadow_if_default()

    return killed


def cleanup_all_homie_bots_in_repo() -> list[int]:
    """Legacy "kill all Homie bots in this repo" behavior.

    Used ONLY by ``bot-status.sh --kill-all-homies`` (operator-driven, opt-in).
    Performs the OLD non-profile-aware cmdline scan that kills every
    ``chat/main.py``/``service.py`` Python process matching this repo's path,
    regardless of which profile spawned it.

    DO NOT call this from automatic bot startup — it will kill bots from
    other profiles. ``cleanup_all_bot_processes()`` is the profile-aware
    automatic path.
    """
    my_pid = os.getpid()
    my_ppid = os.getppid()
    if sys.platform == "win32":
        return _scan_and_kill_windows(my_pid, my_ppid, "", profile_aware=False)
    return _scan_and_kill_unix(my_pid, my_ppid, "", profile_aware=False)


def list_bot_pids_in_active_profile() -> list[int]:
    """Return PIDs of bot processes that belong to the ACTIVE profile.

    Phase 3 F1 fix — replaces the cmdline-only repo-path scan in ``run_chat.sh``
    and ``bot-status.sh`` with a Python helper that uses ``psutil.environ()`` to
    read each candidate process's ``HOMIE_HOME`` and filters by exact match
    against the active profile's ``HOMIE_HOME``. This is the same ownership
    check ``cleanup_all_bot_processes()`` uses (Rule 2 — physical state, not
    sidecar registry).

    FAIL-CLOSED: when ownership cannot be proven (psutil missing, environ
    unreadable, or the process exits between scan and read), the helper
    EXCLUDES the PID from the returned list. The shell scripts treat this as
    "no matching bot" rather than "kill the unknown PID" — killing across
    profiles is the larger evil.

    Returns the list of PIDs (current process and its parent are filtered
    out so the caller never sees its own PID).
    """
    my_pid = os.getpid()
    my_ppid = os.getppid()
    my_homie_home = os.environ.get("HOMIE_HOME", "").strip()

    candidate_pids = _enumerate_bot_candidate_pids()
    matching: list[int] = []
    for pid in candidate_pids:
        if pid in (my_pid, my_ppid):
            continue
        # Rule 2 — verify ownership by reading the target's HOMIE_HOME via
        # psutil. _process_belongs_to_profile() is the canonical helper and
        # already fails closed on psutil ImportError / AccessDenied.
        if _process_belongs_to_profile(pid, my_homie_home):
            matching.append(pid)
    return matching


def _line_is_this_repos_bot(line: str) -> bool:
    """True iff a process cmdline belongs to THIS repo's bot.

    Generic substrings ("service.py", "chat/main.py") can match an UNRELATED
    project's python process, and when both processes lack HOMIE_HOME the
    profile check treats them as same-profile default — a concrete
    foreign-process kill path (#135 gate finding). Anchor on this repo's
    resolved absolute paths instead, both slash flavors (wmic prints
    backslashes; Git Bash / ps print forward slashes). service.py is retired
    (archived 2026-07) but a stale pre-retirement instance still carries the
    repo-anchored path, so it stays killable by full path only.
    """
    chat_dir = (Path(__file__).resolve().parent.parent / "chat").resolve()
    for rel in ("main.py", "service.py"):
        p = str(chat_dir / rel)
        if p in line or p.replace("\\", "/") in line:
            return True
    return False


def _enumerate_bot_candidate_pids() -> list[int]:
    """Return PIDs of every python process running this repo's ``chat/main.py``.

    Used by ``list_bot_pids_in_active_profile()`` to gather the candidate set
    BEFORE filtering by HOMIE_HOME ownership. Also deduplicates the venv
    shim+child pair on Windows (parent_pids in cmdline output) so one logical
    bot is reported once, not twice.

    psutil-only path (no shell-out, no powershell.exe): keeps the helper
    importable from any context the bash scripts spawn it in.
    """
    try:
        import psutil
    except ImportError:
        return []
    # Resolve this repo's chat/main.py absolute path so a sibling repo's bot
    # never lands in the candidate set.
    chat_main = (Path(__file__).resolve().parent.parent / "chat" / "main.py").resolve()
    chat_main_str = str(chat_main)
    chat_main_alt = chat_main_str.replace("\\", "/")
    pids: list[int] = []
    parent_pids: set[int] = set()
    try:
        procs = list(psutil.process_iter(["pid", "ppid", "name", "cmdline"]))
    except Exception:
        return []
    for proc in procs:
        try:
            info = proc.info
            cmdline_list = info.get("cmdline") or []
            if not cmdline_list:
                continue
            cmdline = " ".join(cmdline_list)
            # Match either separator style (Windows shows backslashes; Git Bash
            # shows forward slashes); compare against the resolved absolute path
            # so cross-repo bots are excluded.
            if chat_main_str not in cmdline and chat_main_alt not in cmdline:
                continue
            name = (info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            pids.append(int(info["pid"]))
            ppid = info.get("ppid")
            if isinstance(ppid, int):
                parent_pids.add(ppid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            continue
    # Windows venv shim spawns a child python.exe — both have chat/main.py in
    # cmdline. Deduplicate by removing PIDs that are parents of another match
    # in the set (the child is the real bot; the shim is the parent).
    deduped = [pid for pid in pids if pid not in parent_pids]
    return deduped


def _process_belongs_to_profile(pid: int, my_homie_home: str) -> bool:
    """Return True iff *pid*'s ``HOMIE_HOME`` matches *my_homie_home*.

    Uses psutil to read the target process's environment. Belt-and-suspenders
    fallback: if psutil cannot import or the environment cannot be read,
    REFUSES the kill (returns False) — safer than killing across profiles.
    """
    try:
        import psutil
    except ImportError:
        # Belt-and-suspenders: refuse to kill when we cannot verify ownership.
        return False
    try:
        env = psutil.Process(pid).environ()
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return False
    other_home = (env.get("HOMIE_HOME") or "").strip()
    return other_home == my_homie_home


def _scan_and_kill_windows(
    my_pid: int,
    my_ppid: int,
    my_homie_home: str,
    *,
    profile_aware: bool,
) -> list[int]:
    """Scan and kill bot processes on Windows using wmic.

    When *profile_aware* is True (automatic startup path), only kills processes
    whose ``HOMIE_HOME`` env var matches *my_homie_home*. When False (legacy
    ``--kill-all-homies`` path), kills every match regardless of profile.
    """
    import subprocess as _sp

    killed: list[int] = []
    try:
        result = _sp.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "processid,commandline"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            # Repo-anchored match only — a foreign project's service.py/main.py
            # must never be a kill candidate (#135 gate finding).
            if not _line_is_this_repos_bot(line):
                continue
            # Extract PID (last number on the line)
            parts = line.split()
            try:
                pid = int(parts[-1])
            except (ValueError, IndexError):
                continue
            if pid in (my_pid, my_ppid):
                continue
            # R1 B1: profile-aware filter — only kill processes belonging to
            # the same profile (same HOMIE_HOME).
            if profile_aware and not _process_belongs_to_profile(pid, my_homie_home):
                continue
            # Kill it — service.py first (it's the parent), but order doesn't
            # matter much since we force-kill after 5s anyway
            try:
                _sp.run(["taskkill", "/PID", str(pid)], capture_output=True, timeout=5)
                # Wait up to 5s for graceful exit
                for _ in range(10):
                    time.sleep(0.5)
                    if not is_pid_alive(pid):
                        break
                else:
                    # Force kill if still alive
                    _sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
                killed.append(pid)
            except Exception:
                # Force kill as fallback
                try:
                    _sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=5)
                    killed.append(pid)
                except Exception:
                    pass
    except Exception:
        pass
    return killed


def _scan_and_kill_unix(
    my_pid: int,
    my_ppid: int,
    my_homie_home: str,
    *,
    profile_aware: bool,
) -> list[int]:
    """Scan and kill bot processes on Unix using ps.

    When *profile_aware* is True, only kills processes whose ``HOMIE_HOME``
    env matches *my_homie_home*. See ``_scan_and_kill_windows`` docstring.
    """
    import subprocess as _sp

    killed: list[int] = []
    try:
        result = _sp.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            # Repo-anchored match only — a foreign project's service.py/main.py
            # must never be a kill candidate (#135 gate finding).
            if not _line_is_this_repos_bot(line):
                continue
            parts = line.split()
            try:
                pid = int(parts[1])
            except (ValueError, IndexError):
                continue
            if pid in (my_pid, my_ppid):
                continue
            # R1 B1: profile-aware filter.
            if profile_aware and not _process_belongs_to_profile(pid, my_homie_home):
                continue
            try:
                os.kill(pid, signal.SIGINT)
                # Wait up to 5s for graceful exit
                for _ in range(10):
                    time.sleep(0.5)
                    if not is_pid_alive(pid):
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass
    return killed


def remove_pid(pid_file: Path | None = None) -> None:
    """Remove PID file (called on clean shutdown)."""
    if pid_file is None:
        pid_file = _services.get_bot_pid_path()
    pid_file.unlink(missing_ok=True)
    _remove_compat_shadow_if_default()


# =============================================================================
# LANE INDEX (auto-generated signal lanes: github-signal, upstream scouts, ...)
# =============================================================================
#
# Pipelines that write dated vault notes without inbound links create orphan
# strata that recall's graph hub-boost never surfaces (one lane had piled up
# 77 orphaned notes before this existed). Each lane keeps ONE index note the
# generator regenerates from a directory scan after every write — drift-proof,
# idempotent, and it backfills the whole lane on first run.

_FRONTMATTER_SCAN_LINES = 40


def _parse_scalar_frontmatter(path: Path) -> dict[str, str]:
    """Tolerant scalar-only frontmatter reader (no YAML dependency).

    Returns {key: value} for simple `key: value` lines inside the first
    frontmatter block. List/nested values are skipped; quotes are stripped.
    Never raises — unreadable files return {}.
    """
    fields: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            first = f.readline()
            if first.strip() != "---":
                return fields
            for _ in range(_FRONTMATTER_SCAN_LINES):
                line = f.readline()
                if not line or line.strip() == "---":
                    break
                if line.startswith((" ", "\t", "-")) or ":" not in line:
                    continue
                key, _, value = line.partition(":")
                value = value.strip()
                if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                    # json.dumps-style frontmatter (video dossiers): unescape
                    # so index cells don't render literal backslash escapes.
                    value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                else:
                    value = value.strip("\"'")
                if value and not value.startswith("["):
                    fields[key.strip()] = value
    except OSError:
        return fields
    return fields


def regenerate_lane_index(
    lane_dir: Path,
    index_name: str,
    title: str,
    description: str,
    sections: list[dict[str, Any]],
    moc_link: str = "MOC-thehomie",
) -> Path | None:
    """Regenerate a lane's index note from a directory scan.

    ``sections``: [{"heading": str, "glob": str, "subdir": str | None,
    "columns": [(label, frontmatter_key), ...]}]. Every matching note gets a
    table row starting with its wikilink — the inbound edge that keeps lane
    notes out of the orphan pile. Rows sort newest-first by frontmatter
    ``date`` (stem as tiebreaker). Returns the index path, or None when the
    lane dir does not exist.
    """
    if not lane_dir.exists():
        return None
    index_path = lane_dir / index_name
    today = now_local().strftime("%Y-%m-%d")

    body_parts: list[str] = [
        f"# {title}\n",
        "_Auto-generated lane index — regenerated by the lane's generator on"
        " every run. Do not edit by hand._\n",
    ]
    total_rows = 0
    for section in sections:
        scan_dir = lane_dir / section["subdir"] if section.get("subdir") else lane_dir
        columns: list[tuple[str, str]] = list(section.get("columns", []))
        notes = []
        if scan_dir.exists():
            for note in scan_dir.glob(section["glob"]):
                if note.name == index_name or not note.is_file():
                    continue
                fm = _parse_scalar_frontmatter(note)
                notes.append((fm.get("date", ""), note.stem, fm))
        notes.sort(key=lambda item: (item[0], item[1]), reverse=True)
        total_rows += len(notes)

        body_parts.append(f"\n## {section['heading']} ({len(notes)})\n")
        if not notes:
            body_parts.append("_None yet._\n")
            continue
        header = "| Note | " + " | ".join(label for label, _ in columns) + " |"
        divider = "|---" * (len(columns) + 1) + "|"
        rows = [header, divider]
        for _, stem, fm in notes:
            cells = [f"[[{stem}]]"]
            for _, key in columns:
                cells.append(fm.get(key, "").replace("|", "/"))
            rows.append("| " + " | ".join(cells) + " |")
        body_parts.append("\n".join(rows) + "\n")

    content = (
        "---\n"
        "tags: [system, auto-compiled]\n"
        "status: current\n"
        f"date: {today}\n"
        f"summary: \"{description}\"\n"
        "related:\n"
        f"  - \"[[{moc_link}]]\"\n"
        "---\n\n" + "\n".join(body_parts)
    )
    with file_lock(index_path, timeout=5.0):
        index_path.write_text(content, encoding="utf-8")
    return index_path
