"""Tests for the restart-loop circuit breaker (Hermes v0.18 port).

Covers:
  - trip / no-trip / window-expiry / clear / max_restarts<=0
  - fail-open on garbage + unwritable state path
  - R2 NB1: atomic save (happy path leaves no .tmp; os.replace-raises fails open)
  - main.py boot wiring — ``_boot_breaker_decision``: record-only-on-killed,
    trip → skip restore, non-trip, fail-open on a raising guard (the M2
    NameError class would be caught here)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
# main.py lives in the chat slice; add it too for the boot-helper tests.
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))

from orchestration import restart_loop_guard as guard


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    """Point the breaker at an isolated tmp state file (Rule 1 call-time seam)."""
    p = tmp_path / "restart_loop.json"
    monkeypatch.setattr(guard, "_state_path", lambda: p)
    return p


# ── Guard function behavior ──────────────────────────────────────────────


def test_no_trip_below_threshold(state_path):
    # 2 boots within the window → not tripped (threshold is 3).
    assert guard.check_and_record(now=1000.0) is False
    assert guard.check_and_record(now=1001.0) is False
    assert guard.is_restart_loop_tripped(now=1002.0) is False


def test_trip_at_threshold(state_path):
    # 3 boots within 60s → tripped on the 3rd record and on a fresh check.
    assert guard.check_and_record(now=1000.0) is False
    assert guard.check_and_record(now=1010.0) is False
    assert guard.check_and_record(now=1020.0) is True
    assert guard.is_restart_loop_tripped(now=1021.0) is True


def test_window_expiry_excludes_old_boots(state_path):
    # Two rapid boots would be 2/3 toward a trip...
    guard.record_restart_interrupted_boot(now=1000.0)
    guard.record_restart_interrupted_boot(now=1005.0)
    # ...but a check far outside the window counts ZERO of them.
    assert guard.is_restart_loop_tripped(now=2000.0) is False
    # A record far outside the window prunes both and starts fresh at 1.
    boots, persisted = guard.record_restart_interrupted_boot(now=2000.0)
    assert boots == [2000.0]
    assert persisted is True


def test_clear_removes_state(state_path):
    guard.check_and_record(now=1000.0)
    assert state_path.exists()
    guard.clear()
    assert not state_path.exists()
    # After clear, the boot log is empty again.
    assert guard.is_restart_loop_tripped(now=1000.0) is False


def test_clear_missing_file_is_noop(state_path):
    # clear() on a non-existent file must not raise (missing_ok).
    assert not state_path.exists()
    guard.clear()  # no raise
    assert not state_path.exists()


def test_max_restarts_zero_never_trips(state_path):
    for i in range(5):
        guard.check_and_record(max_restarts=0, now=1000.0 + i)
    assert guard.is_restart_loop_tripped(max_restarts=0, now=1010.0) is False


# ── Fail-open reads ──────────────────────────────────────────────────────


def test_load_tolerates_garbage_json(state_path):
    state_path.write_text("{not valid json", encoding="utf-8")
    # A corrupt file degrades to an empty boot log, no raise.
    assert guard.is_restart_loop_tripped(now=1000.0) is False
    # And a fresh record overwrites the garbage and starts clean.
    boots, _ = guard.record_restart_interrupted_boot(now=1000.0)
    assert boots == [1000.0]


def test_load_tolerates_wrong_shape(state_path):
    # ``boots`` entries that are not numbers are filtered out to empty.
    state_path.write_text('{"boots": ["x", null, {}]}', encoding="utf-8")
    boots, _ = guard.record_restart_interrupted_boot(now=1000.0)
    assert boots == [1000.0]


# ── R2 NB1: atomic save ──────────────────────────────────────────────────


def test_atomic_save_leaves_no_tmp_file(state_path):
    # Happy path: the write goes through tmp + os.replace and leaves no .tmp.
    guard.record_restart_interrupted_boot(now=1000.0)
    assert state_path.exists()
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    assert not tmp.exists(), "atomic save left a .tmp file behind"
    # Round-trips: the persisted boot is readable.
    assert guard._load_boots() == [1000.0]


def test_atomic_save_os_replace_raises_fails_open(state_path, monkeypatch):
    def _boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", _boom)
    # No exception escapes even though os.replace fails; persisted=False.
    boots, persisted = guard.record_restart_interrupted_boot(now=1000.0)
    assert boots == [1000.0]  # in-memory list still returned
    assert persisted is False  # write did not land
    # The real destination was never written (replace failed), so a later
    # read fails open to an empty log.
    assert not state_path.exists()
    assert guard._load_boots() == []


def test_check_and_record_fails_open_when_persistence_broken(state_path, monkeypatch):
    """F2 — the breaker must NOT trip on a count it could not durably record.

    Two prior boots persist to disk. On the third boot os.replace fails, so
    the in-memory list reaches the threshold (3) but the write did not land.
    The stated contract is fail-OPEN on write failure, so check_and_record()
    must return False (main then still restores state) rather than skip
    restore on the strength of unpersisted state.
    """
    assert guard.check_and_record(now=1000.0) is False
    assert guard.check_and_record(now=1010.0) is False

    def _boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", _boom)
    # In-memory count is 3 (>= threshold) but the save failed → fail OPEN.
    assert guard.check_and_record(now=1020.0) is False
    # Disk still holds only the two boots that actually persisted.
    assert guard._load_boots() == [1000.0, 1010.0]


# ── main.py boot wiring — _boot_breaker_decision ─────────────────────────


class _FakeGuard:
    """Injectable stand-in for the restart_loop_guard module."""

    def __init__(self, *, tripped: bool = False, raises: bool = False) -> None:
        self._tripped = tripped
        self._raises = raises
        self.calls = 0

    def check_and_record(self) -> bool:
        self.calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._tripped


@pytest.fixture(scope="module")
def main_mod():
    # Lazy import so a heavy chat-slice import failure isolates to these
    # four tests instead of the whole file.
    import main as _main

    return _main


def test_boot_decision_empty_killed_never_records(main_mod):
    fake = _FakeGuard(tripped=True)  # would trip IF it were ever called
    assert main_mod._boot_breaker_decision([], guard=fake) is False
    assert fake.calls == 0  # no respawn signal → guard never touched


def test_boot_decision_tripped_skips_restore(main_mod, monkeypatch):
    fake = _FakeGuard(tripped=True)
    # Suppress the real daily-log write on the trip path.
    monkeypatch.setattr(main_mod, "append_to_daily_log", lambda *a, **k: None)
    assert main_mod._boot_breaker_decision(["pid1", "pid2"], guard=fake) is True
    assert fake.calls == 1


def test_boot_decision_not_tripped_allows_restore(main_mod):
    fake = _FakeGuard(tripped=False)
    assert main_mod._boot_breaker_decision(["pid1"], guard=fake) is False
    assert fake.calls == 1


def test_boot_decision_raising_guard_fails_open(main_mod):
    fake = _FakeGuard(raises=True)
    # A guard that raises (incl. the M2 NameError class) fails open → False.
    assert main_mod._boot_breaker_decision(["pid1"], guard=fake) is False
    assert fake.calls == 1


def test_boot_decision_write_failing_guard_does_not_skip_restore(main_mod):
    # F2 — when persistence is broken the guard fails open (check_and_record
    # returns False), so the boot decision must NOT skip state restore.
    fake = _FakeGuard(tripped=False)  # models the fail-open (unpersisted) path
    assert main_mod._boot_breaker_decision(["pid1"], guard=fake) is False
    assert fake.calls == 1
