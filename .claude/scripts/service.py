"""
Service wrapper for The Homie bot.

Spawns the chat bot as a subprocess, monitors for crashes, and auto-restarts
with exponential backoff. Enforces max 5 restarts per rolling hour.

Usage:
    cd .claude/scripts && uv run python service.py
    cd .claude/scripts && uv run python service.py --dry-run
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

# Paths
SCRIPTS_DIR = Path(__file__).resolve().parent
CHAT_DIR = SCRIPTS_DIR.parent / "chat"
BOT_SCRIPT = CHAT_DIR / "main.py"

# Ensure scripts dir is on path for shared/notifications imports
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Boot-shim: must run BEFORE any framework imports (config, runtime, etc.)
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

from shared import append_to_daily_log  # noqa: E402


def _resolve_stop_file() -> Path:
    """PRP-7c Phase 3: resolve the profile-aware service-stop sentinel path.

    Default profile keeps the legacy ``<install>/data/state/service-stop``
    location (matches PRD §8.2 — default profile preserves install-dir
    paths). Named/custom profiles get ``<profile_root>/state/service-stop``.

    Resolves on every call (Anti-pattern Rule 1 — never bind at def time
    to ``personas.X`` because tests / live-profile-swap would not see the
    swap. The PRP-7a deferred ``_DEFERRED_VIOLATIONS`` entry is removed by
    this refactor.).
    """
    from personas import get_persona_paths
    from personas.activity import get_active_profile_name
    paths = get_persona_paths(get_active_profile_name())
    return paths["state"] / "service-stop"


def __getattr__(name: str) -> Any:
    """PEP 562 lazy lookup so existing ``from service import STOP_FILE`` /
    ``service.STOP_FILE`` callers keep working — resolution happens at
    attribute access time and follows the active profile.
    """
    if name == "STOP_FILE":
        return _resolve_stop_file()
    raise AttributeError(f"module 'service' has no attribute {name!r}")

try:
    # NOTE: the symbol is ``send_toast_notification`` — there is no bare
    # ``send_notification`` in notifications.py, so the old
    # ``from notifications import send_notification`` ALWAYS raised ImportError
    # and left this None. The ``if send_notification:`` guard below then
    # silently skipped every "bot down" alert this supervisor ever tried to
    # send. Same silent-monitoring class as the wedge itself: the alarm was
    # wired to nothing.
    from notifications import send_toast_notification as send_notification  # noqa: E402
except ImportError:
    send_notification = None  # type: ignore[assignment]

# Restart policy
MAX_RESTARTS_PER_HOUR = 5
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 120


def run_service(dry_run: bool = False) -> None:
    # Clear any stale stop file from previous session
    stop_file = _resolve_stop_file()
    if stop_file.exists():
        stop_file.unlink()

    restart_times: deque[float] = deque()  # timestamps of recent restarts
    backoff = BASE_BACKOFF_SECONDS

    while True:
        # Prune restart times older than 1 hour
        now = time.time()
        while restart_times and (now - restart_times[0]) > 3600:
            restart_times.popleft()

        # Check restart budget
        if len(restart_times) >= MAX_RESTARTS_PER_HOUR:
            msg = (
                f"[{datetime.now()}] Max restarts ({MAX_RESTARTS_PER_HOUR}/hour) "
                f"exceeded. Stopping service wrapper."
            )
            print(msg)
            try:
                append_to_daily_log(
                    f"Bot service stopped — {MAX_RESTARTS_PER_HOUR} restarts in 1 hour. "
                    f"Check for config errors.\n\nPriority: HIGH",
                    "Bot Lifecycle",
                )
                if send_notification:
                    send_notification(
                        "The Homie Bot Down",
                        f"Bot crashed {MAX_RESTARTS_PER_HOUR} times in 1 hour. Service stopped.",
                    )
            except Exception:
                pass
            sys.exit(1)

        # Start bot
        print(f"[{datetime.now()}] Starting bot...")
        if dry_run:
            print("  (dry run — would start bot here)")
            return

        start_time = time.time()
        try:
            proc = subprocess.Popen(
                [sys.executable, str(BOT_SCRIPT)],
                cwd=str(SCRIPTS_DIR),
            )

            # Forward SIGTERM to child so it can shut down cleanly
            def _forward_signal(signum: int, frame: object) -> None:
                proc.send_signal(signum)
                proc.wait(timeout=15)
                sys.exit(0)

            signal.signal(signal.SIGTERM, _forward_signal)

            exit_code = proc.wait()

            # Restore default signal handling between restarts
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

        except KeyboardInterrupt:
            # Forward Ctrl+C to child and wait for it
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=15)
            print(f"\n[{datetime.now()}] Service wrapper interrupted. Exiting.")
            return
        except Exception as e:
            print(f"[{datetime.now()}] Failed to start bot: {e}")
            exit_code = 1

        elapsed = time.time() - start_time

        # Clean exit (code 0) — don't restart
        if exit_code == 0:
            print(f"[{datetime.now()}] Bot exited cleanly. Not restarting.")
            return

        # Check for intentional stop signal
        if stop_file.exists():
            stop_file.unlink()
            print(f"[{datetime.now()}] Stop file detected — intentional stop. Not restarting.")
            try:
                append_to_daily_log("Bot stopped intentionally (stop file).", "Bot Lifecycle")
            except Exception:
                pass
            return

        # Crash detected — compute new backoff BEFORE logging
        restart_times.append(time.time())

        if elapsed > 300:
            backoff = BASE_BACKOFF_SECONDS
        else:
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

        print(f"[{datetime.now()}] Bot crashed (exit code {exit_code}) after {elapsed:.0f}s")

        try:
            append_to_daily_log(
                f"Bot crashed (exit code {exit_code}) after {elapsed:.0f}s. "
                f"Restarting in {backoff}s... "
                f"({len(restart_times)}/{MAX_RESTARTS_PER_HOUR} restarts this hour)",
                "Bot Lifecycle",
            )
        except Exception:
            pass

        print(f"[{datetime.now()}] Restarting in {backoff}s...")
        time.sleep(backoff)


def stop_service() -> None:
    """Create stop file so the service wrapper exits after the bot dies."""
    stop_file = _resolve_stop_file()
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text(str(datetime.now()))
    print(f"Stop file created at {stop_file}")
    print("Now kill the bot process — the service wrapper will NOT restart it.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="The Homie Bot Service Wrapper")
    parser.add_argument("--dry-run", action="store_true", help="Validate config and exit")
    parser.add_argument("--stop", action="store_true", help="Signal bot to stop (no auto-restart)")
    args = parser.parse_args()
    if args.stop:
        stop_service()
    else:
        run_service(dry_run=args.dry_run)
