"""Run one SEO/GEO scheduled job and post its local receipt to Discord.

The wrapper gives every scheduled job the same failure semantics: it posts only
after the child exits and only treats a receipt as fresh when its contents or
mtime changed during this invocation.  A stale receipt can never be reported as
a successful new run.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

from seo_geo_discord_notify import notify


SCRIPTS_DIR = Path(__file__).resolve().parent
PROFILE_ROOT = Path.home() / ".homie" / "profiles" / "seo_geo"

JOB_COMMANDS: dict[str, tuple[list[str], Path]] = {
    "daily": (
        [str(SCRIPTS_DIR / "seo_geo_fleet_pulse.py")],
        PROFILE_ROOT / "data" / "fleet-pulse" / "latest.json",
    ),
    "weekly": (
        [str(SCRIPTS_DIR / "seo_geo_control_review.py"), "--mode", "weekly"],
        PROFILE_ROOT / "data" / "fleet-control" / "weekly-latest.json",
    ),
    "monthly": (
        [str(SCRIPTS_DIR / "seo_geo_control_review.py"), "--mode", "monthly"],
        PROFILE_ROOT / "data" / "fleet-control" / "monthly-latest.json",
    ),
    "paid": (
        [
            str(SCRIPTS_DIR / "seo_geo_paid_research.py"),
            "--mode",
            "production",
            "--timeout-seconds",
            "180",
        ],
        PROFILE_ROOT / "data" / "fleet-paid-research" / "latest.json",
    ),
}


def _stamp(path: Path) -> tuple[int, str] | None:
    try:
        return path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def run_job(*, job: str, dry_run: bool = False) -> tuple[int, dict[str, Any]]:
    command_tail, receipt_path = JOB_COMMANDS[job]
    before = _stamp(receipt_path)
    return_code = 1
    failure_reason: str | None = None
    if dry_run:
        return_code = 0
    else:
        try:
            child = subprocess.run(
                [sys.executable, *command_tail],
                cwd=SCRIPTS_DIR,
                text=True,
                capture_output=True,
                timeout=360,
                check=False,
            )
            return_code = child.returncode
        except subprocess.TimeoutExpired:
            failure_reason = "child timeout after 360 seconds"
        except OSError as exc:
            failure_reason = f"child failed to start ({type(exc).__name__})"
    after = _stamp(receipt_path)
    fresh = after is not None and after != before
    if dry_run:
        status = "dry_run"
    else:
        if failure_reason is None and not fresh:
            failure_reason = "no fresh local receipt was written"
        status = "completed" if return_code == 0 and fresh else "failed"
    notification = notify(
        job=job,
        status=status,
        receipt_path=receipt_path if fresh else None,
        exit_code=return_code,
        failure_reason=failure_reason,
        dry_run=dry_run,
    )
    print(f"SEO_GEO_JOB={job}")
    print(f"RECEIPT_FRESH={str(fresh).lower()}")
    print(f"DISCORD_NOTIFICATION_STATUS={notification['delivery']['status']}")
    if notification["delivery"].get("message_id"):
        print(f"DISCORD_MESSAGE_ID={notification['delivery']['message_id']}")
    if status == "dry_run":
        return 0, notification
    if status == "completed":
        # Receipt generation without delivery is not a completed operator loop.
        # Surface it in Task Scheduler rather than silently waiting for the
        # next human receipt review to discover that Discord was unavailable.
        return (return_code if notification["delivery"]["status"] == "delivered" else 3), notification
    return return_code or 1, notification


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a scheduled SEO/GEO job and notify Discord.")
    parser.add_argument("--job", choices=tuple(JOB_COMMANDS), required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    code, _ = run_job(job=args.job, dry_run=args.dry_run)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
