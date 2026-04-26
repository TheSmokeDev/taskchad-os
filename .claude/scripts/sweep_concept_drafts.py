"""Daily sweep of expired concept drafts (gap-6 conversational compounding).

Heartbeat-pattern entrypoint. OS-scheduled — wire via Task Scheduler /
cron. Inline ``_maybe_inline_sweep`` in ``concept_drafter.create_draft``
covers long-running processes; this standalone covers the daily cadence.

Usage:
    uv run python sweep_concept_drafts.py
"""

from __future__ import annotations

import io
import sys

# Force UTF-8 stdout/stderr on Windows for vault path safety.
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr.encoding != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from config import MEMORY_DIR
from concept_drafter import sweep_expired


def main() -> int:
    removed = sweep_expired(MEMORY_DIR)
    print(f"[sweep] removed {len(removed)} expired drafts", flush=True)
    for path in removed:
        print(f"[sweep]   - {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
