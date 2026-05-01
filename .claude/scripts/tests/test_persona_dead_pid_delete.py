"""PRP-7b WS4 — dead-PID shortcut + alive-PID kill tests.

Exercises ``personas.atomic.is_pid_alive`` and the bot-quiesce step inside
``quiesce_profile``. Asserts:

    - is_pid_alive returns False for known-dead PIDs.
    - is_pid_alive returns True for our own PID.
    - delete_profile with a dead-PID bot.pid completes quickly (no 2s wait).
    - delete_profile with an alive child terminates the child within 2s.
    - corrupt / empty bot.pid files are tolerated (no crash).
"""
from __future__ import annotations

import os
import sys
import time

import pytest

from personas.atomic import is_pid_alive, quiesce_profile
from personas.lifecycle import _profile_root, create_profile, delete_profile


# ---------------------------------------------------------------------------
# is_pid_alive primitive
# ---------------------------------------------------------------------------


def test_is_pid_alive_returns_false_for_known_dead_pid():
    """PID 99999 is exceedingly unlikely to be live on any test host."""
    assert is_pid_alive(99999) is False


def test_is_pid_alive_returns_true_for_own_pid():
    """The current process is definitionally alive."""
    assert is_pid_alive(os.getpid()) is True


def test_is_pid_alive_returns_false_for_zero_or_negative():
    assert is_pid_alive(0) is False
    assert is_pid_alive(-1) is False


def test_is_pid_alive_returns_false_after_subprocess_exit(live_pid_fixture):
    """Once a subprocess exits, is_pid_alive returns False."""
    pid, proc = live_pid_fixture
    assert is_pid_alive(pid) is True
    proc.terminate()
    proc.wait(timeout=5)
    # Note — on some platforms the OS recycles PIDs quickly; on Windows the
    # handle persists. is_pid_alive treats a dead handle/exit-code != STILL_ACTIVE
    # as dead.
    assert is_pid_alive(pid) is False


# ---------------------------------------------------------------------------
# delete_profile with dead-PID bot.pid — fast path
# ---------------------------------------------------------------------------


def test_delete_profile_with_dead_bot_pid_completes_quickly(empty_homie_root):
    """A bot.pid containing a dead PID must NOT trigger a 2s SIGTERM wait."""
    info = create_profile("sales", no_alias=True)
    pid_file = info.path / "run" / "bot.pid"
    pid_file.write_text("99999")

    start = time.monotonic()
    delete_profile("sales", yes=True)
    elapsed = time.monotonic() - start
    # Generous bound — should finish in <1s even on slow CI.
    assert elapsed < 2.0, f"dead-PID delete took {elapsed:.2f}s (expected <2s)"


def test_quiesce_profile_dead_pid_records_bot_was_dead(empty_homie_root):
    """quiesce_profile.bot_was_dead is True when the PID is dead."""
    info = create_profile("sales", no_alias=True)
    pid_file = info.path / "run" / "bot.pid"
    pid_file.write_text("99999")

    result = quiesce_profile("sales", info.path)
    assert result.bot_was_dead is True
    assert result.bot_terminated is False


def test_quiesce_profile_no_bot_pid_skips_step(empty_homie_root):
    """No bot.pid file -> quiesce sees no work; both flags False."""
    info = create_profile("sales", no_alias=True)
    # No pid_file — run/ exists but is empty.
    result = quiesce_profile("sales", info.path)
    assert result.bot_was_dead is False
    assert result.bot_terminated is False


# ---------------------------------------------------------------------------
# Corrupt / empty bot.pid handling
# ---------------------------------------------------------------------------


def test_delete_profile_with_corrupt_bot_pid_does_not_crash(empty_homie_root):
    """Binary garbage in bot.pid must not crash delete_profile."""
    info = create_profile("sales", no_alias=True)
    pid_file = info.path / "run" / "bot.pid"
    pid_file.write_bytes(b"\x00\x01\x02not-a-pid")

    # Should treat as dead-PID-equivalent; rmtree proceeds.
    delete_profile("sales", yes=True)
    assert not info.path.exists()


def test_delete_profile_with_empty_bot_pid_does_not_crash(empty_homie_root):
    info = create_profile("sales", no_alias=True)
    pid_file = info.path / "run" / "bot.pid"
    pid_file.write_text("")
    delete_profile("sales", yes=True)
    assert not info.path.exists()


# ---------------------------------------------------------------------------
# Alive-PID kill path
# ---------------------------------------------------------------------------


def test_delete_profile_kills_alive_child_within_deadline(
    empty_homie_root, live_pid_fixture
):
    """A live subprocess in bot.pid is SIGTERM'd (or TerminateProcess'd) and
    dies within ~2s of the SIGTERM deadline."""
    pid, proc = live_pid_fixture
    info = create_profile("sales", no_alias=True)
    pid_file = info.path / "run" / "bot.pid"
    pid_file.write_text(str(pid))

    delete_profile("sales", yes=True)
    # Subprocess should have been signaled. Allow a short tail for OS cleanup.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    assert proc.poll() is not None, "subprocess was not terminated"
    # Profile dir is gone.
    assert not info.path.exists()
