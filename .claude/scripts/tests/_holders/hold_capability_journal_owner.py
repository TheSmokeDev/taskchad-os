"""Subprocess helper that holds a capability lifecycle journal owner lock."""

from __future__ import annotations

import sys
from pathlib import Path

from runtime.capability_plugin_journal import LockedLifecycleJournal


def main() -> int:
    journal = LockedLifecycleJournal(Path(sys.argv[1]))
    print("READY", flush=True)
    sys.stdin.readline()
    journal.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
