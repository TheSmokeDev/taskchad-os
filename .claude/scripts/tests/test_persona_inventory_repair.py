"""Issue #109 — persona memory inventory inspect/repair.

Covers the durable fix for the "4 of 18 profiles had NO memory/ dir"
bug: pre-contract / hand-provisioned profiles violated the Phase 2
inventory and every cabinet turn silently ran with EMPTY context
(``read_file_safe`` fails open to ``""`` — no raise, no log).

Paths under test (one test per distinct path):
  * ``inspect_profile_inventory`` — pure read-only scan (Rule 2)
  * ``ensure_profile_inventory`` — idempotent seed-if-missing repair;
    NEVER overwrites an authored identity file (the live profiles are
    already authored); kill-switch gated (persona_mutation)
  * orphaned root identity files — report-only, never moved
  * ``ProfileInfo.inventory_ok`` / ``inventory_missing`` via
    ``list_profiles`` / ``show_profile``
  * cabinet boot guard — ``_profile_execution_context`` repairs a
    missing memory dir, fail-open at every seam

Synthetic broken profiles are built in tmp (``empty_homie_root``) by
creating a healthy profile then deleting pieces — the LIVE profiles were
backfilled by hand on 2026-07-07 and must never be touched by tests.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from personas.lifecycle import (  # noqa: E402
    _REQUIRED_IDENTITY_FILES,
    _REQUIRED_MEMORY_DIRS,
    _REQUIRED_PROFILE_DIRS,
    InventoryReport,
    create_profile,
    ensure_profile_inventory,
    inspect_profile_inventory,
    list_profiles,
    show_profile,
)
from security.kill_switches import KillSwitchDisabled  # noqa: E402


def _make_profile(name: str = "sales") -> Path:
    """Create a healthy profile in the tmp HOMIE_HOME; return its root."""
    info = create_profile(name, no_alias=True)
    return info.path


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    """Full recursive snapshot: relative path -> file bytes (None = dir)."""
    snap: dict[str, bytes | None] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        snap[rel] = p.read_bytes() if p.is_file() else None
    return snap


def _assert_full_prd_inventory(profile_dir: Path) -> None:
    """The same assertions test_create_profile_seeds_full_prd_inventory runs."""
    for subdir in _REQUIRED_PROFILE_DIRS:
        assert (profile_dir / subdir).is_dir(), f"missing dir: {subdir}"
    for memdir in _REQUIRED_MEMORY_DIRS:
        assert (profile_dir / "memory" / memdir).is_dir(), (
            f"missing memory dir: {memdir}"
        )
    for fname in _REQUIRED_IDENTITY_FILES:
        path = profile_dir / "memory" / fname
        assert path.exists(), f"missing identity file: {fname}"
        assert path.stat().st_size > 0, f"empty identity file: {fname}"


# ---------------------------------------------------------------------------
# inspect_profile_inventory — read-only scan
# ---------------------------------------------------------------------------


def test_inspect_healthy_profile_reports_healthy_and_writes_nothing(
    empty_homie_root,
):
    profile_dir = _make_profile()
    before = _tree_snapshot(profile_dir)

    rep = inspect_profile_inventory("sales")

    assert isinstance(rep, InventoryReport)
    assert rep.healthy is True
    assert rep.repaired is False
    assert rep.missing_count == 0
    assert rep.orphaned_root_identity_files == ()
    assert _tree_snapshot(profile_dir) == before, "inspect must be pure"


def test_inspect_missing_memory_dir_reports_all_missing(empty_homie_root):
    profile_dir = _make_profile()
    shutil.rmtree(profile_dir / "memory")

    rep = inspect_profile_inventory("sales")

    assert rep.healthy is False
    assert "memory" in rep.missing_profile_dirs
    assert set(rep.missing_memory_dirs) == set(_REQUIRED_MEMORY_DIRS)
    assert set(rep.missing_identity_files) == set(_REQUIRED_IDENTITY_FILES)
    # Still missing after — inspect never writes.
    assert not (profile_dir / "memory").exists()


def test_inspect_detects_orphaned_root_identity_file_and_never_moves_it(
    empty_homie_root,
):
    profile_dir = _make_profile()
    orphan = profile_dir / "SOUL.md"
    orphan.write_text("# orphaned root soul\n", encoding="utf-8")

    rep = inspect_profile_inventory("sales")

    assert rep.orphaned_root_identity_files == ("SOUL.md",)
    # Orphans are warn-level: they do NOT flip health.
    assert rep.healthy is True
    # Never moved, never deleted.
    assert orphan.read_text(encoding="utf-8") == "# orphaned root soul\n"


def test_inspect_rejects_default_profile(empty_homie_root):
    with pytest.raises(ValueError):
        inspect_profile_inventory("default")


def test_inspect_missing_profile_raises_file_not_found(empty_homie_root):
    with pytest.raises(FileNotFoundError):
        inspect_profile_inventory("ghost")


def test_lock_files_are_not_required_by_inventory(empty_homie_root):
    """_IDENTITY_LOCK_FILES are consumer-managed — absent != broken."""
    profile_dir = _make_profile()
    assert not (profile_dir / "memory" / "LOG.md.lock").exists()

    rep = inspect_profile_inventory("sales")
    assert rep.healthy is True

    ensure_profile_inventory("sales")
    assert not (profile_dir / "memory" / "LOG.md.lock").exists(), (
        "repair must not create consumer-managed lock files"
    )


# ---------------------------------------------------------------------------
# ensure_profile_inventory — idempotent repair
# ---------------------------------------------------------------------------


def test_ensure_repairs_missing_memory_dir_and_seeds_files(empty_homie_root):
    profile_dir = _make_profile()
    shutil.rmtree(profile_dir / "memory")

    rep = ensure_profile_inventory("sales")

    # Report holds the PRE-repair state — exactly what was created.
    assert rep.repaired is True
    assert "memory" in rep.missing_profile_dirs
    assert set(rep.missing_memory_dirs) == set(_REQUIRED_MEMORY_DIRS)
    assert set(rep.missing_identity_files) == set(_REQUIRED_IDENTITY_FILES)
    # Disk satisfies the full Phase 2 contract afterward.
    _assert_full_prd_inventory(profile_dir)


def test_ensure_is_idempotent_second_call_reports_repaired_false(
    empty_homie_root,
):
    profile_dir = _make_profile()
    shutil.rmtree(profile_dir / "memory")
    ensure_profile_inventory("sales")

    before = _tree_snapshot(profile_dir)
    rep = ensure_profile_inventory("sales")

    assert rep.repaired is False
    assert rep.healthy is True
    assert rep.missing_count == 0
    assert _tree_snapshot(profile_dir) == before, (
        "second repair must be a byte-wise no-op"
    )


def test_ensure_never_overwrites_authored_identity_file(empty_homie_root):
    """THE invariant — the 4 live profiles are already authored."""
    profile_dir = _make_profile()
    authored = "# Authored SOUL\n\nAI-citation doctrine lives here.\n"
    (profile_dir / "memory" / "SOUL.md").write_text(authored, encoding="utf-8")
    (profile_dir / "memory" / "GOALS.md").unlink()

    rep = ensure_profile_inventory("sales")

    assert rep.repaired is True
    assert rep.missing_identity_files == ("GOALS.md",)
    # Authored file byte-identical; missing file seeded.
    assert (profile_dir / "memory" / "SOUL.md").read_text(
        encoding="utf-8"
    ) == authored
    assert (profile_dir / "memory" / "GOALS.md").stat().st_size > 0


def test_ensure_seeds_missing_file_even_when_orphan_exists_at_root(
    empty_homie_root,
):
    """An orphan at root is NOT a substitute — the memory/ gap still gets a
    stub, and the orphan stays where it is (operator decision)."""
    profile_dir = _make_profile()
    (profile_dir / "memory" / "SOUL.md").unlink()
    orphan = profile_dir / "SOUL.md"
    orphan.write_text("# root soul\n", encoding="utf-8")

    rep = ensure_profile_inventory("sales")

    assert rep.repaired is True
    assert rep.missing_identity_files == ("SOUL.md",)
    assert rep.orphaned_root_identity_files == ("SOUL.md",)
    assert (profile_dir / "memory" / "SOUL.md").exists()
    assert orphan.read_text(encoding="utf-8") == "# root soul\n"


def test_ensure_killswitch_disabled_raises_and_leaves_disk_untouched(
    empty_homie_root, monkeypatch
):
    profile_dir = _make_profile()
    shutil.rmtree(profile_dir / "memory")
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")

    with pytest.raises(KillSwitchDisabled):
        ensure_profile_inventory("sales")

    assert not (profile_dir / "memory").exists(), (
        "refused repair must leave disk state unchanged"
    )


def test_ensure_rejects_default_profile(empty_homie_root):
    with pytest.raises(ValueError):
        ensure_profile_inventory("default")


def test_ensure_missing_profile_raises_file_not_found(empty_homie_root):
    """Repair repairs — it does not create profiles from nothing."""
    with pytest.raises(FileNotFoundError):
        ensure_profile_inventory("ghost")
    assert not (empty_homie_root / "profiles" / "ghost").exists()


# ---------------------------------------------------------------------------
# ProfileInfo inventory fields (list / show)
# ---------------------------------------------------------------------------


def test_list_profiles_populates_inventory_fields(empty_homie_root):
    profile_dir = _make_profile()
    infos = {i.name: i for i in list_profiles()}
    assert infos["sales"].inventory_ok is True
    assert infos["sales"].inventory_missing == 0

    shutil.rmtree(profile_dir / "memory")
    infos = {i.name: i for i in list_profiles()}
    assert infos["sales"].inventory_ok is False
    # memory profile-dir + 19 memory dirs + 15 identity files.
    assert infos["sales"].inventory_missing == (
        1 + len(_REQUIRED_MEMORY_DIRS) + len(_REQUIRED_IDENTITY_FILES)
    )


def test_show_profile_inventory_fields_on_broken_profile(empty_homie_root):
    profile_dir = _make_profile()
    (profile_dir / "memory" / "GOALS.md").unlink()

    info = show_profile("sales")
    assert info.inventory_ok is False
    assert info.inventory_missing == 1


# ---------------------------------------------------------------------------
# Cabinet boot guard — _profile_execution_context (issue #109 seam)
# ---------------------------------------------------------------------------


def _make_cabinet_profile(homie_root: Path, persona_id: str) -> Path:
    """Minimal cabinet-eligible profile (mirrors test_cofounder_persona)."""
    profile_root = homie_root / "profiles" / persona_id
    (profile_root / "memory").mkdir(parents=True)
    (profile_root / "config.yaml").write_text(
        "\n".join(
            [
                "persona:",
                f"  display_name: {persona_id.title()}",
                "  role: test role",
                "cabinet:",
                "  tools: []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (profile_root / "memory" / "SOUL.md").write_text(
        f"# {persona_id} soul", encoding="utf-8"
    )
    return profile_root


def test_cabinet_guard_repairs_missing_memory_dir_before_context_build(
    empty_homie_root, monkeypatch
):
    profile_root = _make_cabinet_profile(empty_homie_root, "seo")
    shutil.rmtree(profile_root / "memory")

    from cabinet import text_orchestrator
    from personas import lifecycle

    calls: list[str] = []
    real_ensure = lifecycle.ensure_profile_inventory

    def spy(name):
        calls.append(name)
        return real_ensure(name)

    monkeypatch.setattr(lifecycle, "ensure_profile_inventory", spy)
    ctx = text_orchestrator._profile_execution_context("seo")

    assert calls == ["seo"]
    assert ctx.error is None
    _assert_full_prd_inventory(profile_root)


def test_cabinet_guard_repair_failure_never_kills_turn(
    empty_homie_root, monkeypatch, caplog
):
    profile_root = _make_cabinet_profile(empty_homie_root, "seo")
    shutil.rmtree(profile_root / "memory")

    from cabinet import text_orchestrator
    from personas import lifecycle

    def explode(name):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(lifecycle, "ensure_profile_inventory", explode)
    with caplog.at_level("ERROR", logger=text_orchestrator.logger.name):
        ctx = text_orchestrator._profile_execution_context("seo")

    assert ctx.error is None, "guard failure must never kill the turn"
    assert any(
        "inventory repair failed" in rec.message for rec in caplog.records
    ), "the failure must be LOUD, not silent"


def test_cabinet_guard_skips_repair_when_killswitch_disabled(
    empty_homie_root, monkeypatch
):
    profile_root = _make_cabinet_profile(empty_homie_root, "seo")
    shutil.rmtree(profile_root / "memory")
    monkeypatch.setenv("HOMIE_KILLSWITCH_PERSONA_MUTATION", "disabled")

    from cabinet import text_orchestrator
    from personas import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "ensure_profile_inventory",
        lambda name: pytest.fail("repair must be skipped when kill-switched"),
    )
    ctx = text_orchestrator._profile_execution_context("seo")

    # Fail-open floor = today's behavior: empty context, turn proceeds.
    assert ctx.error is None
    assert not (profile_root / "memory").exists()


def test_cabinet_guard_no_repair_call_on_healthy_profile(
    empty_homie_root, monkeypatch
):
    """Happy path is ONE stat — the repair primitive is never invoked."""
    _make_cabinet_profile(empty_homie_root, "seo")

    from cabinet import text_orchestrator
    from personas import lifecycle

    monkeypatch.setattr(
        lifecycle,
        "ensure_profile_inventory",
        lambda name: pytest.fail("repair must not run on a healthy profile"),
    )
    ctx = text_orchestrator._profile_execution_context("seo")
    assert ctx.error is None
