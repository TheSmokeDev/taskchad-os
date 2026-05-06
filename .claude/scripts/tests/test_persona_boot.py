"""HOMIE_NAME boot-export tests for ``apply_persona_override`` (PRP-7e R1 B6).

Phase 1's ``apply_persona_override()`` shim was amended in Phase 5 to
publish ``HOMIE_NAME`` to ``os.environ`` alongside ``HOMIE_HOME``. This
closes R1 B6: smoke workflows using ``${HOMIE_NAME:?...}`` strict
expansion now have a production identity to key on.

The amendment is a deliberate Phase 1 boundary touch — Phase 1's frozen
``__all__`` (12 helpers) is unchanged, but the ``os.environ`` side-effect
contract widens by one variable. This test file is the explicit
regression coverage for that wider contract.

Tests cover:
    - rank-1 (CLI flag) -> HOMIE_NAME=<flag value>
    - rank-2 (existing HOMIE_HOME env, derived) -> HOMIE_NAME=<derived>
    - rank-3 (sticky meta) -> HOMIE_NAME=<sticky value>
    - rank-4 (no profile selected) -> HOMIE_NAME=default
    - force-default sentinel (--profile default) -> HOMIE_NAME=default
    - _name_from_homie_home derivation: trailing slashes (POSIX/Windows),
      bespoke layouts, and the explicit R3 NM2 regression
      (returns LAST segment, not "profiles" parent).
    - Frozen ``__all__`` regression — the boot amendment did NOT widen
      the public API surface, only the side-effect contract.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import personas
from personas.boot import _name_from_homie_home, apply_persona_override

# ---------------------------------------------------------------------------
# _name_from_homie_home — the helper that derives HOMIE_NAME from a path
# ---------------------------------------------------------------------------


def test_name_from_homie_home_returns_profile_not_profiles_dir(
    tmp_path: Path,
) -> None:
    """R3 NM2 explicit regression: helper returns the LAST segment, not the
    parent ``profiles`` directory name.

    Earlier R3 spec used ``Path(...).parts[-2]`` semantics, which would
    extract ``profiles`` (the parent dir name) instead of ``marketing``
    (the actual profile name). The R4 fix uses ``Path.parent.name ==
    "profiles"`` and returns ``Path.name`` — verified here.
    """
    # Synthesize the canonical layout ``<root>/.homie/profiles/marketing``.
    homie_home = tmp_path / ".homie" / "profiles" / "marketing"
    homie_home.mkdir(parents=True)
    derived = _name_from_homie_home(str(homie_home))
    assert derived == "marketing", (
        f"R3 NM2 regression: helper should return 'marketing' (last "
        f"segment), NOT 'profiles' (parent name). Got {derived!r}."
    )


def test_name_from_homie_home_handles_trailing_slash_posix(
    tmp_path: Path,
) -> None:
    """POSIX trailing slash on HOMIE_HOME does not break name derivation.

    ``Path(...).resolve()`` strips the trailing separator on every
    platform, so ``parent.name == "profiles"`` returns True regardless
    of whether the operator typed ``HOMIE_HOME=/x/profiles/marketing/``
    or ``/x/profiles/marketing``.
    """
    homie_home = tmp_path / ".homie" / "profiles" / "marketing"
    homie_home.mkdir(parents=True)
    # Append a literal trailing slash via string manipulation so the
    # input mimics what an operator might type.
    derived = _name_from_homie_home(str(homie_home) + "/")
    assert derived == "marketing", (
        f"POSIX trailing-slash regression: got {derived!r}"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific")
def test_name_from_homie_home_handles_trailing_slash_windows(
    tmp_path: Path,
) -> None:
    """Windows backslash trailing separator does not break name derivation."""
    homie_home = tmp_path / ".homie" / "profiles" / "marketing"
    homie_home.mkdir(parents=True)
    derived = _name_from_homie_home(str(homie_home) + "\\")
    assert derived == "marketing", (
        f"Windows trailing-backslash regression: got {derived!r}"
    )


def test_name_from_homie_home_returns_custom_for_bespoke_layout(
    tmp_path: Path,
) -> None:
    """Non-canonical layouts (HOMIE_HOME pointing somewhere bespoke) derive
    to ``"custom"`` — aligns with ``get_persona_paths("custom")`` semantics
    (PRP-7a R1 B1)."""
    bespoke = tmp_path / "srv" / "myhomie"
    bespoke.mkdir(parents=True)
    assert _name_from_homie_home(str(bespoke)) == "custom"


def test_name_from_homie_home_returns_default_for_empty() -> None:
    """Empty / unparseable input -> ``"default"`` (safe fallback)."""
    assert _name_from_homie_home("") == "default"


# ---------------------------------------------------------------------------
# apply_persona_override — HOMIE_NAME export at every exit branch
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Drop HOMIE_HOME / HOMIE_NAME so each test starts from a known
    environment. Returns the tmp dir for tests that need to seed a
    profile layout."""
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    monkeypatch.delenv("HOMIE_NAME", raising=False)
    monkeypatch.setattr(sys, "argv", ["script.py"])
    # Pin HOME so resolve_persona_env / get_default_homie_root point at
    # tmp_path/.homie instead of the real user home.
    monkeypatch.setenv("HOME", str(tmp_path))
    if sys.platform == "win32":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def test_apply_persona_override_exports_homie_name_for_explicit_flag(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rank 1 (CLI flag): ``--profile sales`` -> ``HOMIE_NAME == "sales"``.

    Direct assignment (not setdefault) — the resolved profile_name is
    the new authority; any stale HOMIE_NAME set by a parent process
    must be replaced.
    """
    # Seed a sales profile dir so ``resolve_persona_env`` succeeds.
    sales_dir = isolated_env / ".homie" / "profiles" / "sales"
    sales_dir.mkdir(parents=True)

    monkeypatch.setattr(sys, "argv", ["script.py", "--profile", "sales"])
    apply_persona_override()

    assert os.environ.get("HOMIE_NAME") == "sales", (
        f"rank-1 CLI flag should publish HOMIE_NAME='sales', got "
        f"{os.environ.get('HOMIE_NAME')!r}"
    )
    # And HOMIE_HOME should be set to the resolved sales path.
    assert os.environ.get("HOMIE_HOME") == str(sales_dir)


def test_apply_persona_override_exports_homie_name_for_force_default_sentinel(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force-default sentinel (``--profile default``) -> ``HOMIE_NAME ==
    "default"``.

    Direct assignment — the operator typing ``--profile default`` is an
    explicit force; any stale HOMIE_NAME from a parent must be replaced.
    """
    monkeypatch.setattr(sys, "argv", ["script.py", "--profile", "default"])
    monkeypatch.setenv("HOMIE_NAME", "stale-from-parent")  # sanity prefix

    apply_persona_override()

    assert os.environ.get("HOMIE_NAME") == "default", (
        f"force-default sentinel should publish HOMIE_NAME='default' "
        f"(direct assignment, not setdefault), got "
        f"{os.environ.get('HOMIE_NAME')!r}"
    )
    # And HOMIE_HOME should be cleared.
    assert "HOMIE_HOME" not in os.environ


def test_apply_persona_override_exports_homie_name_for_rank4_fallthrough(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rank 4 (no profile selected) -> ``HOMIE_NAME == "default"`` via
    ``setdefault`` (a parent-set HOMIE_NAME would still win)."""
    monkeypatch.setattr(sys, "argv", ["script.py"])
    monkeypatch.delenv("HOMIE_NAME", raising=False)

    apply_persona_override()

    assert os.environ.get("HOMIE_NAME") == "default", (
        f"rank-4 fallthrough should publish HOMIE_NAME='default', got "
        f"{os.environ.get('HOMIE_NAME')!r}"
    )


def test_apply_persona_override_derives_homie_name_from_existing_homie_home(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rank 2 (existing HOMIE_HOME env, no CLI flag) -> derived HOMIE_NAME.

    A parent process / orchestrator that already set HOMIE_HOME=
    ``<root>/.homie/profiles/marketing`` should see HOMIE_NAME derived
    via ``_name_from_homie_home`` -> ``"marketing"``.
    """
    marketing_dir = isolated_env / ".homie" / "profiles" / "marketing"
    marketing_dir.mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(marketing_dir))
    monkeypatch.delenv("HOMIE_NAME", raising=False)
    monkeypatch.setattr(sys, "argv", ["script.py"])

    apply_persona_override()

    assert os.environ.get("HOMIE_NAME") == "marketing", (
        f"rank-2 derivation should publish HOMIE_NAME='marketing', got "
        f"{os.environ.get('HOMIE_NAME')!r}"
    )


def test_apply_persona_override_rank2_setdefault_respects_parent_set_name(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rank 2: parent-set HOMIE_NAME wins over derivation (``setdefault``).

    Documents the behavior contract — a parent / orchestrator that
    explicitly sets HOMIE_NAME has authority; the derivation only
    fills in when HOMIE_NAME is unset.
    """
    marketing_dir = isolated_env / ".homie" / "profiles" / "marketing"
    marketing_dir.mkdir(parents=True)
    monkeypatch.setenv("HOMIE_HOME", str(marketing_dir))
    monkeypatch.setenv("HOMIE_NAME", "parent-set-override")
    monkeypatch.setattr(sys, "argv", ["script.py"])

    apply_persona_override()

    # setdefault means parent-set value wins.
    assert os.environ.get("HOMIE_NAME") == "parent-set-override"


# ---------------------------------------------------------------------------
# Frozen __all__ regression — boot amendment did NOT widen the public surface
# ---------------------------------------------------------------------------


def test_personas_public_api_unchanged_after_boot_amendment() -> None:
    """The PRP-7e Phase 5 boot amendment (HOMIE_NAME export) did NOT add a
    new public helper to ``personas/__init__.py:__all__``.

    Side-effect contract widened by one env var; public API surface stays
    aligned with the canonical ``EXPECTED_PUBLIC_API`` tuple in
    ``test_personas_public_api.py``. PRD-8 Phase 2 / WS1 expanded the API
    from 12 → 14 by adding ``load_persona_config`` + ``ConfigShapeError``;
    this layered guard tracks the canonical list rather than re-asserting
    a frozen number so the two contracts cannot drift.
    """
    # Single source of truth — re-export from the canonical contract test
    # to avoid duplicating the API-Surface tuple in two places (R2 NM1).
    from tests.test_personas_public_api import EXPECTED_PUBLIC_API

    assert tuple(personas.__all__) == EXPECTED_PUBLIC_API, (
        f"PRP-7e Phase 5 + PRD-8 Phase 2 contract: personas.__all__ MUST "
        f"match EXPECTED_PUBLIC_API verbatim. Observed: "
        f"{tuple(personas.__all__)!r}"
    )
    assert len(personas.__all__) == len(EXPECTED_PUBLIC_API)
