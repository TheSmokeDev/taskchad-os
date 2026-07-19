"""Regression tests for fatal-task supervision (#134).

`_supervise_tasks` was promoted from a closure inside `main()` (the same move
as `_mc_heartbeat_loop`, #130) so these tests exercise the REAL code instead of
a reconstructed copy. The escalation seam — `raise` → `main()`'s catch-all →
`flush_langfuse()` + `sys.exit(1)` (main.py `except Exception`) — is pre-existing
and already proven by the `AdapterWedgedError` path; these tests prove every
fatal ending now reaches it:

  1. A fatal task crashing with ANY exception (not just AdapterWedgedError)
     re-raises, so a crashed bot exits NON-ZERO (the lying exit-0 bug).
  2. A fatal task RETURNING normally (router gave up on every adapter) raises
     FatalTaskExit, so the alive-but-deaf zombie exits NON-ZERO too.

The non-fatal, clean-drain, and cancelled paths are covered to prove the fix
did not over-reach: best-effort tasks still die quietly, a genuine drain still
returns 0, and operator-driven cancellation is never mistaken for a crash.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

CHAT_DIR = Path(__file__).resolve().parents[2] / "chat"
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CHAT_DIR))
sys.path.insert(0, str(SCRIPTS_DIR))

import main  # noqa: E402

FATAL = {"router", "liveness"}


async def _forever() -> None:
    """A task that never ends on its own — must be cancelled by the supervisor."""
    await asyncio.sleep(3600)


async def _returns() -> None:
    """A task that returns normally with no exception."""
    return


@pytest.fixture(autouse=True)
def _no_vault_writes(monkeypatch):
    """Keep append_to_daily_log from touching the vault in any test."""
    monkeypatch.setattr(main, "append_to_daily_log", lambda *a, **k: None)


def test_fatal_crash_reraises_any_exception() -> None:
    """A fatal task crashing with a plain ValueError must re-raise (not return 0)
    and cancel the remaining tasks (acceptance criterion #1, #134)."""

    async def _run() -> None:
        async def _boom() -> None:
            raise ValueError("router blew up")

        relay = asyncio.create_task(_forever())
        tasks = {"router": asyncio.create_task(_boom()), "relay": relay}

        with pytest.raises(ValueError, match="router blew up"):
            await main._supervise_tasks(tasks, fatal_tasks=FATAL)

        # The non-fatal task was cancelled as part of the shutdown.
        with pytest.raises(asyncio.CancelledError):
            await relay
        assert relay.cancelled()

    asyncio.run(_run())


def test_fatal_crash_adapter_wedged_still_raises() -> None:
    """Parity: AdapterWedgedError still propagates under the unconditional
    re-raise, so the liveness fail-fast path is unchanged (#134 preserves #130)."""
    from liveness import AdapterWedgedError

    async def _run() -> None:
        async def _wedged() -> None:
            raise AdapterWedgedError("telegram wedged")

        tasks = {"liveness": asyncio.create_task(_wedged())}
        with pytest.raises(AdapterWedgedError, match="telegram wedged"):
            await main._supervise_tasks(tasks, fatal_tasks=FATAL)

    asyncio.run(_run())


def test_fatal_clean_return_raises_fatal_task_exit() -> None:
    """A fatal task that RETURNS normally is alive-but-deaf: raise FatalTaskExit
    (naming the task) and cancel the rest (acceptance criterion #2, #134)."""

    async def _run() -> None:
        relay = asyncio.create_task(_forever())
        tasks = {"router": asyncio.create_task(_returns()), "relay": relay}

        with pytest.raises(main.FatalTaskExit, match="router"):
            await main._supervise_tasks(tasks, fatal_tasks=FATAL)

        with pytest.raises(asyncio.CancelledError):
            await relay
        assert relay.cancelled()

    asyncio.run(_run())


def test_nonfatal_crash_keeps_serving() -> None:
    """A non-fatal task crashing must be swallowed and the loop keeps serving.
    Proof: the relay's RuntimeError does NOT escape — only the later fatal
    router-return does (FatalTaskExit names 'router', never the relay error)."""

    async def _run() -> None:
        async def _relay_boom() -> None:
            raise RuntimeError("relay died")

        async def _router_returns_later() -> None:
            await asyncio.sleep(0.02)  # ensure the relay crash is processed first

        tasks = {
            "relay": asyncio.create_task(_relay_boom()),
            "router": asyncio.create_task(_router_returns_later()),
        }

        with pytest.raises(main.FatalTaskExit) as exc_info:
            await main._supervise_tasks(tasks, fatal_tasks=FATAL)

        # The escape is the fatal router return, NOT the swallowed relay crash.
        assert "router" in str(exc_info.value)
        assert "relay died" not in str(exc_info.value)

    asyncio.run(_run())


def test_nonfatal_return_completes_cleanly() -> None:
    """Only non-fatal tasks, all returning normally → supervisor returns None.
    The genuine clean-drain (exit-0) path is preserved."""

    async def _run() -> None:
        tasks = {
            "relay": asyncio.create_task(_returns()),
            "diagnostics": asyncio.create_task(_returns()),
        }
        result = await main._supervise_tasks(tasks, fatal_tasks=FATAL)
        assert result is None
        assert not tasks  # every task drained out of the dict

    asyncio.run(_run())


def test_cancelled_fatal_task_is_benign() -> None:
    """A fatal task cancelled EXTERNALLY (operator shutdown) must not flip to a
    crash — the supervisor completes without raising."""

    async def _run() -> None:
        router = asyncio.create_task(_forever())
        tasks = {"router": router}

        async def _cancel_soon() -> None:
            await asyncio.sleep(0.02)
            router.cancel()

        canceller = asyncio.create_task(_cancel_soon())
        # Must NOT raise — cancellation is an external stop, not a failure.
        await main._supervise_tasks(tasks, fatal_tasks=FATAL)
        await canceller
        assert router.cancelled()

    asyncio.run(_run())
