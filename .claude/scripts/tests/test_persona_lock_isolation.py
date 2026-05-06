"""PRP-7c Phase 3 / WS4 — generic lock and SQLite isolation across profiles.

PRD §14.12 — verifies that two profiles' SQLite databases live in separate
directories so WAL/SHM sidecar files cannot cross-pollute. Also covers
generic ``personas.services`` lock-helper invariants:

    * ``get_bot_lock_path()`` resolves on every call (Rule 1 — no def-time
      bind), so a mid-process HOMIE_HOME swap takes effect immediately.
    * Default profile state dir (``<install>/.claude/data/state``) and
      named profile state dir (``<profile>/state``) never overlap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personas import services as _services
from personas.core import get_persona_paths


def test_lock_path_resolves_per_call(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_bot_lock_path()`` re-resolves on every call (Rule 1)."""
    sales_dir = multi_profile_fixture["sales"]
    engineering_dir = multi_profile_fixture["engineering"]

    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    a = _services.get_bot_lock_path()

    monkeypatch.setenv("HOMIE_HOME", str(engineering_dir))
    b = _services.get_bot_lock_path()

    assert a != b, (
        "get_bot_lock_path() returned the same path after HOMIE_HOME swap "
        f"({a}) — Rule 1 violation: helper is binding the resolution at "
        "def time instead of resolving on call."
    )


def test_two_profiles_state_dirs_distinct(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sales / engineering profile state dirs never overlap (PRD §14.12)."""
    sales_dir = multi_profile_fixture["sales"]
    engineering_dir = multi_profile_fixture["engineering"]
    sales_state = get_persona_paths("sales")["state"]
    eng_state = get_persona_paths("engineering")["state"]

    # Each profile's state dir is under its own profile root (the fixture
    # uses ``<empty_homie_root>/profiles/<name>``).
    monkeypatch.setenv("HOMIE_HOME", str(sales_dir))
    assert sales_state == sales_dir / "state"
    monkeypatch.setenv("HOMIE_HOME", str(engineering_dir))
    assert eng_state == engineering_dir / "state"

    assert sales_state != eng_state
    # Cannot share a parent.
    assert sales_state.parent != eng_state.parent


def test_default_state_dir_is_install_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default profile's state dir is install-dir (back-compat)."""
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    paths = _services.get_default_paths()  # noqa: SLF001 — re-export from core
    state = paths["state"]
    assert state.parts[-3:] == (".claude", "data", "state") or (
        state.parts[-2:] == ("data", "state")
    )


def test_sqlite_dbs_in_separate_data_dirs(
    multi_profile_fixture: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRD §14.12 — sqlite DBs live under separate data/ dirs per profile.

    WAL/SHM sidecar files (``X.db-wal``, ``X.db-shm``) sit next to the DB
    file. If two profiles shared a data dir, their WAL files would
    cross-pollute. Verifies that ``get_persona_paths(name)["data"]``
    returns distinct dirs for two named profiles.
    """
    sales_data = get_persona_paths("sales")["data"]
    eng_data = get_persona_paths("engineering")["data"]

    assert sales_data != eng_data
    # Root by tmp_path (fixture builds them at <tmp>/.homie/profiles/<name>/data)
    sales_dir = multi_profile_fixture["sales"]
    engineering_dir = multi_profile_fixture["engineering"]
    assert sales_data == sales_dir / "data"
    assert eng_data == engineering_dir / "data"
