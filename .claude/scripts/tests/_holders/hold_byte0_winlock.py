"""R4 NNNM1 — Windows-only byte-0 lock holder that does NOT truncate.

Opens an ALREADY-suffixed ``.lock`` file with mode ``"r+"`` (no truncate,
file pointer at byte 0) and locks byte 0 directly via ``msvcrt.locking``,
preserving any stale content. This is the dedicated regression test for
the byte-0 vs EOF probe trap.

Why a separate helper instead of reusing ``hold_state_lock.py``:
    ``shared.file_lock(target)`` opens ``target.with_suffix(...".lock")``
    with mode ``"w"`` — that TRUNCATES the lock file to 0 bytes BEFORE
    locking. The byte-0 trap regression test needs the lock file to remain
    NON-EMPTY while byte 0 is held, which ``shared.file_lock`` cannot
    provide. So this helper bypasses ``shared`` entirely and uses raw
    ``msvcrt.locking`` semantics.

Protocol:
    parent -> spawns:  python hold_byte0_winlock.py <lock_path>
              (where <lock_path> is the already-suffixed .lock file with
               stale bytes already written by the parent)
    child  -> prints:  "LOCKED-BYTE0\\n" once byte 0 is locked (flushed)
    child  -> waits on stdin EOF, then unlocks + exits 0
    On POSIX -> prints "SKIP-NON-WINDOWS" and exits 0 (no-op).
"""
from __future__ import annotations

import sys
from pathlib import Path


if __name__ == "__main__":
    if sys.platform != "win32":
        print("SKIP-NON-WINDOWS", flush=True)
        sys.exit(0)

    if len(sys.argv) != 2:
        print("USAGE: hold_byte0_winlock.py <lock_path>", file=sys.stderr)
        sys.exit(2)

    lock_path = Path(sys.argv[1])

    # NO `import shared` — uses raw msvcrt only (avoids shared.file_lock's
    # `"w"` mode which would truncate any stale content).
    import msvcrt

    # "r+" = read+write, no truncate, file pointer at byte 0 by default.
    fd = lock_path.open("r+", encoding="utf-8")
    raw_fd = fd.fileno()
    try:
        # Lock 1 byte at current FP (=byte 0 because "r+" did not seek).
        msvcrt.locking(raw_fd, msvcrt.LK_NBLCK, 1)
        sys.stdout.write("LOCKED-BYTE0\n")
        sys.stdout.flush()
        try:
            sys.stdin.read()
        except (KeyboardInterrupt, BrokenPipeError):
            pass
    finally:
        try:
            msvcrt.locking(raw_fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        fd.close()
