"""PRP-7b WS4 — migrate-default dry-run + journal infrastructure tests.

Phase 2 contract:
    - `migrate_default_dry_run()` returns `list[MigrationOp]`, idempotent.
    - `migrate_default_apply()` returns `None`, prints documented stub
      message verbatim, writes journal at `~/.homie/migration-journal.json`
      atomically (tmp + os.replace), idempotent.
    - Apply NEVER raises, NEVER moves files.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from personas.migrate import (
    MigrationOp,
    _APPLY_STUB_MESSAGE,
    _JOURNAL_VERSION,
    _default_journal_path,
    _serialize_ops,
    _write_migration_journal,
    migrate_default_apply,
    migrate_default_dry_run,
)


# ---------------------------------------------------------------------------
# Dry-run shape + idempotency
# ---------------------------------------------------------------------------


def test_dry_run_returns_list_of_migration_ops():
    """Dry-run returns a list[MigrationOp] (may be empty if no install
    layout is present)."""
    ops = migrate_default_dry_run()
    assert isinstance(ops, list)
    for op in ops:
        assert isinstance(op, MigrationOp)
        assert op.op_type in ("move", "copy")


def test_dry_run_is_idempotent():
    """Repeated calls return equal op lists (no hidden state)."""
    a = migrate_default_dry_run()
    b = migrate_default_dry_run()
    assert len(a) == len(b)
    for op_a, op_b in zip(a, b):
        assert op_a.op_type == op_b.op_type
        assert op_a.source == op_b.source
        assert op_a.destination == op_b.destination


def test_dry_run_does_not_write_anything(empty_homie_root):
    """Dry-run is pure inspection — no journal file is created."""
    journal = _default_journal_path()
    if journal.exists():
        journal.unlink()
    migrate_default_dry_run()
    assert not journal.exists()


# ---------------------------------------------------------------------------
# Apply — stub contract
# ---------------------------------------------------------------------------


def test_apply_returns_none(empty_homie_root):
    """Apply MUST return None (stub contract)."""
    result = migrate_default_apply()
    assert result is None


def test_apply_prints_documented_stub_message(empty_homie_root, capsys):
    """Apply prints the EXACT stub message — tested verbatim."""
    migrate_default_apply()
    captured = capsys.readouterr()
    assert _APPLY_STUB_MESSAGE in captured.out


def test_apply_does_not_raise(empty_homie_root):
    """Apply NEVER raises — stub contract."""
    # Multiple calls all succeed.
    migrate_default_apply()
    migrate_default_apply()
    migrate_default_apply()


def test_apply_writes_journal_at_default_path(empty_homie_root):
    """Apply writes journal at ~/.homie/migration-journal.json."""
    migrate_default_apply()
    journal = _default_journal_path()
    assert journal.exists()
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["version"] == _JOURNAL_VERSION
    assert "started_at" in payload
    assert "operations" in payload
    assert isinstance(payload["operations"], list)


def test_apply_is_idempotent(empty_homie_root):
    """Re-running apply is OK — journal stable in shape (started_at varies).

    Both calls succeed; both leave the same op-list shape on disk.
    """
    migrate_default_apply()
    journal = _default_journal_path()
    payload_1 = json.loads(journal.read_text(encoding="utf-8"))

    migrate_default_apply()
    payload_2 = json.loads(journal.read_text(encoding="utf-8"))

    # Same version, same operations list (started_at may differ).
    assert payload_1["version"] == payload_2["version"]
    assert payload_1["operations"] == payload_2["operations"]


# ---------------------------------------------------------------------------
# Apply — does NOT actually move files
# ---------------------------------------------------------------------------


def test_apply_does_not_move_files(empty_homie_root):
    """Phase 2 stub guarantee — no file actually moves.

    Build a synthetic "install dir" via ops list (the real install paths
    would resolve to the repo root, which we do not want to mutate). The
    test asserts that whatever ops the dry-run lists, the SOURCE paths
    still exist after apply.
    """
    ops_before = migrate_default_dry_run()
    sources_before = [op.source for op in ops_before if op.source.exists()]

    migrate_default_apply()

    # Every source that existed before still exists after.
    for src in sources_before:
        assert src.exists(), f"file moved unexpectedly: {src}"


# ---------------------------------------------------------------------------
# _write_migration_journal — atomic shape
# ---------------------------------------------------------------------------


def test_write_migration_journal_writes_complete_payload(tmp_path):
    """Journal contains version, started_at, operations[] in expected shape."""
    ops = [
        MigrationOp(
            op_type="move",
            source=Path("/tmp/source"),
            destination=Path("/tmp/dest"),
        )
    ]
    journal_path = tmp_path / "journal.json"
    written = _write_migration_journal(ops, journal_path=journal_path)
    assert written == journal_path
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    assert payload["version"] == _JOURNAL_VERSION
    assert "started_at" in payload
    assert len(payload["operations"]) == 1
    op = payload["operations"][0]
    assert op["op_type"] == "move"
    # Path serialized as str.
    assert isinstance(op["source"], str)
    assert isinstance(op["destination"], str)
    assert op["completed"] is False


def test_write_migration_journal_atomic_no_partial_bytes(tmp_path):
    """Atomic write contract — no tmp file leaks after write completes."""
    ops = [
        MigrationOp(op_type="move", source=Path("/a"), destination=Path("/b"))
    ]
    journal_path = tmp_path / "journal.json"
    _write_migration_journal(ops, journal_path=journal_path)
    # Final file exists.
    assert journal_path.exists()
    # No leftover .tmp files in the journal dir.
    leftovers = [
        p for p in tmp_path.iterdir()
        if p.name != journal_path.name and p.suffix == ".tmp"
    ]
    assert not leftovers, f"tmp file leaked: {leftovers}"


def test_write_migration_journal_default_path_resolves_via_homie_home(
    empty_homie_root,
):
    """Default journal path is `<homie_home>/migration-journal.json`."""
    ops: list[MigrationOp] = []
    written = _write_migration_journal(ops)
    assert written.parent == empty_homie_root
    assert written.name == "migration-journal.json"


# ---------------------------------------------------------------------------
# _serialize_ops — Path -> str coercion
# ---------------------------------------------------------------------------


def test_serialize_ops_coerces_paths_to_strings():
    ops = [
        MigrationOp(
            op_type="move",
            source=Path("/x/y"),
            destination=Path("/a/b"),
        )
    ]
    serialized = _serialize_ops(ops)
    assert serialized[0]["source"] == str(Path("/x/y"))
    assert serialized[0]["destination"] == str(Path("/a/b"))
    assert serialized[0]["op_type"] == "move"
    assert serialized[0]["completed"] is False
