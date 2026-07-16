"""Regression tests for #122: SQLite 'database is locked' under concurrent
memory_index runs.

Covers 7 distinct paths through the retry-on-locked fix in db.py:
1. Real thread contention against init_schema()/upsert_meta() on one on-disk
   db file — proves concurrent callers no longer crash each other.
2. Injected OperationalError that resolves within the retry budget — proves
   the retry loop actually recovers (deterministic, no timing flakiness).
3. Injected OperationalError that never resolves — proves retries are
   BOUNDED and the error still propagates (fail-open semantics upstream are
   preserved, not silently swallowed forever); also asserts the exhausted
   attempt count matches the configured budget exactly.
4. A non-lock OperationalError — proves it is NOT retried and raises
   immediately (a real schema/SQL bug must never be masked as contention).
5. A locked error during _get_conn()'s own connection-setup sequence on a
   FRESH SQLiteMemoryDB (sqlite3.connect/PRAGMA path) — proves the deferred
   self._conn assignment never caches a half-configured connection between
   retries, independent of the meta-write retry path.
6. A locked error on the DDL statement inside _create_tables_raw() — proves
   the schema-creation leaf's own retry recovers in isolation, independent of
   the meta-write retry path (init_schema() itself is deliberately
   undecorated; see db.py's SQLiteMemoryDB.init_schema).
7. A locked error raised from self._conn.rollback() itself while the retry
   loop is recovering from a locked write — proves the best-effort
   ``except sqlite3.OperationalError: pass`` around rollback lets the retry
   proceed to its next attempt instead of blowing up on a failed rollback.

Fault injection uses a delegating connection PROXY rather than patching
``sqlite3.Connection.execute`` directly: on CPython that method belongs to an
immutable C type and cannot be reassigned ("cannot set 'execute' attribute of
immutable type 'sqlite3.Connection'"). The proxy intercepts only ``execute``
(and optionally ``rollback``) and forwards everything else to the real
connection, so the retry decorator (which reads ``db._conn`` and calls
``rollback``) still behaves normally.
"""

from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _make_sqlite_db(db_path: Path):
    from db import SQLiteMemoryDB

    return SQLiteMemoryDB(db_path=str(db_path))


class _FaultyConnProxy:
    """Wrap a real sqlite3.Connection, delegating everything except execute()
    (and, when supplied, rollback()).

    ``fault(sql, args)`` returns an Exception to raise instead of running the
    statement, or None to pass through. Lets a test inject a deterministic
    'database is locked' (or a non-lock error) without touching the immutable
    sqlite3.Connection type. ``rollback_fault()`` (optional) does the same for
    ``rollback()``, to exercise the retry decorator's best-effort rollback
    error handling (db.py:60-64).
    """

    def __init__(self, real, fault, rollback_fault=None):
        self._real = real
        self._fault = fault
        self._rollback_fault = rollback_fault

    def execute(self, sql, *args, **kwargs):
        exc = self._fault(sql, args)
        if exc is not None:
            raise exc
        return self._real.execute(sql, *args, **kwargs)

    def rollback(self):
        if self._rollback_fault is not None:
            exc = self._rollback_fault()
            if exc is not None:
                raise exc
        return self._real.rollback()

    def __getattr__(self, name):
        # _real / _fault live in __dict__, so this only fires for delegated
        # attributes (commit, executescript, close, ...).
        return getattr(self._real, name)


def _wrap_connection(db, fault, rollback_fault=None) -> None:
    """Force a real connection, then swap in the fault-injecting proxy so the
    retry decorator (which reads db._conn) and every method body route their
    execute() calls through it."""
    real = db._get_conn()
    db._conn = _FaultyConnProxy(real, fault, rollback_fault=rollback_fault)


# 1. Real contention -----------------------------------------------------

def test_concurrent_init_schema_survives_thread_contention(tmp_path: Path):
    """N threads, each its own SQLiteMemoryDB connection, all calling
    init_schema() against the SAME on-disk file at once. Before the fix,
    this can legitimately raise 'database is locked' (real OS-level file
    contention — not mocked); after the fix, all callers must succeed."""
    db_path = tmp_path / "concurrent.db"

    def _worker() -> None:
        db = _make_sqlite_db(db_path)
        try:
            db.init_schema()
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_worker) for _ in range(8)]
        for future in as_completed(futures):
            future.result()  # re-raises if the worker raised

    db = _make_sqlite_db(db_path)
    try:
        assert db.get_meta("schema_version") == "1"
    finally:
        db.close()


# 2. Deterministic recovery ------------------------------------------------

def test_retry_recovers_from_transient_lock_error(tmp_path: Path):
    """A locked error on the first 2 attempts, success on the 3rd, must
    return normally (proves the retry loop itself works, independent of
    real OS lock timing).

    The injection is scoped to the FIRST meta upsert ('schema_version') so
    the counter reflects exactly one method's retry cycle. init_schema()
    upserts three meta rows via the same SQL; gating on the key keeps the
    assertion an unambiguous 3 (raise, raise, succeed)."""
    db = _make_sqlite_db(tmp_path / "retry.db")
    try:
        calls = {"n": 0}

        def _fault(sql, args):
            if (
                isinstance(sql, str)
                and sql.startswith("INSERT OR REPLACE INTO meta")
                and args
                and args[0]
                and args[0][0] == "schema_version"
            ):
                calls["n"] += 1
                if calls["n"] < 3:
                    return sqlite3.OperationalError("database is locked")
            return None

        _wrap_connection(db, _fault)
        db.init_schema()  # must not raise

        assert calls["n"] == 3
        assert db.get_meta("schema_version") == "1"
    finally:
        db.close()


# 3. Bounded exhaustion, fail-open preserved -------------------------------

def test_retry_exhausted_still_raises(tmp_path: Path):
    """A lock error on EVERY meta write must still raise OperationalError once
    the retry budget is exhausted -- proves retries are bounded and upstream
    fail-open handling still receives a real failure signal. Also asserts the
    exact attempt count, so a regression that shrinks or removes the retry
    loop (while still eventually raising) shows up as a failing assertion
    instead of a silently-still-green test."""
    from db import _LOCK_RETRY_MAX_ATTEMPTS

    db = _make_sqlite_db(tmp_path / "exhausted.db")
    try:
        calls = {"n": 0}

        def _fault(sql, args):
            if isinstance(sql, str) and sql.startswith("INSERT OR REPLACE INTO meta"):
                calls["n"] += 1
                return sqlite3.OperationalError("database is locked")
            return None

        _wrap_connection(db, _fault)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.init_schema()

        assert calls["n"] == _LOCK_RETRY_MAX_ATTEMPTS
    finally:
        db.close()


def test_retry_deadline_caps_wall_clock(tmp_path: Path, monkeypatch):
    """The retry loop has a total wall-clock budget: with the deadline at 0,
    the FIRST lock error must raise without any further attempts. Guards the
    event-loop-stall class (5 attempts x 30s connect timeout ~= 150s of sync
    blocking inside the chat engine's keyword-recall fallback) and bounds
    nested decorated calls."""
    import db as db_mod

    monkeypatch.setattr(db_mod, "_LOCK_RETRY_DEADLINE_S", 0.0)
    db = _make_sqlite_db(tmp_path / "deadline.db")
    try:
        calls = {"n": 0}

        def _fault(sql, args):
            if isinstance(sql, str) and sql.startswith("INSERT OR REPLACE INTO meta"):
                calls["n"] += 1
                return sqlite3.OperationalError("database is locked")
            return None

        _wrap_connection(db, _fault)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            db.init_schema()

        assert calls["n"] == 1  # deadline exceeded -> no retry attempts
    finally:
        db.close()


# 4. Non-lock errors bypass retry ------------------------------------------

def test_non_lock_operational_error_raises_immediately(tmp_path: Path):
    """A different OperationalError (e.g. a real SQL/schema bug) must raise
    on the FIRST attempt -- retry must never mask a non-contention bug."""
    db = _make_sqlite_db(tmp_path / "notlocked.db")
    try:
        db.init_schema()  # get a real connection/schema first

        def _fault(sql, args):
            if isinstance(sql, str) and sql.startswith("INSERT OR REPLACE INTO meta"):
                return sqlite3.OperationalError("no such table: bogus")
            return None

        _wrap_connection(db, _fault)
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            db.upsert_meta("k", "v")
    finally:
        db.close()


# 5. _get_conn()'s own retry/deferred-assignment path ----------------------

def test_get_conn_recovers_from_locked_error_during_setup(tmp_path: Path, monkeypatch):
    """A lock error during connection setup (sqlite3.connect) on a FRESH
    SQLiteMemoryDB must retry and must never cache a half-configured
    connection between attempts (db.py's _get_conn deferred-assignment fix).

    All other tests in this file force a real connection via _wrap_connection
    before injecting faults, so they never re-enter _get_conn()'s own body --
    this test targets that path directly by patching sqlite3.connect itself,
    since Connection is an immutable C type but the module-level `connect`
    function is a regular, patchable attribute."""
    import db as db_module
    from db import SQLiteMemoryDB

    db = SQLiteMemoryDB(db_path=str(tmp_path / "get_conn_retry.db"))
    real_connect = db_module.sqlite3.connect
    calls = {"n": 0}

    def _flaky_connect(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            assert db._conn is None  # never cached a half-configured connection
            raise db_module.sqlite3.OperationalError("database is locked")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(db_module.sqlite3, "connect", _flaky_connect)
    try:
        conn = db._get_conn()  # must not raise
        assert conn is db._conn
        assert calls["n"] == 3
    finally:
        db.close()


# 6. _create_tables_raw()'s own retry, isolated from the meta-write path ---

def test_create_tables_retry_recovers_from_transient_lock_error(tmp_path: Path):
    """A locked error on the vec_chunks DDL statement -- the fault surface
    inside _create_tables_raw(), decorated independently of upsert_meta now
    that init_schema() itself is undecorated -- must retry and succeed within
    budget, proving the DDL-level retry works standalone rather than only as
    a side effect of the meta-write retry path."""
    db = _make_sqlite_db(tmp_path / "ddl_retry.db")
    try:
        calls = {"n": 0}

        def _fault(sql, args):
            if isinstance(sql, str) and "CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks" in sql:
                calls["n"] += 1
                if calls["n"] < 3:
                    return sqlite3.OperationalError("database is locked")
            return None

        _wrap_connection(db, _fault)
        db.init_schema()  # must not raise

        assert calls["n"] == 3
        assert db.get_meta("schema_version") == "1"
    finally:
        db.close()


# 7. Rollback-during-retry failure does not block the next attempt ---------

def test_rollback_failure_during_retry_does_not_block_next_attempt(tmp_path: Path):
    """If self._conn.rollback() itself raises OperationalError while the
    decorator is recovering from a locked write, the best-effort
    ``except sqlite3.OperationalError: pass`` (db.py:60-64) must swallow it
    and still proceed to the next attempt -- a rollback failure must never
    block or crash the outer retry loop."""
    db = _make_sqlite_db(tmp_path / "rollback_fault.db")
    try:
        calls = {"n": 0}

        def _fault(sql, args):
            if (
                isinstance(sql, str)
                and sql.startswith("INSERT OR REPLACE INTO meta")
                and args
                and args[0]
                and args[0][0] == "schema_version"
            ):
                calls["n"] += 1
                if calls["n"] < 3:
                    return sqlite3.OperationalError("database is locked")
            return None

        rollback_calls = {"n": 0}

        def _rollback_fault():
            rollback_calls["n"] += 1
            if rollback_calls["n"] == 1:
                return sqlite3.OperationalError("database is locked")
            return None

        _wrap_connection(db, _fault, rollback_fault=_rollback_fault)
        db.init_schema()  # must not raise even though the first rollback fails

        assert calls["n"] == 3
        assert rollback_calls["n"] == 2
        assert db.get_meta("schema_version") == "1"
    finally:
        db.close()
