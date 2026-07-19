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


def _stage_vec_chunks_at_dim(db_path: Path, dim: int) -> None:
    """Create a vec_chunks virtual table at a specific dim with NO other tables
    — stages the pre-migration physical state the guard must detect (bypasses
    init_schema(), so meta stays empty too)."""
    import sqlite_vec

    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute(f"CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[{dim}])")
    conn.commit()
    conn.close()


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
    _stage_vec_chunks_at_dim(db_path, 512)

    # Confirm the staging worked: vec_chunks at 512, no meta table.
    import sqlite_vec

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
        mi.sync_index(
            memory_dir=empty_memory, generate_embeddings=False, db_path=db_path
        )
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
        mi.sync_index(
            memory_dir=empty_memory, generate_embeddings=False, db_path=db_path
        )
    output = stdout_buf.getvalue()

    assert "Embedding dim mismatch" not in output, (
        f"dim-mismatch message fired unexpectedly on matching schema: {output!r}"
    )
    assert "Model changed" not in output, (
        f"model-change message fired unexpectedly on matching meta: {output!r}"
    )


# -----------------------------------------------------------------------------
# Test 5-7 (#136): reindex_file (the single-file entrypoint) must port the same
# physical-schema dim-drift guard as sync_index. Without it, an embedding-model
# migration makes reindex_file's vec insert fail shape-mismatch, callers
# (memory_flush._reindex_episode, entity_extractor, video_learning) swallow it
# as "non-fatal", and the file silently drops out of recall.
# -----------------------------------------------------------------------------


def _vault_paths(tmp_path: Path):
    """<root>/memory + sibling <root>/data — the profile layout resolve_db_path
    maps to <root>/data/memory.db (no config override needed for the path)."""
    root = tmp_path / "vault"
    mem = root / "memory"
    data = root / "data"
    mem.mkdir(parents=True)
    data.mkdir(parents=True)
    return mem, data / "memory.db"


def test_reindex_file_detects_drift_and_rebuilds(tmp_path: Path, isolated_db_modules):
    """DB staged at dim=512, config flipped to 1024 → reindex_file must detect
    the drift via physical schema and delegate to sync_index(force_rebuild=True),
    leaving vec_chunks at the new dim. 1024 is deliberately NOT the real default
    (768) so a broken isolation fixture fails loudly instead of passing by luck."""
    from recall_service import reindex_file

    mem, db_path = _vault_paths(tmp_path)
    _stage_vec_chunks_at_dim(db_path, 512)

    iso = isolated_db_modules(
        DATABASE_URL="",  # force SQLite
        EMBEDDING_DIMENSIONS=1024,
        EMBEDDING_MODEL="new-model-v2",
    )
    # Liveness: the patched value must reach the fresh modules.
    assert iso.memory_index.EMBEDDING_DIMENSIONS == 1024

    # file_path is intentionally never created — the drift branch returns before
    # _index_file is reached, proving it rebuilds the WHOLE index, not one file.
    stdout_buf = io.StringIO()
    with redirect_stdout(stdout_buf):
        chunks = reindex_file(mem / "episode.md", mem, generate_embeddings=True)
    output = stdout_buf.getvalue()

    # Operator receipt fired (the migration rebuild is not silent).
    assert "reindex_file: embedding dim mismatch" in output, (
        f"expected drift receipt in stdout, got: {output!r}"
    )
    assert "vec schema=512" in output
    assert "config=1024" in output
    assert chunks == 0, "empty vault (file never created) must reindex to zero chunks"

    # The real acceptance criterion: vec_chunks recreated at the migrated dim.
    db_after = iso.db.SQLiteMemoryDB(db_path=str(db_path))
    try:
        assert db_after.get_actual_embedding_dim() == 1024, (
            "reindex_file must rebuild vec_chunks at the new dim on drift"
        )
    finally:
        db_after.close()


def test_reindex_file_no_embeddings_skips_rebuild(
    tmp_path: Path, isolated_db_modules, monkeypatch
):
    """generate_embeddings=False must NEVER trigger a rebuild, even under drift:
    a keyword-only reindex writes no vectors (no shape-mismatch), and a
    sync_index(generate_embeddings=False, force_rebuild=True) would bulk_clear
    and rebuild with NO vectors — destroying semantic search. vec schema stays
    at 512."""
    from recall_service import reindex_file

    mem, db_path = _vault_paths(tmp_path)
    (mem / "note.md").write_text("# Note\n\nSome recallable content.\n", encoding="utf-8")
    _stage_vec_chunks_at_dim(db_path, 512)

    iso = isolated_db_modules(
        DATABASE_URL="",
        EMBEDDING_DIMENSIONS=1024,  # drift is present, but MUST be ignored
        EMBEDDING_MODEL="new-model-v2",
    )

    # Guard: the drift branch (which imports + calls sync_index) must NOT run.
    def _boom(*args, **kwargs):
        raise AssertionError(
            "sync_index rebuild must not fire for generate_embeddings=False"
        )

    monkeypatch.setattr(iso.memory_index, "sync_index", _boom)

    chunks = reindex_file(mem / "note.md", mem, generate_embeddings=False)
    assert chunks >= 1  # the single file was indexed keyword-only

    db_after = iso.db.SQLiteMemoryDB(db_path=str(db_path))
    try:
        assert db_after.get_actual_embedding_dim() == 512, (
            "vec schema must stay at 512 — no rebuild without embeddings"
        )
    finally:
        db_after.close()


def test_reindex_file_fresh_db_no_rebuild(
    tmp_path: Path, isolated_db_modules, monkeypatch
):
    """No vec_chunks table yet → get_actual_embedding_dim() is None → the guard
    is skipped (actual_dim is None) and the normal single-file path runs, never
    delegating to sync_index."""
    from recall_service import reindex_file

    mem, db_path = _vault_paths(tmp_path)
    (mem / "note.md").write_text("# Note\n\nFresh vault content.\n", encoding="utf-8")

    iso = isolated_db_modules(DATABASE_URL="")

    def _boom(*args, **kwargs):
        raise AssertionError("fresh DB (actual_dim None) must not trigger a rebuild")

    monkeypatch.setattr(iso.memory_index, "sync_index", _boom)

    chunks = reindex_file(mem / "note.md", mem, generate_embeddings=False)
    assert chunks >= 1

    db_after = iso.db.SQLiteMemoryDB(db_path=str(db_path))
    try:
        # Fresh init at current config dim — created by the normal path, not None.
        assert db_after.get_actual_embedding_dim() is not None
    finally:
        db_after.close()


def test_reindex_episode_drift_does_not_swallow_as_nonfatal(
    tmp_path: Path, isolated_db_modules, monkeypatch
):
    """The exact caller named in #136: memory_flush._reindex_episode must
    reach the guard's rebuild path, not its own except-Exception fallback.

    The episode file is intentionally never created (same reasoning as
    test_reindex_file_detects_drift_and_rebuilds): the drift branch delegates
    to a whole-vault sync_index() that would otherwise try to generate real
    embeddings via the fake EMBEDDING_MODEL used to stage drift."""
    import memory_flush

    mem, db_path = _vault_paths(tmp_path)
    episode = mem / "episode.md"
    _stage_vec_chunks_at_dim(db_path, 512)

    iso = isolated_db_modules(
        DATABASE_URL="", EMBEDDING_DIMENSIONS=1024, EMBEDDING_MODEL="new-model-v2"
    )
    monkeypatch.setattr(memory_flush, "MEMORY_DIR", mem)

    stdout_buf = io.StringIO()
    with redirect_stdout(stdout_buf):
        memory_flush._reindex_episode(episode)
    output = stdout_buf.getvalue()

    assert "Episode reindex failed (non-fatal)" not in output, (
        f"the caller must not fall into its except-Exception path on drift: {output!r}"
    )
    assert "Episode reindexed" in output
