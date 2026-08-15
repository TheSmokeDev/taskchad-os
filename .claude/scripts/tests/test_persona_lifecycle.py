"""PRP-7b Workstream 4 — lifecycle CREATE / LIST / SHOW / DELETE / USE / INIT-ARCHON tests.

Covers ``personas.lifecycle`` end-to-end via direct Python calls (NOT
``CliRunner`` — Click handler tests live in ``test_persona_cli_handler.py``).

Disposition coverage:
    - R1 B1 — full PRD inventory seeded by create.
    - R1 B2 — every entry of ``_REQUIRED_IDENTITY_FILES`` resolves on disk.
    - R1 B3 — wrapper accepts explicit profile_root (in test_persona_wrapper_generation.py).
    - R1 B4 — OS-flag pre-validation; rollback on wrapper failure.
    - R1 B5 — fixture-split regression: create on `tmp_homie_home` raises
      FileExistsError because the fixture pre-seeds `sales`.
    - R1 M1 — `clone_from="default"` bypasses validate.
    - Anti-pattern Rule 1 — AST scan for `def fn(arg=config.X)` shapes.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
import yaml

from personas.lifecycle import (
    LifecycleError,
    ProfileInfo,
    _REQUIRED_IDENTITY_FILES,
    _REQUIRED_MEMORY_DIRS,
    _REQUIRED_PROFILE_DIRS,
    create_profile,
    delete_profile,
    init_archon,
    list_profiles,
    show_profile,
    use_profile,
)


# ---------------------------------------------------------------------------
# CREATE — R1 B2 inventory
# ---------------------------------------------------------------------------


def test_create_profile_seeds_full_prd_inventory(empty_homie_root):
    """R1 B2 — every required dir AND identity file is present on disk."""
    info = create_profile("sales", no_alias=True)
    assert isinstance(info, ProfileInfo)
    profile_dir = info.path

    for subdir in _REQUIRED_PROFILE_DIRS:
        assert (profile_dir / subdir).is_dir(), (
            f"Missing top-level profile dir: {subdir}"
        )
    for memdir in _REQUIRED_MEMORY_DIRS:
        assert (profile_dir / "memory" / memdir).is_dir(), (
            f"Missing memory subdir: memory/{memdir}"
        )
    for fname in _REQUIRED_IDENTITY_FILES:
        path = profile_dir / "memory" / fname
        assert path.exists(), f"Missing identity file: memory/{fname}"
        # Each file gets a sensible empty body — non-zero bytes.
        assert path.stat().st_size > 0, f"Empty identity file: memory/{fname}"


def test_create_profile_returns_profile_info_dataclass(empty_homie_root):
    """create_profile returns a ProfileInfo, NOT a Path (signature drift fix)."""
    info = create_profile("eng", no_alias=True)
    assert isinstance(info, ProfileInfo)
    assert info.name == "eng"
    assert info.is_default is False
    assert info.bot_running is False  # freshly created -> no bot
    assert info.has_env is False  # no .env seeded for plain create
    assert info.path == empty_homie_root / "profiles" / "eng"


# ---------------------------------------------------------------------------
# CREATE — name validation
# ---------------------------------------------------------------------------


def test_create_profile_default_raises_value_error(empty_homie_root):
    """`default` is reserved; create raises ValueError."""
    with pytest.raises(ValueError):
        create_profile("default", no_alias=True)


def test_create_profile_uppercase_raises_value_error(empty_homie_root):
    """Regex requires lowercase."""
    with pytest.raises(ValueError):
        create_profile("Sales", no_alias=True)


def test_create_profile_homie_reserved_raises_value_error(empty_homie_root):
    """`homie` is in `_RESERVED`."""
    with pytest.raises(ValueError):
        create_profile("homie", no_alias=True)


def test_create_profile_twice_raises_file_exists(empty_homie_root):
    """Second create on the same name raises FileExistsError."""
    create_profile("sales", no_alias=True)
    with pytest.raises(FileExistsError):
        create_profile("sales", no_alias=True)


# ---------------------------------------------------------------------------
# R1 B5 — fixture-split regression
# ---------------------------------------------------------------------------


def test_create_on_pre_seeded_tmp_homie_home_fails_with_FileExistsError(
    tmp_homie_home,
):
    """R1 B5 — `tmp_homie_home` pre-seeds `sales`; create must hit FileExistsError.

    This is the fixture-split honesty test. Misusing `tmp_homie_home` for
    create-tests would silently get masked unless we explicitly assert it
    raises here.
    """
    # tmp_homie_home points HOMIE_HOME at <tmp>/.homie/profiles/sales (the
    # fixture's profile_dir). The profile resolver will treat that as a
    # custom profile path. We need to verify that the named profile
    # `sales` already exists under the resolved homie root.
    with pytest.raises(FileExistsError):
        create_profile("sales", no_alias=True)


# ---------------------------------------------------------------------------
# R1 B4 — OS-flag pre-validation
# ---------------------------------------------------------------------------


def test_create_launchd_on_non_darwin_fails_fast(empty_homie_root, monkeypatch):
    """R1 B4 + R3 NNM3 — --launchd on non-darwin raises LifecycleError BEFORE
    any filesystem work."""
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(LifecycleError, match="--launchd is only valid on macOS"):
        create_profile("sales", install_launchd=True)
    # No partial profile dir.
    assert not (empty_homie_root / "profiles" / "sales").exists()


def test_create_systemd_on_non_linux_fails_fast(empty_homie_root, monkeypatch):
    """R1 B4 + R3 NNM3 — --systemd on non-linux raises LifecycleError BEFORE
    any filesystem work."""
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(LifecycleError, match="--systemd is only valid on Linux"):
        create_profile("sales", install_systemd=True)
    assert not (empty_homie_root / "profiles" / "sales").exists()


# ---------------------------------------------------------------------------
# R1 B4 — wrapper failure rollback
# ---------------------------------------------------------------------------


def test_create_no_alias_skips_wrapper_failure(empty_homie_root, monkeypatch):
    """no_alias=True bypasses wrapper layer entirely (success even if
    wrapper would have raised)."""
    from personas import wrappers

    def _boom(*args, **kwargs):
        raise OSError("wrapper raised — should never be called")

    monkeypatch.setattr(wrappers, "create_wrapper_alias", _boom)
    info = create_profile("sales", no_alias=True)
    assert info.path.exists()


def test_create_wrapper_failure_rolls_back_profile(empty_homie_root, monkeypatch):
    """R1 B4 — default-mode wrapper failure rmtree's the partial profile dir
    and re-raises as LifecycleError."""
    from personas import wrappers as wrappers_mod
    # Patch the lazily-imported attribute the lifecycle module uses.
    # `create_profile` does `from .wrappers import create_wrapper_alias` so
    # patching `personas.wrappers.create_wrapper_alias` covers the lookup.
    def _disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(wrappers_mod, "create_wrapper_alias", _disk_full)

    with pytest.raises(LifecycleError):
        create_profile("sales")
    # Rollback ran — partial profile dir is gone.
    assert not (empty_homie_root / "profiles" / "sales").exists()


def test_create_best_effort_alias_keeps_profile_on_wrapper_fail(
    empty_homie_root, monkeypatch, capsys
):
    """R1 B4 — best_effort_alias=True downgrades wrapper failure to warning."""
    from personas import wrappers as wrappers_mod

    def _disk_full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(wrappers_mod, "create_wrapper_alias", _disk_full)

    info = create_profile("sales", best_effort_alias=True)
    # Profile dir survives.
    assert info.path.exists()
    err = capsys.readouterr().err
    assert "warning" in err.lower() or "best-effort" in err.lower()


# ---------------------------------------------------------------------------
# R1 M1 — clone_from='default' bypasses validate
# ---------------------------------------------------------------------------


def test_create_with_clone_from_default_succeeds(
    empty_homie_root, default_profile_install, monkeypatch
):
    """R1 M1 — `clone_from="default"` is special-cased BEFORE
    `validate_persona_name(clone_from)` runs (which would reject "default").

    R-post-build F5 — also asserts the default's actual identity content
    landed in the destination's ``memory/SOUL.md``, not an empty seed.
    Pre-fix the clone helpers looked for ``<install>/memory/SOUL.md`` (a
    path that doesn't exist) and silently skipped the source, leaving the
    destination with the empty-body seed.

    Uses default_profile_install to ensure get_default_paths()['memory']
    resolves to a real dir on disk so the clone source is materialized.
    """
    # F5 — write a marker into the default SOUL.md so we can assert the
    # destination clones the real default identity, not the empty seed.
    install_root = default_profile_install
    default_soul = install_root / "TheHomie" / "Memory" / "SOUL.md"
    marker = "F5-MARKER-DEFAULT-IDENTITY-CLONE-7b3afe1c"
    default_soul.write_text(
        f"# Default SOUL\n\n{marker}\n", encoding="utf-8"
    )

    # `default_profile_install` overrides HOMIE_VAULT_DIR; we still need
    # HOMIE_HOME pointed at empty_homie_root so the destination lands in
    # the test's homie root (default_profile_install also sets HOMIE_VAULT_DIR
    # for the install layout — both can coexist).
    monkeypatch.setenv("HOMIE_HOME", str(empty_homie_root))
    info = create_profile(
        "sales", clone=True, clone_from="default", no_alias=True
    )
    assert info.path.exists()
    assert info.path.name == "sales"

    # F5 — assert the marker is present in dest/memory/SOUL.md.
    dest_soul = info.path / "memory" / "SOUL.md"
    assert dest_soul.exists(), (
        "F5 violation: dest/memory/SOUL.md was never created"
    )
    dest_text = dest_soul.read_text(encoding="utf-8")
    assert marker in dest_text, (
        f"F5 violation: clone_from='default' did NOT copy default identity "
        f"content into dest. dest SOUL.md content:\n{dest_text}"
    )


# ---------------------------------------------------------------------------
# R-post-build NF1 — clone_all from "default" must NOT copy install repo
# ---------------------------------------------------------------------------


def test_create_clone_all_from_default_does_not_copy_install_repo(
    empty_homie_root, default_profile_install, monkeypatch
):
    """NF1 — ``clone_all=True, clone_from="default"`` MUST NOT recursively
    copy the install repo.

    Pre-fix: ``create_profile(clone_all=True, clone_from="default")``
    resolved source to ``_default_install_root_for_clone()`` and called
    ``_copytree_with_strip(install_root, dest)`` which only filtered
    transient runtime files. The install repo's ``.git/``, nested
    ``.claude/scripts/.env``, ``integrations/`` credentials, and
    workspace code all landed in the destination profile.

    Fix: route default-source clone-all through the same staged
    profile-shaped tree the export path uses
    (``_stage_default_export_tree`` + post-stage scan).

    This test seeds the install layout with secret-shaped paths nested
    inside subdirs that would slip past a top-level-only strip, then
    verifies the destination contains NONE of them. Also asserts the
    F5 contract (default memory content reaches dest/memory/) holds for
    full clone-all, not just light-clone.
    """
    install_root = default_profile_install

    # Seed install-dir with secret-shaped paths and an .git dir to prove
    # the install repo isn't being recursive-copied.
    secret_paths = [
        install_root / ".claude" / "scripts" / ".env",
        install_root / ".claude" / "scripts" / "integrations"
            / "google_token.json",
        install_root / ".claude" / "scripts" / "integrations"
            / "credentials.json",
        install_root / "deep" / "nested" / ".env",
        install_root / "private.pem",
    ]
    for p in secret_paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("FAKE_SECRET_VALUE_DO_NOT_LEAK", encoding="utf-8")
    # Fake a .git dir to prove install-repo recursive copy is gone.
    git_dir = install_root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "config").write_text("[core]\n", encoding="utf-8")

    # Seed default SOUL.md with a marker so the F5-style content assertion
    # works on the clone-all path too.
    default_soul = install_root / "TheHomie" / "Memory" / "SOUL.md"
    marker = "NF1-MARKER-DEFAULT-CLONE-ALL-CONTENT-3a91c5e7"
    default_soul.write_text(
        f"# Default SOUL\n\n{marker}\n", encoding="utf-8"
    )

    # default_profile_install delenv'd HOMIE_HOME — we want the destination
    # under empty_homie_root so re-set it now.
    monkeypatch.setenv("HOMIE_HOME", str(empty_homie_root))

    info = create_profile(
        "ops", clone_all=True, clone_from="default", no_alias=True
    )
    assert info.path.exists()
    dst = info.path

    # NF1 — none of the install-repo secret paths leaked into dest.
    assert not (dst / ".git").exists(), (
        f"NF1 violation: dest contains .git/ from install repo recursive "
        f"copy: {list((dst / '.git').iterdir()) if (dst / '.git').exists() else []}"
    )
    assert not (dst / ".claude").exists(), (
        f"NF1 violation: dest contains .claude/ from install repo: "
        f"{list((dst / '.claude').iterdir()) if (dst / '.claude').exists() else []}"
    )
    assert not (dst / "TheHomie").exists(), (
        f"NF1 violation: dest contains TheHomie/ — default memory should "
        f"have been remapped to dest/memory/, not preserved at install "
        f"layout."
    )
    assert not (dst / "integrations").exists(), (
        "NF1 violation: dest contains integrations/ (credentials root)"
    )
    assert not (dst / "credentials").is_dir() or not any(
        (dst / "credentials").iterdir()
    ), (
        "NF1 violation: dest/credentials/ is non-empty — secrets leaked"
    )
    assert not (dst / ".env").exists(), (
        "NF1 violation: dest/.env exists from install-repo recursive copy"
    )

    # NF1 — no token-shaped files anywhere in dest.
    leaks: list[str] = []
    for path in dst.rglob("*"):
        if path.is_dir():
            continue
        name = path.name.lower()
        rel = str(path.relative_to(dst))
        if name == ".env" or name.startswith(".env."):
            leaks.append(rel)
        elif name.endswith(".pem") or name.endswith(".key"):
            leaks.append(rel)
        elif "token" in name and name.endswith(".json"):
            leaks.append(rel)
        elif "credentials" in name and name.endswith(".json"):
            leaks.append(rel)
    assert not leaks, (
        f"NF1 violation: secret-shaped leaks in dest: {leaks}"
    )

    # NF1 + F5 — default memory content reached dest/memory/SOUL.md.
    dest_soul = dst / "memory" / "SOUL.md"
    assert dest_soul.exists(), (
        "NF1+F5 violation: dest/memory/SOUL.md not created"
    )
    dest_text = dest_soul.read_text(encoding="utf-8")
    assert marker in dest_text, (
        f"NF1+F5 violation: clone_all from 'default' did NOT map default "
        f"memory content into dest/memory/SOUL.md. Got:\n{dest_text}"
    )


def test_create_clone_all_from_named_profile_still_uses_copytree(
    tmp_homie_home, monkeypatch
):
    """NF1 negative test — clone_all from a NAMED profile (not "default")
    keeps using ``_copytree_with_strip``.

    The install-repo recursive-copy problem ONLY applies to the default
    source (whose root is the install repo). Named-profile clone-all
    should still get the full ``_copytree_with_strip`` path so a partial
    source's caches/configs come along. This test pins that behavior so
    the staging-swap doesn't accidentally cross-pollute named-profile
    clone-all.

    Setup: ``tmp_homie_home`` pre-seeds a ``sales`` profile. Add a
    ``sales/skills/cool-skill/SKILL.md`` that's NOT in
    ``_stage_default_export_tree``'s safe-keys list — if the named-profile
    clone-all path accidentally went through staging, this file might be
    handled by the export ignore filter rather than copytree's transient
    filter and could be missed/duplicated. Either way the contract is:
    named clone-all goes through ``_copytree_with_strip``.
    """
    homie_root = tmp_homie_home.parent.parent
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))

    # Seed a recognizable file in sales/skills that proves we used the
    # full-tree copy (and a runtime-state file that should get stripped
    # by `_copytree_with_strip` per `_CLONE_ALL_STRIP`).
    skills_dir = tmp_homie_home / "skills" / "cool-skill"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "SKILL.md").write_text(
        "# cool skill\n", encoding="utf-8"
    )
    run_dir = tmp_homie_home / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "bot.pid").write_text("99999", encoding="utf-8")

    info = create_profile(
        "ops", clone_all=True, clone_from="sales", no_alias=True
    )
    dst = info.path
    assert dst.exists()

    # The skills file was copied — proves _copytree_with_strip ran (the
    # staging path's ignore filter would have excluded the install-repo
    # `.claude/skills/` dir, but we're cloning from a named profile here
    # so the named-profile skills/ should come through).
    assert (dst / "skills" / "cool-skill" / "SKILL.md").exists(), (
        "NF1 negative regression: clone_all from named profile lost "
        "skills/ — staging path may have been used incorrectly."
    )
    # Runtime state stripped per `_CLONE_ALL_STRIP`.
    assert not (dst / "run" / "bot.pid").exists(), (
        "clone_all should strip run/bot.pid via _CLONE_ALL_STRIP"
    )


# ---------------------------------------------------------------------------
# LIST / SHOW
# ---------------------------------------------------------------------------


def test_list_profiles_returns_pre_seeded_profile(tmp_homie_home):
    """list_profiles walks ~/.homie/profiles/ and returns the seeded entry."""
    profiles = list_profiles()
    names = [p.name for p in profiles if not p.is_default]
    assert "sales" in names


def test_show_profile_returns_seeded_data(tmp_homie_home):
    """show_profile returns ProfileInfo with the right shape."""
    info = show_profile("sales")
    assert info.name == "sales"
    assert info.is_default is False
    assert info.bot_running is False
    assert info.has_env is True  # tmp_homie_home seeds .env


def test_show_profile_nonexistent_raises_file_not_found(empty_homie_root):
    with pytest.raises(FileNotFoundError):
        show_profile("nonexistent")


# ---------------------------------------------------------------------------
# DELETE / USE / INIT-ARCHON
# ---------------------------------------------------------------------------


def test_delete_profile_removes_dir(tmp_homie_home):
    """delete_profile rmtree's the profile dir."""
    profile_root = tmp_homie_home  # The fixture returns the profile dir directly.
    assert profile_root.exists()
    delete_profile("sales", yes=True)
    assert not profile_root.exists()


def test_delete_profile_default_raises_value_error(empty_homie_root):
    with pytest.raises(ValueError):
        delete_profile("default", yes=True)


def test_delete_profile_nonexistent_raises_file_not_found(empty_homie_root):
    with pytest.raises(FileNotFoundError):
        delete_profile("nonexistent", yes=True)


def test_use_profile_writes_active_profile_file(tmp_homie_home):
    """use_profile writes ~/.homie/active_profile = name."""
    use_profile("sales")
    # active_profile lives at <homie_root>/active_profile. The fixture's
    # tmp_homie_home returns the PROFILE dir; the homie root is its parent
    # of parent (`<tmp>/.homie/profiles/sales` -> `<tmp>/.homie`).
    homie_root = tmp_homie_home.parent.parent
    active_file = homie_root / "active_profile"
    assert active_file.exists()
    assert active_file.read_text(encoding="utf-8").strip() == "sales"


def test_use_profile_nonexistent_raises_file_not_found(empty_homie_root):
    with pytest.raises(FileNotFoundError):
        use_profile("nonexistent")


def test_init_archon_creates_skeleton(tmp_homie_home, monkeypatch):
    """init_archon creates ``.archon/`` + 5 subdirs + config.yaml.

    PRP-7e R3 cascade fix: directory is now ``.archon`` (dotted) per Archon's
    discovery convention.

    PRP-7e R1 M1 fix: Phase 5 ``init_archon`` calls ``detect_archon_binary``
    pre-flight. To keep this Phase 2-era test green without requiring the
    archon binary on PATH, monkeypatch the detector to return a fake.
    """
    from pathlib import Path
    monkeypatch.setattr(
        "personas.archon.detect_archon_binary",
        lambda **_kw: (Path("/fake/archon"), "0.3.10"),
    )
    init_archon("sales")
    archon = tmp_homie_home / ".archon"
    assert archon.is_dir()
    for sub in ("workflows", "commands", "artifacts", "ralph", "worktrees"):
        assert (archon / sub).is_dir()
    assert (archon / "config.yaml").exists()


# ---------------------------------------------------------------------------
# Issue #422 — born learning (always-on at create)
# ---------------------------------------------------------------------------


def _read_config(profile_dir: Path) -> dict:
    """Parse a profile's config.yaml (physical read — Rule 2)."""
    return yaml.safe_load(
        (profile_dir / "config.yaml").read_text(encoding="utf-8")
    )


def _audit_rows() -> list[dict]:
    """Every persona-learning audit row written so far.

    ``config.DATA_DIR`` is redirected into the test tmp tree by the
    ``empty_homie_root`` fixture, and the helper resolves it at CALL time,
    so this reads the redirected ledger — never the checkout's live one.
    """
    import config

    ledger = Path(config.DATA_DIR) / "persona_learning_audit.jsonl"
    if not ledger.is_file():
        return []
    return [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_create_profile_is_born_learning_enabled(empty_homie_root):
    """#422 — a newborn carries ``learning: {enabled: true}`` on disk.

    The doctrine is "no off button": the persona does not wait for an
    operator to opt it into the learning loop.
    """
    info = create_profile("sales", no_alias=True)

    config_path = info.path / "config.yaml"
    assert config_path.is_file(), "newborn has no config.yaml"
    config = _read_config(info.path)
    assert config["learning"]["enabled"] is True

    # Reconcile round 2 — prove the tick's REAL admission check (not a
    # re-derived stand-in) accepts this door's newborn too, same as the
    # CLI and dashboard doors in test_persona_creation_surfaces.py /
    # test_dashboard_api.py.
    from persona_learning_tick import is_learning_eligible

    assert is_learning_eligible(config) is True


def test_create_profile_seeds_experience_note_dir(empty_homie_root):
    """#422 — the loop's substrate exists at birth, not on first write."""
    info = create_profile("sales", no_alias=True)
    assert (info.path / "memory" / "experience").is_dir()


def test_experience_dir_matches_writer_constant():
    """The inventory literal is pinned to the writer's own constant.

    ``lifecycle`` keeps a literal (every sibling entry is one, and importing
    ``personas.experience`` there would add an import edge for one string),
    so this test is what stops the two from drifting apart.
    """
    from personas.experience import NOTES_SUBDIR

    assert NOTES_SUBDIR in _REQUIRED_MEMORY_DIRS


def test_create_profile_writes_learning_audit_row(empty_homie_root):
    """#422 — enablement leaves an audit row naming the creating door."""
    create_profile("sales", no_alias=True)

    rows = [r for r in _audit_rows() if r["persona_id"] == "sales"]
    assert len(rows) == 1, f"expected exactly one row, got {rows}"
    row = rows[0]
    assert row["action"] == "enable"
    assert row["enabled"] is True
    assert row["actor"] == "lifecycle_create_profile"
    assert row["timestamp"]


def test_clone_does_not_carry_disabled_learning_into_newborn(
    empty_homie_root,
):
    """#422 ordering — the write runs AFTER the clone copy.

    A source profile that was surgically disabled for debugging must not
    hand its ``enabled: false`` to a child. The RMW shape is asserted too:
    the source's other authored sections survive into the newborn, which
    proves this is a read-modify-write and not a blind overwrite.
    """
    source = create_profile("sales", no_alias=True)
    (source.path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "persona": {"display_name": "Sales"},
                "learning": {"enabled": False},
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    child = create_profile(
        "ops", clone=True, clone_from="sales", no_alias=True
    )

    data = _read_config(child.path)
    assert data["learning"]["enabled"] is True, (
        "cloned learning:false survived into the newborn — the enablement "
        "write must run after the clone copies"
    )
    assert data["persona"]["display_name"] == "Sales", (
        "strict-read RMW lost an authored section from the cloned config"
    )


def test_learning_write_failure_rolls_back_profile(
    empty_homie_root, monkeypatch
):
    """#422 — a failed enablement rolls back rather than shipping a
    persona that silently never learns."""
    from personas import services as services_mod

    def _boom(*_args, **_kwargs):
        raise RuntimeError("config.yaml is a directory")

    monkeypatch.setattr(services_mod, "set_persona_learning", _boom)

    with pytest.raises(LifecycleError, match="Learning enablement failed"):
        create_profile("sales", no_alias=True)

    assert not (empty_homie_root / "profiles" / "sales").exists()


def test_wrapper_failure_rollback_removes_learning_config(
    empty_homie_root, monkeypatch
):
    """R1 B4 still holds with the new write — rollback takes config.yaml too.

    Masked-test fix (R2 round-2 gate finding): the pre-existing R1 B4
    rollback already deletes the whole partial profile, so a wrapper-failure
    test that only asserts post-rollback absence would still pass if the
    born-learning write were deleted outright. The injected wrapper failure
    now asserts the write landed BEFORE it raises, so this test actually
    fails without the Step 5b write.
    """
    from personas import wrappers as wrappers_mod

    profile_dir = empty_homie_root / "profiles" / "sales"

    def _disk_full(*args, **kwargs):
        # Wrapper creation runs after Step 5b (learning enablement) in
        # create_profile — the config write must already be on disk here.
        config = _read_config(profile_dir)
        assert config["learning"]["enabled"] is True, (
            "config.yaml was not born-learning before wrapper creation ran"
        )
        raise OSError("disk full")

    monkeypatch.setattr(wrappers_mod, "create_wrapper_alias", _disk_full)

    with pytest.raises(LifecycleError):
        create_profile("sales")

    assert not profile_dir.exists()
    assert not (profile_dir / "config.yaml").exists()


def test_rolled_back_create_writes_no_audit_row(empty_homie_root, monkeypatch):
    """#422 — the audit row is written on COMMIT.

    A create that rolled back must not leave a row claiming enablement for
    a profile that no longer exists on disk.
    """
    from personas import wrappers as wrappers_mod

    monkeypatch.setattr(
        wrappers_mod,
        "create_wrapper_alias",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(LifecycleError):
        create_profile("sales")

    assert [r for r in _audit_rows() if r["persona_id"] == "sales"] == []


def test_create_rejects_name_whose_config_escapes_profile_dir(
    empty_homie_root,
):
    """#422 containment — refuse before any filesystem work.

    ``get_persona_paths("custom")`` roots at ``HOMIE_HOME`` itself, so the
    enablement write for a profile literally named ``custom`` would land
    outside the dir the rollback deletes — on the operator's live config.
    The guard refuses the create instead, leaving zero disk trace.
    """
    with pytest.raises(LifecycleError, match="outside the profile dir"):
        create_profile("custom", no_alias=True)

    assert not (empty_homie_root / "profiles" / "custom").exists()
    assert not (empty_homie_root / "config.yaml").exists(), (
        "the enablement write escaped onto the operator's live config"
    )


# ---------------------------------------------------------------------------
# Anti-pattern Rule 1 — AST scan for `def fn(arg=config.X)` shapes
# ---------------------------------------------------------------------------


def test_no_default_arg_config_binding():
    """MEMORY.md Rule 1: no `def fn(arg=config.X)` in personas/*.py.

    Walks the personas/ package and flags any function default whose value
    is `config.<ATTR>` (an Attribute node accessed via Name 'config'). The
    None-sentinel pattern is required for runtime-overridable config.
    """
    persona_dir = Path(__file__).resolve().parent.parent / "personas"
    violations: list[str] = []
    for pyfile in persona_dir.glob("*.py"):
        if pyfile.name == "__init__.py":
            continue
        tree = ast.parse(pyfile.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for default in node.args.defaults + node.args.kw_defaults:
                if default is None:
                    continue
                if (
                    isinstance(default, ast.Attribute)
                    and isinstance(default.value, ast.Name)
                    and default.value.id == "config"
                ):
                    violations.append(
                        f"{pyfile.name}::{node.name} binds config.{default.attr} "
                        f"as default arg — use None-sentinel pattern"
                    )
    assert not violations, "\n".join(violations)
