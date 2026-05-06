"""PRP-7c Phase 3 / WS4 — live-chat concurrent-write stress test.

1000 messages × 4 concurrent senders = 4000 messages total. Verifies the
WS3 cross-repo fix: ``send()`` wraps its JSONL append in
``_file_lock()`` so concurrent multi-process writes do not interleave
mid-line.

R2 NM1 split — module-level pytestmark BEFORE the module-level skip so
``pytest --collect-only -m live_chat_acceptance`` still lists this test
even when the live-chat repo is absent. Module-level skip kicks in after
collection so unrelated dev runs (``pytest tests/``) don't fail when the
WS3 repo is checked-out elsewhere.

Windows-spawn-safe — the worker function is a top-level module function
(NOT a closure or a fixture-defined inner function), so multiprocessing
can pickle it for the spawn-based child interpreter.
"""

from __future__ import annotations

import importlib
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest


# R3 NM1 — module-level marker MUST come BEFORE the module-level skip so
# `pytest --collect-only -m live_chat_acceptance` registers this test
# even when the live-chat repo is missing.
pytestmark = pytest.mark.live_chat_acceptance


_LIVE_CHAT_REPO = Path("~/.claude/live-chat").expanduser()
_LIVE_CHAT_PY = _LIVE_CHAT_REPO / "live_chat.py"

if not _LIVE_CHAT_PY.exists():
    pytest.skip(
        "Live-chat repo missing at ~/.claude/live-chat/ — preflight test "
        "fails (test_live_chat_acceptance_preflight). Stress test skipped "
        "at module load.",
        allow_module_level=True,
    )


# Make the live-chat repo importable for both this process and any child
# processes spawned by ProcessPoolExecutor. The path goes through
# Path.expanduser() before insert so child interpreters resolve
# correctly.
sys.path.insert(0, str(_LIVE_CHAT_REPO))


# ---------------------------------------------------------------------------
# Top-level worker — Windows-spawn-safe (module-level function picklable)
# ---------------------------------------------------------------------------


def _send_messages(args: tuple[int, str, int]) -> int:
    """Worker: send *count* messages from *author* tagged with *worker_id*.

    Pickled by multiprocessing.spawn on Windows; must NOT be a closure or
    inner function. Returns count of messages sent (proves the worker
    finished).
    """
    worker_id, author, count = args
    # Re-import live_chat in the child process — modules are not inherited
    # under spawn-based multiprocessing on Windows.
    import live_chat  # type: ignore[import-not-found]

    importlib.reload(live_chat)
    # The CHAT_FILE override is set BEFORE submission; child processes inherit
    # it via the env var fallback OR via direct attribute set in the parent
    # before the executor was created — but spawn-based pool doesn't inherit
    # module state. We set it via an env var that the helper below reads.
    import os

    chat_file_override = os.environ.get("LIVE_CHAT_TEST_CHAT_FILE")
    if chat_file_override:
        live_chat.CHAT_FILE = Path(chat_file_override)

    for i in range(count):
        # Send a message with a deterministic body so we can verify ALL
        # messages landed (no row loss).
        live_chat.send(author, f"worker={worker_id} idx={i}")
    return count


# ---------------------------------------------------------------------------
# Stress test
# ---------------------------------------------------------------------------


def test_concurrent_writes_no_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """4 concurrent senders × 1000 messages = 4000 messages. No corruption.

    Verifies:
        * row count == 4000 (no row loss)
        * every line is valid JSON (no mid-line interleave)
        * every line has the expected ``author`` and ``message`` fields
        * no row truncation (each message body parses out cleanly)
    """
    # Custom CHAT_FILE under tmp_path to avoid polluting the real
    # ~/.claude/live-chat/live-chat.jsonl.
    chat_file = tmp_path / "live-chat-stress.jsonl"
    monkeypatch.setenv("LIVE_CHAT_TEST_CHAT_FILE", str(chat_file))

    workers = 4
    per_worker = 1000
    expected_total = workers * per_worker
    args_list = [
        (worker_id, f"worker_{worker_id}", per_worker)
        for worker_id in range(workers)
    ]

    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_send_messages, args_list))

    # Each worker must report sending its full count.
    assert results == [per_worker] * workers, (
        f"Worker results {results!r} disagree with expected "
        f"[{per_worker}]*{workers}"
    )

    # Read the JSONL and verify each line.
    assert chat_file.exists(), "CHAT_FILE not written"
    lines = chat_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == expected_total, (
        f"Row count {len(lines)} != expected {expected_total} — "
        "concurrent writes lost rows (interleave or truncation)"
    )

    # Per-line JSON validity. Schema (from live_chat.send()): ``{ts, from, msg}``.
    parsed: list[dict] = []
    for idx, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"Line {idx} is not valid JSON: {exc}\n  raw: {line[:200]!r}"
            )
        assert "from" in entry, f"Line {idx} missing 'from': {entry!r}"
        assert "msg" in entry, f"Line {idx} missing 'msg': {entry!r}"
        assert "ts" in entry, f"Line {idx} missing 'ts': {entry!r}"
        parsed.append(entry)

    # Per-author count: each worker should have written exactly per_worker rows.
    by_author: dict[str, int] = {}
    for entry in parsed:
        by_author[entry["from"]] = by_author.get(entry["from"], 0) + 1
    for worker_id in range(workers):
        author = f"worker_{worker_id}"
        assert by_author.get(author) == per_worker, (
            f"Author {author!r} sent {by_author.get(author)} messages, "
            f"expected {per_worker}"
        )

    # Spot-check that message bodies follow the expected pattern (no
    # truncation in the middle of the body).
    for entry in parsed[:50]:
        msg = entry["msg"]
        assert msg.startswith("worker=") and " idx=" in msg, (
            f"Truncated or corrupted message body: {msg!r}"
        )
