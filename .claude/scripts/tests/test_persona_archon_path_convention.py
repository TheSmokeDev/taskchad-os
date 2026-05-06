"""Path-convention regression tests for the PRP-7e R3 cascade fix.

Locks the dotted-``.archon`` directory convention at the resolver level
so any future regression at ``personas/core.py:get_persona_paths`` (or
``get_default_paths``) fails THIS test FIRST — before the symptom
surfaces in archon initialization, migration, or lifecycle layers.

Why these tests exist:
    The R3 cascade renamed the on-disk directory from ``archon`` to
    ``.archon`` (Archon's own discovery convention) at 8 sites in
    ``personas/*`` and the test fixtures. The dict KEY ``"archon"`` in
    the resolver maps STAYS unchanged for back-compat with one known
    consumer at ``lifecycle.py:get_default_paths()["archon"]`` and the
    new ``personas/archon.py`` module. If a future refactor flattens
    that distinction (e.g. ``profile_root / "archon"`` literal join
    creeps back in), these tests catch it BEFORE the rest of the
    archon test suite has to debug a path mismatch.

Test count: 6 (per PRP-7e WS1 spec).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import personas


@pytest.fixture
def isolated_homie_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Pin ``HOMIE_HOME`` to a fresh tmp ``.homie`` so resolver tests don't
    pick up the real user environment."""
    homie = tmp_path / ".homie"
    (homie / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    if Path.home() != tmp_path:
        # On Windows, HOME may be ignored; pin USERPROFILE too.
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    return homie


def test_get_persona_paths_default_returns_dotted_archon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_persona_paths("default")["archon"]`` returns ``<install>/.archon``.

    The default-profile path is computed by ``get_default_paths()``
    which derives from this file's location, NOT from any env var.
    The dotted convention has been baked into ``core.py:205`` since
    pre-R3 — this test locks it.
    """
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    paths = personas.get_persona_paths("default")
    archon_path = paths["archon"]
    assert archon_path.name == ".archon", (
        f"PRP-7e R3 cascade regression: get_persona_paths('default')"
        f"['archon'] should resolve to a path ending in '.archon' "
        f"(dotted), got {archon_path.name!r} from {archon_path}"
    )


def test_get_persona_paths_named_returns_dotted_archon(
    isolated_homie_root: Path,
) -> None:
    """``get_persona_paths("sales")["archon"]`` returns ``<profile>/.archon``.

    This is the R3 fix at ``personas/core.py:250``. Before the fix,
    the resolver returned ``<profile>/archon`` (no dot), which broke
    Archon's auto-bootstrap discovery convention.
    """
    paths = personas.get_persona_paths("sales")
    archon_path = paths["archon"]
    assert archon_path.name == ".archon", (
        f"PRP-7e R3 cascade regression at personas/core.py:250 — "
        f"get_persona_paths('sales')['archon'] should end in '.archon' "
        f"(dotted), got {archon_path.name!r} from {archon_path}"
    )
    # Also check the parent shape: should be ``<...>/profiles/sales/.archon``
    assert archon_path.parent.name == "sales", (
        f"named profile 'sales' archon path parent should be 'sales', "
        f"got {archon_path.parent.name!r}"
    )


def test_get_persona_paths_custom_returns_dotted_archon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``get_persona_paths("custom")["archon"]`` returns ``<HOMIE_HOME>/.archon``.

    Custom profiles use HOMIE_HOME directly as the profile root (PRP-7a
    R1 B1). The dotted-``.archon`` convention applies the same way.
    """
    custom_home = tmp_path / "bespoke-deploy"
    custom_home.mkdir()
    monkeypatch.setenv("HOMIE_HOME", str(custom_home))
    paths = personas.get_persona_paths("custom")
    archon_path = paths["archon"]
    assert archon_path.name == ".archon", (
        f"custom-profile archon path should end in '.archon', "
        f"got {archon_path.name!r} from {archon_path}"
    )


def test_required_profile_dirs_includes_dotted_archon() -> None:
    """``personas.lifecycle._REQUIRED_PROFILE_DIRS`` ships ``.archon`` (dotted).

    This is the seed list ``create_profile`` walks to bootstrap a new
    profile's directory tree. After the R3 cascade, the literal in the
    tuple must be the dotted form so freshly-created profiles match the
    resolver's path math.
    """
    from personas.lifecycle import _REQUIRED_PROFILE_DIRS

    assert ".archon" in _REQUIRED_PROFILE_DIRS, (
        f"PRP-7e R3 cascade regression at personas/lifecycle.py:175 — "
        f"_REQUIRED_PROFILE_DIRS must include '.archon' (dotted), "
        f"got {_REQUIRED_PROFILE_DIRS!r}"
    )
    # And the bare-name form must NOT be in the tuple — that's a regression
    # signal even if both end up present.
    assert "archon" not in _REQUIRED_PROFILE_DIRS, (
        f"PRP-7e R3 cascade regression: bare 'archon' (non-dotted) leaked "
        f"back into _REQUIRED_PROFILE_DIRS — only the dotted form should "
        f"be present. Got {_REQUIRED_PROFILE_DIRS!r}"
    )


def test_dict_key_archon_preserved_in_get_default_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_default_paths()`` STILL exposes the dict KEY ``"archon"``.

    The R3 cascade renamed only the Path VALUES (and the directory name
    on disk). The dict KEYS are part of the public contract — consumers
    do ``paths["archon"]``, not ``paths[".archon"]``. This test prevents
    a well-meaning refactor from "harmonizing" the key to ``".archon"``.
    """
    monkeypatch.delenv("HOMIE_HOME", raising=False)
    monkeypatch.delenv("HOMIE_VAULT_DIR", raising=False)
    paths = personas.get_default_paths()
    assert "archon" in paths.keys(), (
        f"PRP-7e R3 contract: get_default_paths() must keep dict KEY "
        f"'archon' (back-compat). Keys observed: {sorted(paths.keys())!r}"
    )
    # The VALUE under that key must be dotted.
    assert paths["archon"].name == ".archon"


def test_dict_key_archon_preserved_in_get_persona_paths(
    isolated_homie_root: Path,
) -> None:
    """``get_persona_paths(name)`` STILL exposes the dict KEY ``"archon"``."""
    paths = personas.get_persona_paths("sales")
    assert "archon" in paths.keys(), (
        f"PRP-7e R3 contract: get_persona_paths('sales') must keep dict "
        f"KEY 'archon' (back-compat). Keys observed: {sorted(paths.keys())!r}"
    )
    assert paths["archon"].name == ".archon"
