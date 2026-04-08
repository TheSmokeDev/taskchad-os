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
    """Save state to a JSON file."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


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


# =============================================================================
# PID FILE MANAGEMENT
# =============================================================================

BOT_PID_FILE = STATE_DIR / "bot.pid"


def write_pid(pid_file: Path = BOT_PID_FILE) -> None:
    """Write current process PID to file."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")


def read_pid(pid_file: Path = BOT_PID_FILE) -> int | None:
    """Read PID from file, return None if missing/invalid."""
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


def cleanup_stale_pid(pid_file: Path = BOT_PID_FILE) -> int | None:
    """Check PID file, kill stale process if alive, remove PID file.

    Returns the stale PID if one was found and killed, else None.
    """
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
        return pid
    # PID file exists but process is dead — clean up
    pid_file.unlink(missing_ok=True)
    return None


def cleanup_all_bot_processes(pid_file: Path = BOT_PID_FILE) -> list[int]:
    """Kill ALL bot-related processes — service.py wrappers and chat/main.py instances.

    Unlike cleanup_stale_pid (which only kills the PID in bot.pid), this scans
    all running Python processes by command line to catch service.py wrappers
    that would respawn the bot after a simple PID kill.

    Returns list of killed PIDs.
    """
    my_pid = os.getpid()
    my_ppid = os.getppid()
    killed: list[int] = []

    if sys.platform == "win32":
        killed = _scan_and_kill_windows(my_pid, my_ppid)
    else:
        killed = _scan_and_kill_unix(my_pid, my_ppid)

    # Clean up PID file regardless
    pid_file.unlink(missing_ok=True)

    return killed


def _scan_and_kill_windows(my_pid: int, my_ppid: int) -> list[int]:
    """Scan and kill bot processes on Windows using wmic."""
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
            # Match bot processes: chat/main.py or chat\main.py
            is_bot = "chat/main.py" in line or "chat\\main.py" in line
            is_service = "service.py" in line
            if not (is_bot or is_service):
                continue
            # Extract PID (last number on the line)
            parts = line.split()
            try:
                pid = int(parts[-1])
            except (ValueError, IndexError):
                continue
            if pid in (my_pid, my_ppid):
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


def _scan_and_kill_unix(my_pid: int, my_ppid: int) -> list[int]:
    """Scan and kill bot processes on Unix using ps."""
    import subprocess as _sp

    killed: list[int] = []
    try:
        result = _sp.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            is_bot = "chat/main.py" in line or "chat\\main.py" in line
            is_service = "service.py" in line
            if not (is_bot or is_service):
                continue
            parts = line.split()
            try:
                pid = int(parts[1])
            except (ValueError, IndexError):
                continue
            if pid in (my_pid, my_ppid):
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


def remove_pid(pid_file: Path = BOT_PID_FILE) -> None:
    """Remove PID file (called on clean shutdown)."""
    pid_file.unlink(missing_ok=True)
