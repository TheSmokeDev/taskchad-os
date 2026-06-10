"""Regression tests for #14: dim-drift guard must introspect physical schema,
not trust meta rows.

The original bug: sync_index() read meta.embedding_model/embedding_dimensions
to decide rebuild. If meta was missing/stale/corrupted (copied DB, partial
rebuild), guard didn't fire even when vec_chunks DDL was at the old dim.
init_schema() then upserted current config to meta, making meta lie forever.

The fix: get_actual_embedding_dim() reads the vector dim from the physical
schema (sqlite_master DDL for SQLite, pg_attribute.atttypmod for Postgres).
Meta is no longer the truth source for dim-drift detection.
"""

from __future__ import annotations

import io
import sqlite3
import sys
from contextlib import redirect_stdout
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _make_sqlite_db(db_path: Path):
    """Build a fresh SQLiteMemoryDB pointed at an isolated file.

    Forces DATABASE_URL empty so the factory picks SQLite (Postgres path
    would need a live server).
    """
    from db import SQLiteMemoryDB
    return SQLiteMemoryDB(db_path=str(db_path))


# -----------------------------------------------------------------------------
# Test 1: get_actual_embedding_dim reads the physical schema correctly
# -----------------------------------------------------------------------------

def test_sqlite_schema_introspection_returns_actual_dim(tmp_path: Path):
    """After init_schema, get_actual_embedding_dim() must return the dim
    that was baked into vec_chunks DDL. This is the truth source the
    sync_index guard now relies on."""
    from config import EMBEDDING_DIMENSIONS

    db_path = tmp_path / "test_mem.db"
    db = _make_sqlite_db(db_path)
    try:
        db.init_schema()
        actual_dim = db.get_actual_embedding_dim()
        assert actual_dim == EMBEDDING_DIMENSIONS, (
            f"expected {EMBEDDING_DIMENSIONS}, got {actual_dim}"
        )
    finally:
        db.close()


def test_sqlite_schema_introspection_returns_none_when_table_missing(tmp_path: Path):
    """Fresh DB with no init_schema() call -> vec_chunks doesn't exist yet.
    get_actual_embedding_dim() must return None (not raise) so sync_index
    can skip the mismatch check on first-run."""
    db_path = tmp_path / "test_mem_empty.db"
    db = _make_sqlite_db(db_path)
    try:
        # Do NOT call init_schema() -- we want to observe the "no table" branch.
        actual_dim = db.get_actual_embedding_dim()
        assert actual_dim is None
    finally:
        db.close()


# -----------------------------------------------------------------------------
# Test 2: sync_index rebuilds when meta is missing/lying but schema is stale
# -----------------------------------------------------------------------------

def test_sync_index_rebuilds_when_meta_missing_but_schema_stale(
    tmp_path: Path, isolated_db_modules
):
    """The core bug this issue fixes: meta rows are gone (copied DB,
    partial rebuild) but vec_chunks still has the old dim baked into DDL.

    Set up vec_chunks at dim=512 with NO meta rows, then bump config to 1024.
    sync_index() must detect the mismatch via schema introspection and
    trigger bulk_clear().

    1024 is deliberately NOT the real config default (768) — if the
    isolation fixture silently stopped re-importing the modules under the
    patched config, the `config=1024` assertions below would fail instead
    of passing by coincidence.
    """
    # Create an isolated SQLite DB manually so we control exactly what's there.
    db_path = tmp_path / "stale_schema.db"
    # Empty memory dir so sync_index has nothing to index.
    empty_memory = tmp_path / "empty_memory"
    empty_memory.mkdir()

    # db.py and memory_index.py copy config values at import time
    # (`from config import ...`), so patching config alone never reaches
    # them. The fixture pops both modules from sys.modules and fresh-imports
    # them under the patched config (the same import-time binding path a
    # real import takes), then restores the pristine originals on teardown
    # so nothing leaks to later tests — see tests/module_isolation.py.
    iso = isolated_db_modules(
        DATABASE_PATH=db_path,
        DATABASE_URL="",  # force SQLite
        EMBEDDING_DIMENSIONS=1024,
        EMBEDDING_MODEL="new-model-v2",
        MEMORY_DIR=empty_memory,
    )
    db_mod = iso.db
    mi = iso.memory_index

    # Liveness check: the patched values must be visible in the fresh
    # modules. Fails loudly if the fixture stops re-importing.
    assert db_mod.EMBEDDING_DIMENSIONS == 1024
    assert mi.EMBEDDING_DIMENSIONS == 1024
    assert mi.EMBEDDING_MODEL == "new-model-v2"

    # Manually create a DB with vec_chunks at dim=512 and NO meta rows.
    # We bypass init_schema() so meta stays empty.
    import sqlite_vec
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[512])")
    # Intentionally do NOT create meta table or insert any meta rows.
    conn.commit()
    conn.close()

    # Confirm the staging worked: vec_chunks at 512, no meta table.
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vec_chunks'"
    ).fetchone()
    assert row is not None
    assert "float[512]" in row[0]
    meta_row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    assert meta_row is None, "meta table should NOT exist yet (staging the bug)"
    conn.close()

    # Call sync_index with generate_embeddings=False so we don't need a
    # real embedding model.
    stdout_buf = io.StringIO()
    with redirect_stdout(stdout_buf):
        mi.sync_index(memory_dir=empty_memory, generate_embeddings=False)
    output = stdout_buf.getvalue()

    # The schema-vs-config mismatch should trigger a rebuild announcement.
    assert "Embedding dim mismatch" in output, (
        f"expected dim mismatch message in stdout, got: {output!r}"
    )
    assert "vec schema=512" in output
    assert "config=1024" in output

    # After rebuild, the physical schema must be at the new dim.
    db_after = db_mod.SQLiteMemoryDB(db_path=str(db_path))
    try:
        actual = db_after.get_actual_embedding_dim()
        assert actual == 1024, f"expected vec_chunks recreated at 1024, got {actual}"
    finally:
        db_after.close()


# -----------------------------------------------------------------------------
# Test 3: sync_index skips rebuild when schema matches config
# -----------------------------------------------------------------------------

def test_sync_index_skips_rebuild_when_schema_matches(
    tmp_path: Path, isolated_db_modules
):
    """Happy path: DB already at current config dim. sync_index() must NOT
    print the dim-mismatch rebuild message. (Model-change message stays
    separate -- covered by a different check.)"""
    db_path = tmp_path / "fresh.db"
    empty_memory = tmp_path / "empty_memory"
    empty_memory.mkdir()

    # Paths-only isolation: dims/model stay at the real config values, but
    # the fresh import points db/memory_index at the tmp DB + empty memory
    # dir (import-time bindings — see tests/module_isolation.py).
    iso = isolated_db_modules(
        DATABASE_PATH=db_path,
        DATABASE_URL="",  # force SQLite
        MEMORY_DIR=empty_memory,
    )
    db_mod = iso.db
    mi = iso.memory_index

    # Initialize the DB once -- this bakes the current EMBEDDING_DIMENSIONS
    # into vec_chunks AND seeds meta rows with the current model.
    db_first = db_mod.SQLiteMemoryDB(db_path=str(db_path))
    try:
        db_first.init_schema()
    finally:
        db_first.close()

    stdout_buf = io.StringIO()
    with redirect_stdout(stdout_buf):
        mi.sync_index(memory_dir=empty_memory, generate_embeddings=False)
    output = stdout_buf.getvalue()

    assert "Embedding dim mismatch" not in output, (
        f"dim-mismatch message fired unexpectedly on matching schema: {output!r}"
    )
    assert "Model changed" not in output, (
        f"model-change message fired unexpectedly on matching meta: {output!r}"
    )
