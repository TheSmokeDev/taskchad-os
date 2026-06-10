"""Live Postgres/pgvector integration tests for issues #24 and #25.

First live-DB integration tests in this repo. The mock suite
(test_postgres_dim_migration.py) asserts the SQL *sequence* emitted by
PostgresMemoryDB.bulk_clear(); these tests run the same code paths against a
real pgvector container and verify the behaviors mocks cannot:

- #24: transactional DROP INDEX (hnsw) + ALTER TABLE DROP/ADD COLUMN +
  pgvector OID re-registration + CREATE INDEX, then a real 768-d insert
  succeeding after a vector(512) -> vector(768) migration, including
  psycopg3 prepared-statement survival across the schema change.
- #25: get_actual_embedding_dim() decoding pg_attribute.atttypmod against a
  real column at 512 and after recreate at 768, and returning None (not
  raising) when the chunks table does not exist. The first live run of these
  tests caught a real production bug: pgvector stores the dimension DIRECTLY
  in atttypmod (vector(512) -> atttypmod 512, no VARHDRSZ +4 offset), but
  db.py decoded `atttypmod - 4` -- making every Postgres sync_index() see
  false dim drift (764 != 768) and force a destructive full rebuild. The
  catalog-level encoding is pinned by a raw pg_attribute assertion below so
  a pgvector upgrade changing the encoding fails loudly.

Infrastructure: spins up a throwaway pgvector container (default image
pgvector/pgvector:pg17, override via THEHOMIE_PGTEST_IMAGE) on a per-run
free loopback port (override via THEHOMIE_PGTEST_PORT) with a pid-unique
container name, so concurrent runs from parallel git worktrees cannot
collide. Tests skip gracefully when docker or psycopg is unavailable. Each
test gets its own freshly created database.

Dimension control: db.py imports EMBEDDING_DIMENSIONS at module level and
both init_schema() and bulk_clear() resolve it from the db module namespace
at call time (f-strings), so monkeypatch.setattr(db, "EMBEDDING_DIMENSIONS",
N) is the exact production lever for simulating a dim swap. config.py
hardcodes 768; PostgresMemoryDB takes only a DSN.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

psycopg = pytest.importorskip("psycopg")

def _free_port() -> int:
    """OS-assigned free loopback port (race window between probe and
    docker bind is accepted — collisions surface as a container start
    failure, not silent cross-talk)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Per-run unique name + port so concurrent runs (parallel git worktrees are
# this repo's documented workflow) cannot pre-clean each other's container
# or collide on the host port.
_CONTAINER_NAME = f"thehomie-pgtest-i24-{os.getpid()}"
_PORT = int(os.environ.get("THEHOMIE_PGTEST_PORT", "0")) or _free_port()
_ADMIN_DSN = f"postgresql://postgres:test@127.0.0.1:{_PORT}/postgres"
_DEFAULT_IMAGE = "pgvector/pgvector:pg17"


# ---------------------------------------------------------------------------
# Container lifecycle
# ---------------------------------------------------------------------------

def _docker_available() -> str | None:
    """Return a skip reason if docker is unusable, else None."""
    if shutil.which("docker") is None:
        return "docker CLI not found"
    try:
        info = subprocess.run(["docker", "info"], capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"docker daemon not reachable: {exc}"
    if info.returncode != 0:
        return "docker daemon not running"
    return None


def _wait_until_ready(dsn: str, deadline_s: float = 90.0) -> None:
    """Block until Postgres accepts connections.

    Requires TWO consecutive successful connects ~1s apart: the official
    postgres entrypoint starts a temporary server during initdb and then
    restarts, so a single successful connect can land on the temp server
    and the next one drops (known single-connect readiness flake).
    """
    deadline = time.monotonic() + deadline_s
    consecutive = 0
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            consecutive += 1
        except Exception as exc:  # noqa: BLE001 - any failure resets readiness
            consecutive = 0
            last_err = exc
        if consecutive >= 2:
            return
        time.sleep(1.0)
    pytest.fail(
        f"postgres container did not become ready within {deadline_s}s: {last_err}"
    )


@pytest.fixture(scope="module")
def pg_container() -> Iterator[str]:
    """Start a throwaway pgvector container; always tear it down."""
    reason = _docker_available()
    if reason is not None:
        pytest.skip(reason)

    image = os.getenv("THEHOMIE_PGTEST_IMAGE", _DEFAULT_IMAGE)
    # The docker run itself sits INSIDE the try: on a slow image pull,
    # subprocess.TimeoutExpired can fire after the daemon already created
    # the container — the finally still removes it.
    try:
        run = subprocess.run(
            [
                "docker", "run", "--rm", "-d",
                "--name", _CONTAINER_NAME,
                "-e", "POSTGRES_PASSWORD=test",
                "-p", f"127.0.0.1:{_PORT}:5432",
                image,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if run.returncode != 0:
            pytest.skip(f"could not start pgvector container: {run.stderr.strip()}")
        _wait_until_ready(_ADMIN_DSN)
        yield _ADMIN_DSN
    finally:
        subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)


@pytest.fixture()
def pg_dsn(pg_container: str) -> Iterator[str]:
    """Per-test isolated database inside the shared container."""
    dbname = f"testdb_{uuid.uuid4().hex[:8]}"
    # CREATE DATABASE cannot run inside a transaction -> autocommit admin conn.
    with psycopg.connect(pg_container, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{dbname}"')
    yield f"postgresql://postgres:test@127.0.0.1:{_PORT}/{dbname}"
    with psycopg.connect(pg_container, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')


@pytest.fixture()
def pg_db(pg_dsn: str):
    """PostgresMemoryDB bound to the per-test database; closed before drop."""
    from db import PostgresMemoryDB

    pdb = PostgresMemoryDB(pg_dsn)
    yield pdb
    try:
        pdb.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_chunk(pdb, path: str = "a.md", content: str = "hello world") -> int:
    pdb.upsert_file(path, "hash", 0, 1, 0)
    return pdb.insert_chunk(path, 1, 2, "title", content, "hash", 0)


# ---------------------------------------------------------------------------
# #24 — bulk_clear() dim migration against live pgvector
# ---------------------------------------------------------------------------

def test_bulk_clear_migrates_live_vector_column_512_to_768(pg_db, monkeypatch):
    """Seed vector(512) + data, swap dims to 768, bulk_clear(), insert 768-d.

    Exercises live what the mocks only assert textually: DROP INDEX hnsw +
    ALTER TABLE DROP/ADD COLUMN + register_vector OID refresh + CREATE INDEX
    in ONE transaction on the SAME connection, then real inserts and a
    vector_search through the recreated partial HNSW index.
    """
    import db as db_module

    monkeypatch.setattr(db_module, "EMBEDDING_DIMENSIONS", 512)
    pg_db.init_schema()
    cid = _seed_chunk(pg_db)
    # Cross psycopg3's auto-prepare threshold (5) BEFORE the ALTER TABLE so
    # the UPDATE statement is server-side prepared when the schema changes —
    # exercising prepared-statement cache invalidation (#24 gap list).
    for _ in range(6):
        pg_db.insert_vector(cid, np.full(512, 0.1, dtype=np.float32))
    pg_db.commit()
    assert pg_db.get_actual_embedding_dim() == 512

    # The dim swap: production reads db.EMBEDDING_DIMENSIONS at call time.
    monkeypatch.setattr(db_module, "EMBEDDING_DIMENSIONS", 768)
    pg_db.bulk_clear()

    assert pg_db.get_actual_embedding_dim() == 768
    stats = pg_db.get_stats()
    assert stats["chunks"] == 0
    assert stats["files"] == 0

    # The #24 acceptance criterion: a real 768-d insert succeeds post-swap.
    cid2 = _seed_chunk(pg_db, path="b.md", content="post migration chunk")
    for _ in range(6):  # cross the prepare threshold again on the new column
        pg_db.insert_vector(cid2, np.full(768, 0.2, dtype=np.float32))
    pg_db.commit()

    # Negative control: the recreated column genuinely enforces vector(768).
    # Without this, a test against a typeless/typo'd column could pass
    # vacuously. The 512-d insert must be rejected by live pgvector typing
    # (float8[] -> vector AS ASSIGNMENT cast checks the typmod).
    with pytest.raises(psycopg.Error):
        pg_db.insert_vector(cid2, np.full(512, 0.3, dtype=np.float32))
    pg_db._conn.rollback()  # the failed statement aborted the txn; clear it

    # The recreated partial HNSW index + refreshed OID cache serve queries.
    results = pg_db.vector_search(np.full(768, 0.2, dtype=np.float32), limit=5)
    assert len(results) == 1
    assert results[0]["file_path"] == "b.md"
    assert results[0]["score"] == pytest.approx(1.0)  # distance 0 -> score 1


# ---------------------------------------------------------------------------
# #25 — get_actual_embedding_dim() against live pg_attribute
# ---------------------------------------------------------------------------

def test_get_actual_embedding_dim_decodes_atttypmod_at_512(pg_db, monkeypatch):
    """vector(512) column -> atttypmod 512 (pgvector: typmod IS the dim)."""
    import db as db_module

    monkeypatch.setattr(db_module, "EMBEDDING_DIMENSIONS", 512)
    pg_db.init_schema()

    # Pin the catalog-level encoding independently of the function under
    # test (not circular): pgvector's vector_typmod_in stores the dimension
    # directly -- NO VARHDRSZ +4 offset (that applies to varchar typmods).
    # If a pgvector upgrade ever changes this encoding, fail loudly here.
    cur = pg_db._get_conn().cursor()
    cur.execute(
        """SELECT atttypmod FROM pg_attribute
        WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"""
    )
    assert cur.fetchone()[0] == 512

    assert pg_db.get_actual_embedding_dim() == 512


def test_get_actual_embedding_dim_tracks_recreate_at_768(pg_db, monkeypatch):
    """Recreate the column at vector(768) via the production path -> 768."""
    import db as db_module

    monkeypatch.setattr(db_module, "EMBEDDING_DIMENSIONS", 512)
    pg_db.init_schema()
    assert pg_db.get_actual_embedding_dim() == 512

    monkeypatch.setattr(db_module, "EMBEDDING_DIMENSIONS", 768)
    pg_db.bulk_clear()  # the production recreate path, not raw test DDL
    assert pg_db.get_actual_embedding_dim() == 768


def test_get_actual_embedding_dim_returns_none_when_table_missing(pg_db):
    """Fresh database, init_schema never called: None, not an exception."""
    assert pg_db.get_actual_embedding_dim() is None

    # LATENT BUG DOCUMENTATION (upstream candidate — intentionally NOT fixed
    # in this test slice): 'chunks'::regclass raises UndefinedTable inside
    # the autocommit=False transaction and db.py's bare `except Exception:
    # return None` swallows it WITHOUT rollback, leaving the connection in
    # InFailedSqlTransaction. A second call only returns None because the
    # poisoned-transaction error is ALSO swallowed by the same except.
    assert pg_db.get_actual_embedding_dim() is None

    # Prove the transaction really is poisoned: any non-swallowing statement
    # on this connection now fails. A dim-drift guard calling
    # get_actual_embedding_dim() before init_schema() on Postgres would
    # abort the transaction and break the subsequent init_schema().
    with pytest.raises(psycopg.errors.InFailedSqlTransaction):
        pg_db._get_conn().execute("SELECT 1")

    # Clear the aborted txn so fixture teardown (close + DROP DATABASE WITH
    # (FORCE)) doesn't race an aborted session.
    pg_db._conn.rollback()
