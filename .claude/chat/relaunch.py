"""Detached bot relauncher — makes ``/restart`` actually restart.

A process can't reliably restart itself: killing itself leaves nothing to bring
it back. So ``core_handlers.handle_restart`` spawns THIS script detached (it
survives the old bot's death), then the old bot exits. This script then performs
a deterministic handoff — wait for the old bot to die, force-kill any straggler,
spawn a fresh bot — the same shape ``run_chat.sh`` uses, but in pure Python:

  * no ``bash`` dependency (the old handler shelled out to ``bash run_chat.sh``,
    which fails when bash isn't on the Windows bot's PATH);
  * the Claude-Code nesting markers are scrubbed for the child via
    ``runtime.subprocess_env.scrub_nested_claude_state`` so the relaunched bot's
    Agent SDK does not refuse to start ("cannot be launched inside another
    Claude Code session");
  * the active profile is preserved (``HOMIE_HOME`` flows through unchanged).

Run standalone to test a live restart (safe even from inside a Claude Code
session — markers are scrubbed for the child):

    python .claude/chat/relaunch.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

# Boot-shim: add chat + scripts dirs to path BEFORE framework imports, then
# resolve the active profile from the inherited HOMIE_HOME (mirrors main.py).
_CHAT_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _CHAT_DIR.parent / "scripts"
for _p in (str(_CHAT_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from personas import apply_persona_override  # noqa: E402

apply_persona_override()

from personas import services as _services  # noqa: E402
from runtime.subprocess_env import scrub_nested_claude_state  # noqa: E402
from shared import (  # noqa: E402
    cleanup_all_bot_processes,
    list_bot_pids_in_active_profile,
    spawn_detached,
)

# Wait this long for the OLD bot to finish its clean exit before forcing it.
_WAIT_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.5


def _wait_for_old_bot_exit() -> None:
    """Poll until no ``main.py`` bot of this profile is alive, or timeout."""
    deadline = time.monotonic() + _WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if not list_bot_pids_in_active_profile():
            return
        time.sleep(_POLL_INTERVAL_S)


def relaunch() -> int:
    """Wait for the old bot to die, kill stragglers, spawn a fresh bot.

    Returns the new bot PID.
    """
    main_py = _CHAT_DIR / "main.py"

    # 1. Let the old bot finish its clean exit (it os._exit's right after
    #    spawning us). Deterministic handoff — the new bot's mutex-first acquire
    #    must not race a live old holder.
    _wait_for_old_bot_exit()

    # 2. Belt: force-kill any straggler of THIS profile + unlink the stale pid
    #    file (os._exit on the old bot skips its atexit pid cleanup).
    cleanup_all_bot_processes()

    # 3. Spawn the fresh bot, detached, with nesting markers scrubbed (so the
    #    Agent SDK launches) and HOMIE_HOME preserved (same profile). Logs append
    #    to the profile-aware bot.log; main.py writes its own pid on startup.
    log_path = _services.get_log_dir() / "bot.log"
    child_env = scrub_nested_claude_state()
    child_env["PYTHONUNBUFFERED"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    venv_python = _SCRIPTS_DIR / ".venv" / "Scripts" / "python.exe"
    runtime_python = str(venv_python if venv_python.is_file() else Path(sys.executable))
    new_pid = spawn_detached(
        [runtime_python, str(main_py)],
        env=child_env,
        log_path=log_path,
        cwd=str(_SCRIPTS_DIR),
    )
    print(f"[relaunch] spawned fresh bot PID {new_pid} (log: {log_path})")
    return new_pid


if __name__ == "__main__":
    relaunch()
