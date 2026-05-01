"""R2 NB1 + R3 NNB1 + R3 NNB2 — subprocess helper that holds a real
``shared.file_lock`` open until parent test signals release.

Consumed by:
    - ``tests/test_persona_quiesce_before_delete.py::test_step5_subprocess_holds_real_state_lock``

Critical (R3 NNB2): the child python process does NOT execute pytest's
conftest.py, so we must insert ``.claude/scripts/`` into ``sys.path`` BEFORE
``import shared``. The pytest parent injects this via conftest.py:10-13, but
the subprocess only sees what it wires up itself.

Critical (R3 NNB1): the helper writes nothing to the lock file. The real
``shared.file_lock`` opens with mode ``"w"`` which truncates the file to 0
bytes — the byte-0 probe semantics on Windows depend on this shape. This
helper matches that behavior (does NOT write any content into the lock file).

Protocol:
    parent -> spawns:  python hold_state_lock.py <state_file_path>
    child  -> prints:  "LOCKED\\n" once the lock is acquired (flushed)
    parent -> reads stdout line == "LOCKED" -> proceeds with assertions
    parent -> closes child's stdin -> child reads EOF -> releases lock
              and exits cleanly with code 0
"""
from __future__ import annotations

import sys
from pathlib import Path

# R3 NNB2 — insert .claude/scripts/ into sys.path BEFORE `import shared`.
# parents[0]=_holders, parents[1]=tests, parents[2]=scripts.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import shared  # noqa: E402 — must come after sys.path mutation


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("USAGE: hold_state_lock.py <state_file_path>", file=sys.stderr)
        sys.exit(2)

    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)

    with shared.file_lock(target):
        # Signal acquisition — flush so the parent's readline() returns
        # immediately. Do NOT write anything else (R3 NNB1 byte-0 contract).
        sys.stdout.write("LOCKED\n")
        sys.stdout.flush()
        # Block until parent closes stdin (EOF == "release").
        try:
            sys.stdin.read()
        except (KeyboardInterrupt, BrokenPipeError):
            pass
