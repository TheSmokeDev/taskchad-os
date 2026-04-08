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

# Paths
SCRIPTS_DIR = Path(__file__).resolve().parent
CHAT_DIR = SCRIPTS_DIR.parent / "chat"
BOT_SCRIPT = CHAT_DIR / "main.py"
STOP_FILE = SCRIPTS_DIR.parent / "data" / "state" / "service-stop"

# Ensure scripts dir is on path for shared/notifications imports
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from shared import append_to_daily_log  # noqa: E402

try:
    from notifications import send_notification  # noqa: E402
except ImportError:
    send_notification = None  # type: ignore[assignment]

# Restart policy
MAX_RESTARTS_PER_HOUR = 5
BASE_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 120


def run_service(dry_run: bool = False) -> None:
    # Clear any stale stop file from previous session
    if STOP_FILE.exists():
        STOP_FILE.unlink()

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
        if STOP_FILE.exists():
            STOP_FILE.unlink()
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
    STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    STOP_FILE.write_text(str(datetime.now()))
    print(f"Stop file created at {STOP_FILE}")
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
