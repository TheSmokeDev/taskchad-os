"""PRP-7e Phase 5 — WS4 unit tests for ``personas.archon``.

Pure unit-level tests for the ``init_archon`` contract — NO real archon
binary required. ``detect_archon_binary`` is monkeypatched throughout
to return a fake ``(Path, version_str)`` tuple. Every test starts from
the ``empty_homie_root`` / ``tmp_homie_home`` fixture in conftest.py so
the on-disk side-effects are isolated to ``tmp_path``.

Coverage groups (per PRP-7e §6 task 6 + R1/R3 disposition):

    1. ``init_archon`` happy path / idempotency / migration / --force
    2. Version-lock + strict-version drift behavior (R1 M4)
    3. ``is_archon_initialized`` Rule 2 fix (shape-aware, not is_file-only)
    4. ``_validate_config_shape`` value-aware checks (R3 NB2)
    5. ``_merge_config_shape`` overwrite-derived / preserve-custom semantics
    6. ``_atomic_write_yaml`` Windows-safe temp-then-rename
    7. ``detect_archon_binary`` Rule 1 None-sentinel signature

Anti-pattern compliance verified:
    - Rule 1: ``init_archon`` and ``detect_archon_binary`` use the
      ``None`` sentinel pattern (no ``def fn(arg=config.X)`` shape).
    - Rule 2: ``is_archon_initialized`` parses the actual YAML and runs a
      value-aware shape check; never trusts ``Path.is_file()`` alone.
    - Rule 3: N/A in WS4 (no Langfuse calls in the init hot path).
"""

from __future__ import annotations

import inspect
import threading
from pathlib import Path
from typing import Optional

import pytest
import yaml

from personas.archon import (
    ArchonConfigShapeError,
    ArchonError,
    ArchonNotInstalledError,
    ArchonVersionMismatchError,
    _atomic_write_yaml,
    _build_capability_config,
    _CANONICAL_DERIVED_VALUES,
    _CAPABILITY_CONFIG_TEMPLATE,
    _merge_config_shape,
    _REQUIRED_CONFIG_FIELDS,
    _validate_config_shape,
    detect_archon_binary,
    get_actual_config_shape,
    get_archon_config_path,
    init_archon,
    is_archon_initialized,
)


# =============================================================================
# Helpers / shared fixtures
# =============================================================================


_FAKE_BINARY: Path = Path("/fake/archon")
_FAKE_VERSION: str = "0.3.10"


@pytest.fixture
def fake_archon_binary(monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """Default monkeypatch for ``detect_archon_binary``: returns a fixed
    ``(Path, version)`` so tests don't need a real binary on PATH.

    Tests that need to override (drift, missing-binary) replace this in
    the test body via their own ``monkeypatch.setattr`` call AFTER this
    fixture has applied (last setattr wins).
    """
    fake = (_FAKE_BINARY, _FAKE_VERSION)

    def _fake_detect(*, expected_version: Optional[str] = None) -> tuple[Path, str]:
        if expected_version is not None and expected_version != _FAKE_VERSION:
            raise ArchonVersionMismatchError(
                f"fake binary reports {_FAKE_VERSION!r}, expected {expected_version!r}"
            )
        return fake

    monkeypatch.setattr("personas.archon.detect_archon_binary", _fake_detect)
    return fake


def _phase2_stub_config() -> dict:
    """Return the Phase 2 stub shape that ``init-archon`` historically wrote.

    Shape: ``archon: {enabled: true, version: "stub"}`` — this is the exact
    string the Phase 2 stub at ``lifecycle.py:885-927`` produced, NOT under
    a ``capabilities`` key. R1 B4 fix expects a migration from this shape.
    """
    return {
        "archon": {
            "enabled": True,
            "version": "stub",
        }
    }


def _install_default_minimal_config() -> dict:
    """Return the install-level minimal ``.archon/config.yaml`` shape.

    The current 4-line install default ships only ``worktree.baseBranch``
    (the rest of the PRD §11.1 fields are absent). R1 B4 expects migration
    to add the ``capabilities.archon`` block while preserving worktree.baseBranch.
    """
    return {
        "worktree": {
            "baseBranch": "master",
        }
    }


def _compliant_config(version: str = _FAKE_VERSION) -> dict:
    """Return a fully PRD §11.1-compliant config (passes ``_validate_config_shape``)."""
    return {
        "capabilities": {
            "archon": {
                "enabled": True,
                "binary": "archon",
                "archon_version": version,
                "root": ".archon",
                "workflows_dir": ".archon/workflows",
                "commands_dir": ".archon/commands",
                "artifacts_dir": ".archon/artifacts",
                "ralph_dir": ".archon/ralph",
                "worktrees_dir": ".archon/worktrees",
                "default_workflow": "archon-assist",
            }
        },
        "worktree": {
            "baseBranch": "master",
            "base_path": ".archon/worktrees",
        },
    }


def _stale_r2_config(version: str = _FAKE_VERSION) -> dict:
    """Return a config with all required keys but stale R2 derived values.

    Keys are present (presence-check passes) but ``root: archon`` (no dot)
    leaks the pre-R3 cascade convention. R3 NB2 expects ``_validate_config_shape``
    to FAIL this config so migration overwrites the stale values.
    """
    return {
        "capabilities": {
            "archon": {
                "enabled": True,
                "binary": "archon",
                "archon_version": version,
                "root": "archon",  # stale — should be ".archon"
                "workflows_dir": "archon/workflows",
                "commands_dir": "archon/commands",
                "artifacts_dir": "archon/artifacts",
                "ralph_dir": "archon/ralph",
                "worktrees_dir": "archon/worktrees",
                "default_workflow": "archon-assist",
            }
        },
        "worktree": {
            "baseBranch": "master",
            "base_path": "archon/worktrees",  # stale — should be ".archon/worktrees"
        },
    }


# =============================================================================
# Group 1: init_archon — happy path / idempotency / migration / --force
# =============================================================================


def test_init_archon_writes_config_yaml_on_fresh_profile(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Fresh profile → config.yaml + 5 subdirs + smoke YAML are written.

    Asserts the returned Path is the archon ROOT (per the docstring; the
    PRP wording 'config.yaml path' is loose — the source returns the
    ``archon_root`` and the Click handler builds ``config_path`` from it).
    """
    archon_root = init_archon("sales")

    expected_root = tmp_homie_home / ".archon"
    assert archon_root == expected_root
    assert archon_root.is_dir()
    config_path = archon_root / "config.yaml"
    assert config_path.is_file(), "PRD §11.1 config.yaml must be written"
    for sub in ("workflows", "commands", "artifacts", "ralph", "worktrees"):
        assert (archon_root / sub).is_dir(), f"missing required subdir {sub}"

    # PRD §11.1 shape verification on the written file.
    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert _validate_config_shape(parsed)

    # Smoke workflow seeded (template ships at .claude/templates/).
    smoke_path = archon_root / "workflows" / "profile-isolation-smoke.yaml"
    assert smoke_path.is_file(), "WS2a smoke workflow asset must be seeded"


def test_init_archon_returns_archon_root_path(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Return value is the ``<archon_root>`` Path (NOT the config.yaml path).

    The Click handler at ``cli.py:2245`` does ``smoke_path = archon_root /
    "workflows" / ...`` so the return value MUST be the directory, not
    the config file. This test locks the contract.
    """
    result = init_archon("sales")
    assert isinstance(result, Path)
    assert result == tmp_homie_home / ".archon"
    assert result.is_dir()


def test_init_archon_preserves_compliant_existing_config(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Re-run on PRD §11.1-compliant config: NO-OP, byte-for-byte preserved.

    R1 B4 fix: shape check passes → fresh write skipped. Tests the
    idempotency invariant — second run cannot mutate a compliant config.
    """
    archon_root = init_archon("sales")
    config_path = archon_root / "config.yaml"
    first_bytes = config_path.read_bytes()

    # Mutate file mtime by re-running. Bytes must be identical.
    init_archon("sales")
    second_bytes = config_path.read_bytes()
    assert first_bytes == second_bytes, (
        "Re-init on PRD §11.1-compliant config must be byte-for-byte idempotent"
    )


def test_init_archon_idempotent_subdirs(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Re-run preserves all 5 subdirs (re-create is idempotent)."""
    init_archon("sales")
    archon_root = tmp_homie_home / ".archon"
    init_archon("sales")
    for sub in ("workflows", "commands", "artifacts", "ralph", "worktrees"):
        assert (archon_root / sub).is_dir(), f"subdir {sub} should still exist"


def test_init_archon_migrates_phase2_stub_config(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Phase 2 stub (``archon: {enabled, version: stub}``) is MIGRATED on re-init.

    R1 B4 fix: the stub config lacks ``capabilities.archon.archon_version``,
    so ``is_archon_initialized`` returns False → init writes the merged
    template. The original ``archon.enabled = true`` key SURVIVES the
    merge (operator's custom non-derived key).
    """
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    config_path = archon_root / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_phase2_stub_config()), encoding="utf-8"
    )

    # Pre-condition: stub fails shape check (R1 B4 — the bug).
    assert not is_archon_initialized("sales")

    init_archon("sales")

    # Post-condition: shape now passes.
    assert is_archon_initialized("sales")

    # Operator's stub key preserved (it's non-derived — neither in
    # _CANONICAL_DERIVED_VALUES nor required, but the merge keeps it).
    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "archon" in parsed, "Phase 2 stub's top-level archon key must survive"
    assert parsed["archon"]["enabled"] is True


def test_init_archon_migrates_install_default_config(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Install-level minimal config (``worktree.baseBranch`` only) is migrated.

    R1 B4 fix: the 4-line install default lacks ``capabilities.archon``
    entirely → init writes the merged template. ``worktree.baseBranch``
    is NOT in ``_CANONICAL_DERIVED_VALUES`` so it MUST be preserved at
    its existing value.
    """
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    config_path = archon_root / "config.yaml"
    minimal = _install_default_minimal_config()
    minimal["worktree"]["baseBranch"] = "main"  # custom, non-default
    config_path.write_text(yaml.safe_dump(minimal), encoding="utf-8")

    init_archon("sales")

    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # Migrated: capabilities.archon now present + canonical.
    assert _validate_config_shape(parsed)
    # Preserved: operator's non-default baseBranch survives merge.
    assert parsed["worktree"]["baseBranch"] == "main", (
        "operator's worktree.baseBranch must NOT be overwritten by template"
    )
    # Migrated derived layout: base_path under worktree (canonical).
    assert parsed["worktree"]["base_path"] == ".archon/worktrees"


def test_init_archon_preserves_operator_custom_keys_during_migrate(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Operator-added custom keys (non-derived) survive migration.

    R1 B4: ``_merge_config_shape`` only overwrites entries in
    ``_CANONICAL_DERIVED_VALUES``. Anything else the operator added is
    deep-merged through.
    """
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    config_path = archon_root / "config.yaml"
    operator_config = _phase2_stub_config()
    operator_config["archon"]["custom_field"] = "operator-value"
    operator_config["operator_section"] = {"deeply": {"nested": "value"}}
    config_path.write_text(yaml.safe_dump(operator_config), encoding="utf-8")

    init_archon("sales")

    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # Required PRD §11.1 fields added.
    assert _validate_config_shape(parsed)
    # Custom keys preserved across the merge.
    assert parsed["archon"]["custom_field"] == "operator-value"
    assert parsed["operator_section"]["deeply"]["nested"] == "value"


def test_init_archon_force_overwrites_existing(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """``--force`` writes a fresh PRD §11.1 config, discarding operator customizations."""
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    config_path = archon_root / "config.yaml"
    operator = _phase2_stub_config()
    operator["archon"]["operator_secret"] = "must-be-discarded"
    config_path.write_text(yaml.safe_dump(operator), encoding="utf-8")

    init_archon("sales", force=True)

    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # Fresh template — top-level ``archon`` key NOT present (template uses
    # ``capabilities.archon`` namespace).
    assert "archon" not in parsed, (
        "--force must write a fresh template; the Phase 2 ``archon`` block "
        "must be discarded"
    )
    assert _validate_config_shape(parsed)
    # Should also re-seed smoke workflow (idempotent overwrite).
    assert (archon_root / "workflows" / "profile-isolation-smoke.yaml").is_file()


def test_init_archon_archon_version_override_pins_explicit(
    tmp_homie_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--archon-version 0.4.0`` writes that version into the lock.

    Detected version is 0.3.10 (fake), but the operator pin overrides
    the auto-detected value when ``strict_version=False`` (default).
    """
    monkeypatch.setattr(
        "personas.archon.detect_archon_binary",
        lambda **_kw: (Path("/fake/archon"), "0.4.0"),
    )
    init_archon("sales", archon_version="0.4.0")
    config_path = tmp_homie_home / ".archon" / "config.yaml"
    parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert (
        parsed["capabilities"]["archon"]["archon_version"] == "0.4.0"
    ), "operator pin must propagate to the locked config"


def test_init_archon_strict_version_fails_on_drift(
    tmp_homie_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--strict-version`` raises ``ArchonVersionMismatchError`` on drift.

    PRD §12.3 / Q3: maps to exit 7 in the Click handler. Test the
    direct-call shape; the Click handler is covered separately.
    """
    monkeypatch.setattr(
        "personas.archon.detect_archon_binary",
        lambda *, expected_version=None: (
            (_ for _ in ()).throw(
                ArchonVersionMismatchError(
                    f"installed 0.3.10 != expected {expected_version!r}"
                )
            )
            if expected_version is not None and expected_version != "0.3.10"
            else (Path("/fake/archon"), "0.3.10")
        ),
    )
    with pytest.raises(ArchonVersionMismatchError):
        init_archon("sales", archon_version="0.4.0", strict_version=True)


def test_init_archon_no_partial_state_on_strict_version_fail(
    tmp_homie_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict-version drift fails BEFORE any disk write — no subdirs / config.

    R1 M4: pre-flight detection runs before mkdir / write. Verifies the
    fail-fast contract — a partial init must NOT leave half-baked state
    on disk.
    """
    monkeypatch.setattr(
        "personas.archon.detect_archon_binary",
        lambda *, expected_version=None: (
            (_ for _ in ()).throw(
                ArchonVersionMismatchError("drift")
            )
            if expected_version == "0.4.0"
            else (Path("/fake/archon"), "0.3.10")
        ),
    )
    with pytest.raises(ArchonVersionMismatchError):
        init_archon("sales", archon_version="0.4.0", strict_version=True)

    archon_root = tmp_homie_home / ".archon"
    # The fixture pre-seeds the .archon dir as a top-level placeholder
    # (multi_profile fixture does this; tmp_homie_home does NOT — so this
    # is the truer test). Nothing should have been WRITTEN.
    assert not (archon_root / "config.yaml").exists(), (
        "no config.yaml should be written on strict-version fail"
    )
    # Subdirs should NOT exist either (pre-flight blocks mkdir).
    for sub in ("workflows", "commands", "artifacts", "ralph", "worktrees"):
        assert not (archon_root / sub).is_dir(), (
            f"no subdir {sub} should be created on strict-version fail"
        )


def test_init_archon_strict_version_drift_existing_config(
    tmp_homie_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1 post-build fix — existing shape-valid config with a locked
    ``archon_version`` that drifts from the installed binary must raise
    ``ArchonVersionMismatchError`` under ``strict_version=True``.

    Pre-fix bug: the existing-config no-op short-circuit ran BEFORE any
    version comparison, so a profile locked to ``0.3.10`` against an
    installed ``0.4.0`` binary returned silently — exit code 7
    unreachable for the common "binary upgraded after init" case.
    """
    # Existing config locks 0.3.10.
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    config_path = archon_root / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_compliant_config(version="0.3.10")), encoding="utf-8"
    )

    # Installed binary reports 0.4.0 (drift). The fake honors expected_version
    # by raising — but for `archon_version=None`, the `init_archon` body
    # calls `detect_archon_binary()` (no expected_version) and gets the
    # installed version back, then compares it with the existing locked
    # value in the new step-4 check.
    def _fake_detect(*, expected_version: Optional[str] = None):
        if expected_version is not None and expected_version != "0.4.0":
            raise ArchonVersionMismatchError(
                f"installed 0.4.0 != expected {expected_version!r}"
            )
        return (Path("/fake/archon"), "0.4.0")

    monkeypatch.setattr("personas.archon.detect_archon_binary", _fake_detect)

    pre_bytes = config_path.read_bytes()
    with pytest.raises(ArchonVersionMismatchError):
        init_archon("sales", strict_version=True)
    # Config bytes preserved on raise — strict mode does NOT mutate.
    assert config_path.read_bytes() == pre_bytes, (
        "strict-version drift must NOT rewrite the existing config"
    )


def test_init_archon_non_strict_version_drift_warns(
    tmp_homie_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """F1 post-build fix — non-strict drift logs a warning and PRESERVES the
    existing config (no rewrite).

    The operator-friendly path: keep the lock, surface the drift via stderr
    so they can decide to re-init with --force or pin a different version.
    """
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    config_path = archon_root / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(_compliant_config(version="0.3.10")), encoding="utf-8"
    )

    def _fake_detect(*, expected_version: Optional[str] = None):
        return (Path("/fake/archon"), "0.4.0")

    monkeypatch.setattr("personas.archon.detect_archon_binary", _fake_detect)

    # Spy on _atomic_write_yaml — it MUST NOT be called for shape-valid
    # configs in non-strict mode (preserves existing lock).
    write_calls: list = []
    real_write = None
    from personas import archon as _archon_mod
    real_write = _archon_mod._atomic_write_yaml

    def _spy_write(target, payload):
        write_calls.append((target, payload))
        return real_write(target, payload)

    monkeypatch.setattr(
        "personas.archon._atomic_write_yaml", _spy_write
    )

    pre_bytes = config_path.read_bytes()
    init_archon("sales", strict_version=False)  # default

    # Config preserved byte-for-byte.
    assert config_path.read_bytes() == pre_bytes, (
        "non-strict drift must preserve existing config (no rewrite)"
    )
    # No write_yaml call — config left alone (the smoke seed step does
    # not call _atomic_write_yaml; it uses write_bytes directly).
    config_writes = [c for c in write_calls if c[0] == config_path]
    assert config_writes == [], (
        "non-strict drift on shape-valid config must NOT invoke "
        "_atomic_write_yaml on the config path"
    )
    # Warning surfaced on stderr.
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "0.3.10" in captured.err
    assert "0.4.0" in captured.err


def test_validate_config_shape_rejects_null_archon_version() -> None:
    """F1 post-build fix — ``archon_version: null`` fails shape validation.

    Without this gate a hand-edited / partially migrated config could
    carry a null lock value and still be treated as initialized — which
    would silently bypass the strict-version drift check in init_archon.
    """
    cfg = _compliant_config()
    cfg["capabilities"]["archon"]["archon_version"] = None
    assert not _validate_config_shape(cfg), (
        "null archon_version must fail shape so init triggers MERGE"
    )


def test_validate_config_shape_rejects_empty_archon_version() -> None:
    """F1 post-build fix — empty / whitespace-only archon_version fails."""
    cfg = _compliant_config()
    cfg["capabilities"]["archon"]["archon_version"] = ""
    assert not _validate_config_shape(cfg)
    cfg["capabilities"]["archon"]["archon_version"] = "   "
    assert not _validate_config_shape(cfg)
    # Non-string also fails.
    cfg["capabilities"]["archon"]["archon_version"] = 42  # type: ignore[assignment]
    assert not _validate_config_shape(cfg)


def test_init_archon_repairs_null_version_lock_via_merge(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Iter-2 F1 contract test: ``init_archon`` MUST repair
    ``archon_version: null`` by merging in the detected version, NOT
    preserve the null and write back invalid shape.

    Bug data flow (pre-fix):
        1. Iter-1 fix: ``_validate_config_shape`` rejects null version.
        2. Existing config with ``archon_version: null`` no longer passes
           the no-op short-circuit at archon.py:656.
        3. ``init_archon`` falls into the merge path. ``_merge_config_shape``
           only overwrites MISSING keys + canonical-derived path keys.
           ``archon_version`` was preserved as ``None``.
        4. Result: ``init_archon`` wrote back invalid shape.
           ``is_archon_initialized`` returned False on the just-written
           config — broken loop.

    Iter-2 F1 fix: ``_merge_config_shape`` now repairs invalid scalar
    ``archon_version`` (None / "" / non-string) by replacing with the
    template's detected version. Same pattern as the existing
    ``default_workflow`` safety net.
    """
    # Setup: seed an archon dir with a config that has all required keys
    # BUT archon_version is null. This fails iter-1 shape validation, so
    # init falls into the merge branch.
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    config_path = archon_root / "config.yaml"
    seeded = _compliant_config()
    seeded["capabilities"]["archon"]["archon_version"] = None  # invalid
    config_path.write_text(yaml.safe_dump(seeded), encoding="utf-8")

    # Pre-condition: shape rejects the seeded config, so init must merge.
    assert not is_archon_initialized("sales"), (
        "iter-1 shape gate must reject null archon_version (pre-condition "
        "for iter-2 fix to be exercised)"
    )

    # Act: run init. Expected: merge repairs null version with template's
    # detected version (_FAKE_VERSION via fake_archon_binary fixture).
    init_archon("sales")

    # Assert: written config now has the detected version, NOT null.
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert (
        written["capabilities"]["archon"]["archon_version"] == _FAKE_VERSION
    ), (
        "iter-2 F1 fix: merge must replace invalid archon_version with "
        f"detected binary version {_FAKE_VERSION!r}, got "
        f"{written['capabilities']['archon']['archon_version']!r}"
    )
    # Contract: init must produce a config that is_archon_initialized
    # accepts (i.e. write a shape-valid config, not preserve the null).
    assert is_archon_initialized("sales"), (
        "iter-2 F1 fix: init_archon must produce a shape-valid config; "
        "is_archon_initialized must return True on the just-written file"
    )


def test_init_archon_repairs_empty_string_version_lock_via_merge(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Iter-2 F1 contract test (variant): empty-string archon_version is
    also repaired by the merge — same path as null but covers the
    whitespace-only / empty-string class of bad input."""
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    config_path = archon_root / "config.yaml"
    seeded = _compliant_config()
    seeded["capabilities"]["archon"]["archon_version"] = "   "  # invalid
    config_path.write_text(yaml.safe_dump(seeded), encoding="utf-8")

    assert not is_archon_initialized("sales")
    init_archon("sales")
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert (
        written["capabilities"]["archon"]["archon_version"] == _FAKE_VERSION
    )
    assert is_archon_initialized("sales")


def test_merge_config_shape_replaces_null_archon_version() -> None:
    """Iter-2 F1 unit test: ``_merge_config_shape`` directly. Existing
    config with null ``archon_version`` is replaced by the template's
    value during merge — same pattern as the ``default_workflow`` safety
    net."""
    template = _build_capability_config(archon_version=_FAKE_VERSION)
    existing = _compliant_config()
    existing["capabilities"]["archon"]["archon_version"] = None  # invalid

    merged = _merge_config_shape(existing, template)

    assert (
        merged["capabilities"]["archon"]["archon_version"] == _FAKE_VERSION
    ), "null archon_version must be replaced by template's value"
    assert _validate_config_shape(merged), (
        "post-merge config must be shape-valid (iter-2 F1 contract)"
    )


def test_merge_config_shape_preserves_valid_archon_version() -> None:
    """Iter-2 F1 unit test: a non-empty-string ``archon_version`` (the
    operator's pin) MUST survive the merge regardless of the template's
    detected version. Symmetric counter-test to the null/empty-string
    repair case."""
    template = _build_capability_config(archon_version="0.4.0")
    existing = _compliant_config(version="0.3.10")  # operator's pin

    merged = _merge_config_shape(existing, template)

    assert (
        merged["capabilities"]["archon"]["archon_version"] == "0.3.10"
    ), "operator's pinned archon_version must be preserved across merge"
    assert _validate_config_shape(merged)


def test_init_archon_install_smoke_false_skips_smoke_seed(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """``install_smoke=False`` skips the smoke-workflow seed (kw-only)."""
    init_archon("sales", install_smoke=False)
    smoke = tmp_homie_home / ".archon" / "workflows" / "profile-isolation-smoke.yaml"
    assert not smoke.is_file()


def test_init_archon_raises_filenotfound_for_unknown_profile(
    empty_homie_root: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """``init_archon("ghost")`` raises ``FileNotFoundError`` before any work.

    R1 fail-fast: ``_profile_root("ghost")`` does not exist → guard
    rejects before binary detection (saves cost) and before disk write.
    """
    with pytest.raises(FileNotFoundError):
        init_archon("ghost")


# =============================================================================
# Group 2: is_archon_initialized — Rule 2 fix
# =============================================================================


def test_is_archon_initialized_false_when_config_absent(
    tmp_homie_home: Path,
) -> None:
    """Rule 2: missing config.yaml → returns False (no Path.is_file gymnastics)."""
    # No archon dir, no config.
    assert not is_archon_initialized("sales")


def test_is_archon_initialized_false_when_config_is_phase2_stub(
    tmp_homie_home: Path,
) -> None:
    """R1 B4: Phase 2 stub fails shape check → returns False (triggers migration).

    Pre-PRP-7e implementations checked only ``Path.is_file()``, which
    silently treated the stub as ``initialized``. The shape-aware version
    REJECTS the stub.
    """
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    cfg = archon_root / "config.yaml"
    cfg.write_text(yaml.safe_dump(_phase2_stub_config()), encoding="utf-8")

    assert not is_archon_initialized("sales"), (
        "Rule 2 fix: shape check must reject Phase 2 stub even though "
        "config.yaml exists"
    )


def test_is_archon_initialized_false_when_config_is_install_default(
    tmp_homie_home: Path,
) -> None:
    """R1 B4: 4-line install-default config fails shape check → returns False."""
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    cfg = archon_root / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(_install_default_minimal_config()), encoding="utf-8"
    )

    assert not is_archon_initialized("sales")


def test_is_archon_initialized_false_when_config_is_stale_r2(
    tmp_homie_home: Path,
) -> None:
    """R3 NB2: stale R2 derived values (``root: archon`` no dot) fail shape.

    The presence check passes but the canonical-value check rejects the
    stale layout, so init must MIGRATE rather than treating it as ready.
    """
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    cfg = archon_root / "config.yaml"
    cfg.write_text(yaml.safe_dump(_stale_r2_config()), encoding="utf-8")

    assert not is_archon_initialized("sales"), (
        "R3 NB2: stale R2 derived values must trigger migration"
    )


def test_is_archon_initialized_true_after_init(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Round-trip: init → is_archon_initialized returns True."""
    init_archon("sales")
    assert is_archon_initialized("sales")


def test_is_archon_initialized_true_when_compliant_with_extra_keys(
    tmp_homie_home: Path,
) -> None:
    """Operator added a custom key — shape check still passes.

    Custom non-required keys are tolerated; only missing required keys
    or stale derived values fail the check.
    """
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    cfg = archon_root / "config.yaml"
    payload = _compliant_config()
    payload["operator_custom"] = {"any": "value"}
    cfg.write_text(yaml.safe_dump(payload), encoding="utf-8")

    assert is_archon_initialized("sales")


def test_is_archon_initialized_false_on_unparseable_yaml(
    tmp_homie_home: Path,
) -> None:
    """Garbage YAML in config.yaml → returns False (does not raise)."""
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    cfg = archon_root / "config.yaml"
    cfg.write_text("{not: [valid: yaml", encoding="utf-8")

    assert not is_archon_initialized("sales"), (
        "Unparseable YAML must return False (graceful degrade), not raise"
    )


# =============================================================================
# Group 3: _validate_config_shape — value-aware (R3 NB2)
# =============================================================================


def test_validate_config_shape_accepts_compliant() -> None:
    """The ``_compliant_config`` helper actually passes (sanity check)."""
    assert _validate_config_shape(_compliant_config())


def test_validate_config_shape_rejects_non_dict() -> None:
    """Non-dict input returns False (no raise)."""
    assert not _validate_config_shape(None)  # type: ignore[arg-type]
    assert not _validate_config_shape("not a dict")  # type: ignore[arg-type]
    assert not _validate_config_shape([1, 2, 3])  # type: ignore[arg-type]


def test_validate_config_shape_rejects_phase2_stub() -> None:
    """Phase 2 stub fails presence check (no ``capabilities`` key)."""
    assert not _validate_config_shape(_phase2_stub_config())


def test_validate_config_shape_rejects_install_default() -> None:
    """4-line install default fails presence check."""
    assert not _validate_config_shape(_install_default_minimal_config())


def test_validate_config_shape_rejects_stale_r2_values() -> None:
    """R3 NB2: stale ``root: archon`` (no dot) fails value-aware check."""
    cfg = _stale_r2_config()
    # Sanity: presence check would pass — that was the R2-era bug.
    for path in _REQUIRED_CONFIG_FIELDS:
        node = cfg
        for key in path:
            assert isinstance(node, dict)
            assert key in node
            node = node[key]
    # But the value-aware check must FAIL.
    assert not _validate_config_shape(cfg)


def test_validate_config_shape_requires_default_workflow_non_empty() -> None:
    """R3 NB2 fix: ``default_workflow`` must be a non-empty string."""
    cfg = _compliant_config()
    cfg["capabilities"]["archon"]["default_workflow"] = ""
    assert not _validate_config_shape(cfg)

    cfg["capabilities"]["archon"]["default_workflow"] = "   "
    assert not _validate_config_shape(cfg)

    cfg["capabilities"]["archon"]["default_workflow"] = None
    assert not _validate_config_shape(cfg)


def test_validate_config_shape_rejects_one_stale_derived_value() -> None:
    """Even ONE stale derived value fails the whole check.

    Locks the contract: every entry in ``_CANONICAL_DERIVED_VALUES``
    is gated. If a future refactor adds a new derived field but forgets
    to add a stale-value test, this row pattern catches it.
    """
    for path, _expected in _CANONICAL_DERIVED_VALUES.items():
        cfg = _compliant_config()
        # Walk to the path and corrupt the value.
        cursor = cfg
        for key in path[:-1]:
            cursor = cursor[key]
        cursor[path[-1]] = "stale-value"
        assert not _validate_config_shape(cfg), (
            f"stale value at {path} should fail shape check"
        )


# =============================================================================
# Group 4: _merge_config_shape — overwrite-derived / preserve-custom
# =============================================================================


def test_merge_config_shape_adds_missing_required_keys() -> None:
    """Missing required keys are filled in from the template."""
    template = _build_capability_config(archon_version=_FAKE_VERSION)
    merged = _merge_config_shape(_phase2_stub_config(), template)
    # Now passes shape.
    assert _validate_config_shape(merged)


def test_merge_config_shape_overwrites_stale_derived_values() -> None:
    """R3 NB2: stale derived values are OVERWRITTEN regardless of what's there."""
    template = _build_capability_config(archon_version=_FAKE_VERSION)
    stale = _stale_r2_config()
    # Pre: stale ``root: archon`` (no dot)
    assert stale["capabilities"]["archon"]["root"] == "archon"

    merged = _merge_config_shape(stale, template)
    # Post: canonical ``root: .archon`` — overwritten verbatim.
    assert merged["capabilities"]["archon"]["root"] == ".archon"
    assert merged["worktree"]["base_path"] == ".archon/worktrees"
    assert _validate_config_shape(merged)


def test_merge_config_shape_preserves_operator_custom_keys() -> None:
    """Custom non-derived keys deep-merge through and survive."""
    template = _build_capability_config(archon_version=_FAKE_VERSION)
    existing = _phase2_stub_config()
    existing["operator_section"] = {"foo": "bar"}
    existing["archon"]["custom"] = "preserved"

    merged = _merge_config_shape(existing, template)
    assert merged["operator_section"]["foo"] == "bar"
    assert merged["archon"]["custom"] == "preserved"


def test_merge_config_shape_preserves_default_workflow_when_set() -> None:
    """Operator's non-empty ``default_workflow`` survives merge."""
    template = _build_capability_config(archon_version=_FAKE_VERSION)
    existing = _compliant_config()
    existing["capabilities"]["archon"]["default_workflow"] = "operator-custom"
    merged = _merge_config_shape(existing, template)
    assert merged["capabilities"]["archon"]["default_workflow"] == "operator-custom"


def test_merge_config_shape_falls_back_to_template_default_workflow_when_empty() -> None:
    """Empty/missing ``default_workflow`` falls back to template value.

    Safety net: post-merge config is always shape-valid even when the
    operator set ``default_workflow: ""`` (or null).
    """
    template = _build_capability_config(archon_version=_FAKE_VERSION)
    existing = _compliant_config()
    existing["capabilities"]["archon"]["default_workflow"] = ""
    merged = _merge_config_shape(existing, template)
    assert merged["capabilities"]["archon"]["default_workflow"] == "archon-assist"
    assert _validate_config_shape(merged)


def test_merge_config_shape_is_pure_no_input_mutation() -> None:
    """Merge does NOT mutate either input dict (deepcopy under the hood)."""
    template = _build_capability_config(archon_version=_FAKE_VERSION)
    existing = _phase2_stub_config()
    existing_snapshot = yaml.safe_dump(existing, sort_keys=True)
    template_snapshot = yaml.safe_dump(template, sort_keys=True)

    _merge_config_shape(existing, template)
    assert yaml.safe_dump(existing, sort_keys=True) == existing_snapshot
    assert yaml.safe_dump(template, sort_keys=True) == template_snapshot


# =============================================================================
# Group 5: _atomic_write_yaml — Windows-safe pattern
# =============================================================================


def test_atomic_write_yaml_creates_target_with_payload(tmp_path: Path) -> None:
    """Basic write: target exists with serialized YAML payload."""
    target = tmp_path / "subdir" / "out.yaml"
    payload = {"a": 1, "b": {"c": "d"}}
    _atomic_write_yaml(target, payload)

    assert target.is_file()
    parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert parsed == payload


def test_atomic_write_yaml_no_orphan_tmp_files(tmp_path: Path) -> None:
    """After a successful write, no ``.config.*.tmp`` siblings remain.

    Verifies the temp-then-rename pattern completes cleanly — the temp
    file is moved (os.replace), not left behind.
    """
    target = tmp_path / "out.yaml"
    _atomic_write_yaml(target, {"k": "v"})

    siblings = list(tmp_path.iterdir())
    assert len(siblings) == 1, (
        f"Expected only the target file, found {[s.name for s in siblings]}"
    )
    assert siblings[0].name == "out.yaml"


def test_atomic_write_yaml_uses_tempfile_in_target_directory() -> None:
    """Source-level: tmp file is created in the SAME directory as target.

    Reflective check — same-directory + close-before-rename is what makes
    the rename atomic on Windows. Mirrors ``activity.set_active_profile``.
    """
    src = inspect.getsource(_atomic_write_yaml)
    # Target dir passed to NamedTemporaryFile.
    assert "dir=str(target.parent)" in src, (
        "_atomic_write_yaml must place the temp file in target.parent "
        "(Windows-safe rename invariant)"
    )
    # delete=False is required so the file survives the with-block exit
    # for os.replace to consume.
    assert "delete=False" in src
    # os.replace is the rename primitive (atomic on POSIX + Windows).
    assert "os.replace" in src


def test_atomic_write_yaml_under_concurrent_writers(tmp_path: Path) -> None:
    """Two threads write the same target; final file is one of the two writes.

    No partial / interleaved YAML on disk. ``os.replace`` is atomic so the
    target is always either payload-A or payload-B in full — never a mix.

    Windows note: ``os.replace`` can occasionally raise ``PermissionError``
    when two writers race because the OS briefly holds the source/target
    handles open during rename. This is expected, NOT a correctness bug —
    the contract is "the final on-disk file is always one complete payload",
    not "every write succeeds". We swallow PermissionError per-write and
    only assert the final file shape.
    """
    target = tmp_path / "concurrent.yaml"
    payload_a = {"writer": "A", "data": list(range(50))}
    payload_b = {"writer": "B", "data": list(range(50, 100))}
    barrier = threading.Barrier(2)
    write_successes = {"A": 0, "B": 0}
    write_lock = threading.Lock()

    def _writer(payload: dict, writer_id: str) -> None:
        try:
            barrier.wait(timeout=5)
        except threading.BrokenBarrierError:  # pragma: no cover
            return
        for _ in range(20):
            try:
                _atomic_write_yaml(target, payload)
            except PermissionError:
                # Windows-specific rename race — expected, not a bug.
                continue
            with write_lock:
                write_successes[writer_id] += 1

    t_a = threading.Thread(target=_writer, args=(payload_a, "A"))
    t_b = threading.Thread(target=_writer, args=(payload_b, "B"))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    # Both writers should have landed at least one successful write
    # (otherwise the test isn't actually exercising the race).
    assert write_successes["A"] + write_successes["B"] > 0, (
        "neither writer completed any write — race not exercised"
    )

    final = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert final in (payload_a, payload_b), (
        "Final file must be one of the complete writes — no partial / "
        "interleaved content. Got: " + repr(final)
    )

    # On Windows, a failed os.replace can leave a tmp file behind when
    # the rename loses the race (the helper does NOT have a try/finally
    # cleanup — same shape as activity.set_active_profile). Tolerate
    # leftover tmps — the load-bearing assertion is "final file is one
    # complete payload", which already passed above.


# =============================================================================
# Group 6: detect_archon_binary — Rule 1 None-sentinel signature
# =============================================================================


def test_detect_archon_binary_signature_uses_none_sentinel() -> None:
    """Rule 1: ``expected_version`` defaults to ``None``, NOT a config-bound constant.

    Reflective check — function default at def time MUST be ``None`` so
    the body resolves the value on every call (no def-time caching).
    """
    sig = inspect.signature(detect_archon_binary)
    expected_param = sig.parameters.get("expected_version")
    assert expected_param is not None, (
        "detect_archon_binary must accept an ``expected_version`` keyword arg"
    )
    assert expected_param.default is None, (
        f"Rule 1 violation: expected_version default must be None "
        f"(got {expected_param.default!r}). Resolve config inside the body."
    )
    assert expected_param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "expected_version must be keyword-only to prevent positional misuse"
    )


def test_init_archon_signature_uses_none_sentinel() -> None:
    """Rule 1: ``archon_version`` defaults to ``None``; ``install_smoke`` is a fixed bool.

    Per PRP-7e R1 minor caveat: ``install_smoke=True`` is a fixed boolean
    (NOT a tunable config value), so the bool default is exempt from
    Rule 1. Only the runtime-tunable ``archon_version`` gets the sentinel.
    """
    sig = inspect.signature(init_archon)

    av = sig.parameters.get("archon_version")
    assert av is not None and av.default is None, (
        "Rule 1: archon_version default must be None (resolve in body)"
    )
    assert av.kind is inspect.Parameter.KEYWORD_ONLY

    # install_smoke is a fixed bool, NOT a config bind — that's allowed.
    smoke = sig.parameters.get("install_smoke")
    assert smoke is not None and smoke.default is True
    assert smoke.kind is inspect.Parameter.KEYWORD_ONLY

    # force / strict_version: fixed bool defaults, also allowed.
    force = sig.parameters.get("force")
    assert force is not None and force.default is False
    strict = sig.parameters.get("strict_version")
    assert strict is not None and strict.default is False


def test_detect_archon_binary_raises_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``shutil.which`` returns None → ``ArchonNotInstalledError``."""
    monkeypatch.setattr("personas.archon.shutil.which", lambda _name: None)
    with pytest.raises(ArchonNotInstalledError):
        detect_archon_binary()


def test_detect_archon_binary_no_version_check_when_expected_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``expected_version=None``, no version check is performed.

    Even if installed != some other value, the call returns cleanly. This
    locks the Rule 1 None-sentinel semantics: passing None means "don't
    enforce a pin", NOT "use the module default".
    """

    class _CompletedProcess:
        stdout = b"Archon CLI v0.3.10\n"
        stderr = b""

    def _fake_run(*_a, **_kw):
        return _CompletedProcess()

    monkeypatch.setattr(
        "personas.archon.shutil.which", lambda _n: "/fake/archon"
    )
    monkeypatch.setattr("personas.archon.subprocess.run", _fake_run)

    path, version = detect_archon_binary(expected_version=None)
    assert path == Path("/fake/archon")
    assert version == "0.3.10"


def test_detect_archon_binary_raises_on_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``expected_version`` set + drift → ``ArchonVersionMismatchError``."""

    class _CompletedProcess:
        stdout = b"Archon CLI v0.3.10\n"
        stderr = b""

    monkeypatch.setattr(
        "personas.archon.shutil.which", lambda _n: "/fake/archon"
    )
    monkeypatch.setattr(
        "personas.archon.subprocess.run", lambda *_a, **_kw: _CompletedProcess()
    )
    with pytest.raises(ArchonVersionMismatchError):
        detect_archon_binary(expected_version="999.0.0")


# =============================================================================
# Group 7: get_actual_config_shape (Rule 2 helper)
# =============================================================================


def test_get_actual_config_shape_returns_none_when_missing(
    tmp_homie_home: Path,
) -> None:
    """Missing config.yaml → returns None (caller handles absence)."""
    assert get_actual_config_shape("sales") is None


def test_get_actual_config_shape_returns_parsed_dict(
    tmp_homie_home: Path,
) -> None:
    """Present config.yaml → returns the parsed dict verbatim."""
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    payload = _compliant_config()
    (archon_root / "config.yaml").write_text(
        yaml.safe_dump(payload), encoding="utf-8"
    )

    parsed = get_actual_config_shape("sales")
    assert parsed == payload


def test_get_actual_config_shape_returns_none_on_garbage_yaml(
    tmp_homie_home: Path,
) -> None:
    """Unparseable YAML → returns None (does not raise)."""
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    (archon_root / "config.yaml").write_text("{not: [valid", encoding="utf-8")
    assert get_actual_config_shape("sales") is None


def test_get_actual_config_shape_returns_none_when_yaml_is_scalar(
    tmp_homie_home: Path,
) -> None:
    """YAML loads to a scalar (not a dict) → returns None (shape sanity)."""
    archon_root = tmp_homie_home / ".archon"
    archon_root.mkdir(parents=True, exist_ok=True)
    (archon_root / "config.yaml").write_text("just a scalar", encoding="utf-8")
    assert get_actual_config_shape("sales") is None


# =============================================================================
# Group 8: Exception hierarchy invariants
# =============================================================================


def test_archon_not_installed_is_subclass_of_archon_error() -> None:
    """``ArchonNotInstalledError`` extends ``ArchonError`` so the broad
    ``except ArchonError`` catch order at the CLI handler doesn't break."""
    assert issubclass(ArchonNotInstalledError, ArchonError)


def test_archon_version_mismatch_is_subclass_of_archon_error() -> None:
    """Same invariant for the exit-7-mapped exception."""
    assert issubclass(ArchonVersionMismatchError, ArchonError)


def test_archon_config_shape_error_is_subclass_of_archon_error() -> None:
    """Same invariant for the exit-1-mapped exception."""
    assert issubclass(ArchonConfigShapeError, ArchonError)


# =============================================================================
# Group 9: Module-level constants — locked-order template + required fields
# =============================================================================


def test_capability_config_template_is_field_order_locked() -> None:
    """The module-level template carries exact PRD §11.1 field order.

    Python 3.7+ preserves dict insertion order; ``yaml.safe_dump(sort_keys=False)``
    emits keys in this exact sequence. A future refactor that re-orders
    these breaks the contract.
    """
    archon_block = _CAPABILITY_CONFIG_TEMPLATE["capabilities"]["archon"]
    expected_keys = [
        "enabled",
        "binary",
        "archon_version",
        "root",
        "workflows_dir",
        "commands_dir",
        "artifacts_dir",
        "ralph_dir",
        "worktrees_dir",
        "default_workflow",
    ]
    assert list(archon_block.keys()) == expected_keys


def test_required_config_fields_match_canonical_paths() -> None:
    """Every entry in ``_REQUIRED_CONFIG_FIELDS`` is reachable in the template.

    Locks the contract: shape allowlist references real paths in the
    template, not stale ones.
    """
    for path in _REQUIRED_CONFIG_FIELDS:
        cursor = _CAPABILITY_CONFIG_TEMPLATE
        for key in path:
            assert isinstance(cursor, dict)
            assert key in cursor, (
                f"Required path {path!r} is unreachable in the template"
            )
            cursor = cursor[key]


def test_init_archon_yaml_field_order_locked(
    tmp_homie_home: Path, fake_archon_binary: tuple[Path, str]
) -> None:
    """Re-load the written YAML, assert keys appear in the locked sequence."""
    init_archon("sales")
    config_path = tmp_homie_home / ".archon" / "config.yaml"
    raw = config_path.read_text(encoding="utf-8")

    # Find the order each archon-block key appears in raw text.
    expected_order = [
        "  enabled:",
        "  binary:",
        "  archon_version:",
        "  root:",
        "  workflows_dir:",
        "  commands_dir:",
        "  artifacts_dir:",
        "  ralph_dir:",
        "  worktrees_dir:",
        "  default_workflow:",
    ]
    positions = [raw.index(token) for token in expected_order]
    assert positions == sorted(positions), (
        f"Field order leak — got positions {positions}. The PRD §11.1 "
        f"locked sequence must be preserved on disk."
    )


def test_capability_config_archon_version_filled_at_build_time() -> None:
    """``_build_capability_config`` populates ``archon_version`` (Rule 1)."""
    cfg = _build_capability_config(archon_version="0.5.1")
    assert cfg["capabilities"]["archon"]["archon_version"] == "0.5.1"


def test_build_capability_config_rejects_empty_version() -> None:
    """Empty / non-string version → ``ArchonConfigShapeError``."""
    with pytest.raises(ArchonConfigShapeError):
        _build_capability_config(archon_version="")
    with pytest.raises(ArchonConfigShapeError):
        _build_capability_config(archon_version="   ")


def test_build_capability_config_returns_deep_copy() -> None:
    """Each call returns an independent dict — mutating one doesn't affect the next."""
    a = _build_capability_config(archon_version="1.0.0")
    a["capabilities"]["archon"]["mutated"] = True
    b = _build_capability_config(archon_version="1.0.0")
    assert "mutated" not in b["capabilities"]["archon"]


# =============================================================================
# Group 10: get_archon_config_path (sanity)
# =============================================================================


def test_get_archon_config_path_named_profile(tmp_homie_home: Path) -> None:
    """``get_archon_config_path("sales")`` resolves to ``<profile>/.archon/config.yaml``."""
    cfg = get_archon_config_path("sales")
    assert cfg == tmp_homie_home / ".archon" / "config.yaml"
