"""PRP-7c Phase 3 / WS4 — bot lock path isolation across profiles.

Two profiles, two bots, distinct lock paths via
``personas.services.get_bot_lock_path()``. Default profile lock at
``<install>/.claude/chat/bot.lock``; named profile lock at
``<profile>/run/bot.lock``. Tests the byte-0 lock semantic (Windows
``msvcrt.locking`` LK_NBLCK on byte 0 / Unix ``fcntl.flock`` LOCK_EX) so
two bots can hold their respective locks simultaneously.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from personas import services as _services


def test_default_profile_lock_path_is_chat_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default profile lock = ``<install>/.claude/chat/bot.lock`` (legacy)."""
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    p = _services.get_bot_lock_path()
    assert p.parts[-3:] == ("chat", "bot.lock") or p.parts[-3:] == (
        ".claude",
        "chat",
        "bot.lock",
    )
    assert p.name == "bot.lock"


def test_named_profile_lock_path_is_run_dir(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named profile lock = ``<profile>/run/bot.lock``.

    Phase 1 layout. Distinct from default profile's chat-dir location so
    a sales bot's lock can't collide with the default's lock or vice
    versa.
    """
    sales_dir = multi_profile_fixture["sales"]
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))

    p = _services.get_bot_lock_path()
    assert p.parent.name == "run"
    assert p.name == "bot.lock"
    # Path must be under the sales profile root.
    assert sales_dir in p.parents


def test_two_profile_lock_paths_distinct(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sales and engineering profiles get distinct lock paths."""
    sales_dir = multi_profile_fixture["sales"]
    engineering_dir = multi_profile_fixture["engineering"]

    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    sales_lock = _services.get_bot_lock_path()

    monkeypatch.setenv("HOMIE_HOME", str(engineering_dir))
    engineering_lock = _services.get_bot_lock_path()

    assert sales_lock != engineering_lock
    assert sales_dir in sales_lock.parents
    assert engineering_dir in engineering_lock.parents


def test_default_vs_named_lock_paths_distinct(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default and a named profile have distinct lock paths."""
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    default_lock = _services.get_bot_lock_path()

    sales_dir = multi_profile_fixture["sales"]
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    sales_lock = _services.get_bot_lock_path()

    assert default_lock != sales_lock
    # Default lives in chat dir; sales lives in run dir.
    assert default_lock.parent.name == "chat"
    assert sales_lock.parent.name == "run"


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-specific msvcrt byte-0 lock semantic",
)
def test_two_byte0_locks_acquired_simultaneously_on_windows(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two profile-distinct lock files can both be held simultaneously.

    Exercises the same ``msvcrt.locking(fileno, LK_NBLCK, 1)`` shape
    ``chat/main.py:_acquire_instance_lock()`` uses on Unix — but on
    Windows the bot uses the named-mutex path; the lock-file path is
    only consulted on Unix. We test it here on Windows to ensure the
    helper at least does NOT crash when called on Windows file handles.
    """
    import msvcrt

    sales_dir = multi_profile_fixture["sales"]
    engineering_dir = multi_profile_fixture["engineering"]

    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    sales_lock_path = _services.get_bot_lock_path()
    sales_lock_path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOMIE_HOME", str(engineering_dir))
    eng_lock_path = _services.get_bot_lock_path()
    eng_lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open both files and acquire byte-0 lock on each.
    fh_a = open(sales_lock_path, "w")
    fh_b = open(eng_lock_path, "w")
    try:
        # Both locks should succeed because they target DIFFERENT files.
        msvcrt.locking(fh_a.fileno(), msvcrt.LK_NBLCK, 1)
        msvcrt.locking(fh_b.fileno(), msvcrt.LK_NBLCK, 1)
        # Release.
        msvcrt.locking(fh_a.fileno(), msvcrt.LK_UNLCK, 1)
        msvcrt.locking(fh_b.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        fh_a.close()
        fh_b.close()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Unix-specific fcntl.flock semantic",
)
def test_two_flock_acquired_simultaneously_on_unix(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unix fcntl.flock — two profile-distinct lock files held in parallel."""
    import fcntl

    sales_dir = multi_profile_fixture["sales"]
    engineering_dir = multi_profile_fixture["engineering"]

    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    sales_lock_path = _services.get_bot_lock_path()
    sales_lock_path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOMIE_HOME", str(engineering_dir))
    eng_lock_path = _services.get_bot_lock_path()
    eng_lock_path.parent.mkdir(parents=True, exist_ok=True)

    fh_a = open(sales_lock_path, "w")
    fh_b = open(eng_lock_path, "w")
    try:
        fcntl.flock(fh_a, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh_b, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh_a, fcntl.LOCK_UN)
        fcntl.flock(fh_b, fcntl.LOCK_UN)
    finally:
        fh_a.close()
        fh_b.close()
