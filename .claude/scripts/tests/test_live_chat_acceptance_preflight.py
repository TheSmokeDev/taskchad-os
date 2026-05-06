"""PRP-7c Phase 3 / WS4 — live-chat preflight gate (R2 NM1).

This file is the GATE for the cross-repo live-chat lock fix (WS3). It is
marked with ``live_chat_acceptance`` so ``pytest -m live_chat_acceptance``
runs it as the entry test for the marker.

Critically, this file does NOT skip at module load. If
``~/.claude/live-chat/live_chat.py`` is absent, ``test_live_chat_repo_present``
FAILS with an explicit error message pointing at the missing repo. That
failure is the signal to the human that WS3 wasn't delivered before WS4
ran. The companion stress-test file
(``test_live_chat_concurrent_writes.py``) skips at module load when the
repo is missing, but THIS file fails so the gate is observable.

Final Phase 3 validation::

    pytest -m live_chat_acceptance -v

That command lists the preflight here AND the stress test, runs the
preflight first, and runs the 1000×4 stress only when the preflight
passed.
"""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.live_chat_acceptance


_LIVE_CHAT_REPO = Path("~/.claude/live-chat").expanduser()
_LIVE_CHAT_PY = _LIVE_CHAT_REPO / "live_chat.py"


def test_live_chat_repo_present() -> None:
    """The cross-repo live-chat fix must be checked out before WS4 runs.

    PRP-7c §"Compatibility Shadow Rule" / WS3 contract: the orchestrator
    commits the live-chat fix in ``~/.claude/live-chat/`` BEFORE the
    thehomie Phase 3 commit. If this assertion fails, the live-chat
    repo is missing and the stress test cannot run — Phase 3 is not
    deliverable.
    """
    assert _LIVE_CHAT_PY.exists(), (
        "Live-chat repo missing — Phase 3 cross-repo lock fix not "
        f"delivered. Check out ~/.claude/live-chat/ and apply commit bb300a4. "
        f"Expected file: {_LIVE_CHAT_PY}"
    )


def test_live_chat_send_helper_has_file_lock() -> None:
    """Confirm WS3's ``_file_lock`` helper landed in live_chat.py.

    Reads the file as text — the smoke check verifies both the
    ``_file_lock`` definition is present AND that ``send()`` calls it.
    Without this, the 1000×4 stress test would still pass occasionally
    on lucky scheduling but corrupt the JSONL most of the time.
    """
    if not _LIVE_CHAT_PY.exists():
        pytest.skip("live-chat repo missing — gate failure handled by preflight")

    src = _LIVE_CHAT_PY.read_text(encoding="utf-8")
    assert "def _file_lock" in src, (
        "WS3 helper ``_file_lock`` not found in live_chat.py — the lock "
        "fix did not land. Apply commit bb300a4 in the live-chat repo."
    )
    # send() must call the helper.
    assert "with _file_lock(" in src, (
        "WS3 ``send()`` does not wrap the JSONL write in ``_file_lock`` — "
        "the lock helper exists but is not used. Apply commit bb300a4."
    )
