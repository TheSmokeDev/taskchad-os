"""Regression tests for the MC heartbeat loop event-loop-wedge fixes (#130).

`_mc_heartbeat_loop` was promoted from a nested closure inside `main()` to a
module-level coroutine so it is importable and exercisable here (the ONLY way to
prove the two fixes against the real code instead of a reconstructed copy):

  1. The blocking `urlopen` POST runs OFF the event loop via `asyncio.to_thread`,
     so a slow/half-open endpoint stalls only a worker thread, not the shared
     loop that Telegram/Discord/`/health`/liveness all ride.
  2. `get_current_version()` + the POST share ONE `try/except`, so a transient
     version-read failure logs and continues instead of permanently killing the
     task.
"""

from __future__ import annotations

import asyncio
import sys
import urllib.request
from pathlib import Path

CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHAT_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import main  # noqa: E402


class _FakeResp:
    """Minimal context-manager stand-in for the urlopen response."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b""


def test_heartbeat_survives_transient_version_read_failure(monkeypatch) -> None:
    """A single get_current_version() failure must not permanently kill the
    heartbeat task (acceptance criteria #2, #130).

    The first tick's version read raises; if the loop DIED there (the old
    behavior — the read sat between the two try/except blocks), version would be
    read exactly once and no POST would ever happen. With the fix, the read is
    inside the POST's try/except, so the loop logs, sleeps, and keeps ticking.
    """
    import update_check

    calls = {"version": 0}

    def _flaky_version() -> str:
        calls["version"] += 1
        if calls["version"] == 1:
            raise RuntimeError("version file unreadable")
        return "1.2.3"

    monkeypatch.setattr(update_check, "get_current_version", _flaky_version)

    posts: list[bytes] = []

    def _fake_urlopen(req, timeout=None):
        posts.append(req.data)
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    async def _run() -> None:
        # interval=0 → ticks rapidly; run briefly, then cancel.
        task = asyncio.create_task(
            main._mc_heartbeat_loop("http://mc.local/heartbeat", "api-key", interval=0)
        )
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    # Survived the first-tick failure and recovered: read >1 time, POSTed after.
    assert calls["version"] >= 2
    assert len(posts) >= 1


def test_heartbeat_post_runs_off_loop(monkeypatch) -> None:
    """A slow urlopen must not block a concurrently-scheduled coroutine (#130).

    Completion ORDER is the proof: while the 0.2s POST runs in its worker thread
    the loop is free, so the 0.05s ticker finishes first. If urlopen ran on the
    loop, "posted" would land before "ticked".
    """
    import update_check

    monkeypatch.setattr(update_check, "get_current_version", lambda: "1.0.0")

    order: list[str] = []

    def _slow_urlopen(req, timeout=None):
        import time

        time.sleep(0.2)
        order.append("posted")
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", _slow_urlopen)

    async def _run() -> None:
        # Large interval so the loop parks after one tick; we cancel explicitly.
        task = asyncio.create_task(
            main._mc_heartbeat_loop("http://mc.local/heartbeat", "api-key", interval=3600)
        )
        await asyncio.sleep(0.05)
        order.append("ticked")
        await asyncio.sleep(0.3)  # let the off-loop POST finish
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    assert order == ["ticked", "posted"]
