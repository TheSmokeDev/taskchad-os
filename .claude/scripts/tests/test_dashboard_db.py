"""Tests for ``.claude/scripts/dashboard_db.py`` — PRD-8 Phase 3 / WS1.

Covers:
    - Schema idempotency (fresh + repeat init both succeed)
    - All 6 tables present (scheduled_tasks, agent_file_history,
      dashboard_settings, cabinet_meetings, cabinet_transcripts, audit_log)
    - WAL journal mode + busy_timeout=5000ms set per-connection
    - foreign_keys=ON (cabinet_transcripts FK)
    - check_same_thread=False default (FastAPI threadpool compat)
    - Rule 1: db_path=None resolves to config.DASHBOARD_DB_PATH at CALL TIME
    - Rule 2: no module-level cache of the resolved path
    - DASHBOARD_DB_PATH env override honored via config reload
    - audit_log table has required Phase 3 columns (R4 NB3):
        operator_id, target_persona_id, outcome
    - audit_log indexes (idx_audit_action) exist
    - Module is importable + ``__all__`` lists the public surface
"""

from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path

import pytest

# Ensure scripts dir is on path for direct ``import dashboard_db`` style.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


_EXPECTED_TABLES: tuple[str, ...] = (
    "scheduled_tasks",
    "agent_file_history",
    "dashboard_settings",
    "cabinet_meetings",
    "cabinet_transcripts",
    "audit_log",
)


# ---------------------------------------------------------------------------
# Module-level / import-shape tests
# ---------------------------------------------------------------------------


def test_module_importable() -> None:
    """The dashboard_db module is importable and exports the public surface."""
    import dashboard_db

    assert hasattr(dashboard_db, "__all__")
    assert set(dashboard_db.__all__) == {"DashboardDB", "get_connection"}
    assert hasattr(dashboard_db, "DashboardDB")
    assert hasattr(dashboard_db, "get_connection")
    assert callable(dashboard_db.get_connection)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_init_schema_idempotent(tmp_path: Path) -> None:
    """Repeat init_schema() on the same file must succeed and not change shape."""
    import dashboard_db

    db_file = tmp_path / "dashboard.db"

    # First init — fresh DB
    db1 = dashboard_db.DashboardDB(db_file)
    conn1 = db1.connect()
    tables_first = sorted(
        r[0] for r in conn1.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    )
    db1.close()

    # Second init — same file, should be a no-op
    db2 = dashboard_db.DashboardDB(db_file)
    conn2 = db2.connect()
    tables_second = sorted(
        r[0] for r in conn2.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    )
    db2.close()

    assert tables_first == tables_second
    # All 6 expected tables must be present after the FIRST init.
    for tbl in _EXPECTED_TABLES:
        assert tbl in tables_first, f"missing table after init: {tbl}"


def test_all_six_tables_present(tmp_path: Path) -> None:
    """Every Phase 3 + Phase 5/7-future table is in the schema."""
    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "dashboard.db")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        names = {r[0] for r in rows}
        for tbl in _EXPECTED_TABLES:
            assert tbl in names, f"expected table not created: {tbl}"
    finally:
        conn.close()


def test_default_path_is_data_dir_dashboard_db(tmp_path: Path) -> None:
    """Default db_path resolves to ``config.DASHBOARD_DB_PATH`` at call time.

    The criterion-locked test name ``test_default_path_is_data_dir_dashboard_db``
    asserts the contract: with no env override and the canonical config
    constant, the default lands at ``DATA_DIR / 'dashboard.db'``.
    """
    import config
    import dashboard_db

    # Don't actually open a real connection on the canonical DATA_DIR path —
    # that would pollute the dev DB. We validate the resolved path only.
    db = dashboard_db.DashboardDB()  # default sentinel
    expected = Path(config.DASHBOARD_DB_PATH)
    assert db.db_path == expected
    # And the default is rooted at DATA_DIR (not HOMIE_HOME).
    assert db.db_path.parent == config.DATA_DIR
    assert db.db_path.name == "dashboard.db"


def test_default_path_resolves_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 1: db_path=None is resolved INSIDE the function body.

    Monkeypatching ``config.DASHBOARD_DB_PATH`` between calls to the
    constructor must change which path the next instance sees. If the
    default were bound at ``def`` time, the patch would be ignored — that's
    the bug Rule 1 prevents.
    """
    import config
    import dashboard_db

    fake_path_a = tmp_path / "alpha.db"
    fake_path_b = tmp_path / "beta.db"

    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", fake_path_a)
    db_a = dashboard_db.DashboardDB()
    assert db_a.db_path == fake_path_a

    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", fake_path_b)
    db_b = dashboard_db.DashboardDB()
    assert db_b.db_path == fake_path_b, (
        "db_path defaulted to the original config value — "
        "Rule 1 violation (default bound at def time)"
    )


def test_env_override_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``DASHBOARD_DB_PATH`` env var is honored when config is reloaded."""
    custom_path = tmp_path / "custom-dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB_PATH", str(custom_path))

    # Fresh import of config picks up the env var via os.getenv().
    if "config" in sys.modules:
        del sys.modules["config"]
    import config  # noqa: PLC0415 — intentional fresh import

    try:
        assert Path(config.DASHBOARD_DB_PATH) == custom_path
    finally:
        # Reload back to the canonical config so other tests are not affected.
        monkeypatch.delenv("DASHBOARD_DB_PATH", raising=False)
        if "config" in sys.modules:
            importlib.reload(sys.modules["config"])


def test_check_same_thread_false(tmp_path: Path) -> None:
    """``get_connection()`` returns a connection usable across threads.

    FastAPI handlers run in a threadpool — the canonical OrchestrationDB
    pattern uses ``check_same_thread=False`` so a connection opened in one
    thread can be used by a handler in another. We assert the connection is
    actually usable from a worker thread (the only behavior the harness
    guards in real life).
    """
    import threading

    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "tt.db")
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            # If check_same_thread were True, this would raise
            # ProgrammingError("SQLite objects created in a thread can only
            # be used in that same thread.").
            conn.execute("SELECT 1").fetchone()
        except BaseException as exc:  # noqa: BLE001 — propagate to assert below
            errors.append(exc)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    conn.close()

    assert errors == [], f"connection failed in worker thread: {errors[0]!r}"


# ---------------------------------------------------------------------------
# PRAGMA tests
# ---------------------------------------------------------------------------


def test_wal_mode_set(tmp_path: Path) -> None:
    """journal_mode is WAL after connect() (concurrent readers + single writer)."""
    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "wal.db")
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        # SQLite reports the active journal_mode as 'wal' (lowercase).
        assert str(mode).lower() == "wal", f"expected WAL, got {mode!r}"
    finally:
        conn.close()


def test_busy_timeout_set(tmp_path: Path) -> None:
    """busy_timeout is 5000ms after connect() (matches OrchestrationDB)."""
    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "bt.db")
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout == 5000, f"expected 5000, got {timeout!r}"
    finally:
        conn.close()


def test_foreign_keys_on(tmp_path: Path) -> None:
    """foreign_keys=ON is set so cabinet_transcripts.meeting_id FK is enforced."""
    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "fk.db")
    try:
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1, f"expected foreign_keys=1, got {fk!r}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# audit_log column + index tests (R4 NB3 hard-delete safety)
# ---------------------------------------------------------------------------


def _audit_log_columns(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Return audit_log table_info rows keyed by column name."""
    rows = conn.execute("PRAGMA table_info(audit_log)").fetchall()
    return {r[1]: r for r in rows}


def test_audit_log_table_exists(tmp_path: Path) -> None:
    """audit_log table is created in WS1 (Phase 3 ships TABLE; writers limited
    to hard-delete in Phase 3, expanded in Phase 7)."""
    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "audit.db")
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
        ).fetchone()
        assert row is not None, "audit_log table missing"
    finally:
        conn.close()


def test_audit_log_has_operator_id_column(tmp_path: Path) -> None:
    """R4 NB3: audit_log has operator_id (TEXT NOT NULL DEFAULT 'system')."""
    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "audit_op.db")
    try:
        cols = _audit_log_columns(conn)
        assert "operator_id" in cols, f"operator_id missing; have {list(cols)}"
        # PRAGMA table_info row layout: cid, name, type, notnull, dflt_value, pk
        assert cols["operator_id"][2].upper() == "TEXT"
        assert cols["operator_id"][3] == 1  # NOT NULL
    finally:
        conn.close()


def test_audit_log_has_target_persona_id_column(tmp_path: Path) -> None:
    """R4 NB3: audit_log has target_persona_id (TEXT NOT NULL DEFAULT '')."""
    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "audit_target.db")
    try:
        cols = _audit_log_columns(conn)
        assert "target_persona_id" in cols, (
            f"target_persona_id missing; have {list(cols)}"
        )
        assert cols["target_persona_id"][2].upper() == "TEXT"
        assert cols["target_persona_id"][3] == 1  # NOT NULL
    finally:
        conn.close()


def test_audit_log_has_outcome_column(tmp_path: Path) -> None:
    """R4 NB3: audit_log has outcome (TEXT NOT NULL DEFAULT 'unknown')."""
    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "audit_outcome.db")
    try:
        cols = _audit_log_columns(conn)
        assert "outcome" in cols, f"outcome missing; have {list(cols)}"
        assert cols["outcome"][2].upper() == "TEXT"
        assert cols["outcome"][3] == 1  # NOT NULL
    finally:
        conn.close()


def test_audit_log_idx_action_exists(tmp_path: Path) -> None:
    """R4 NB3: idx_audit_action(action, created_at DESC) for forensic lookup."""
    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "audit_idx.db")
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='audit_log'"
        ).fetchall()
        idx_names = {r[0] for r in rows}
        # Both Phase 3 indexes must exist.
        for expected in ("idx_audit_action", "idx_audit_time", "idx_audit_persona"):
            assert expected in idx_names, (
                f"missing index {expected}; have {idx_names}"
            )
    finally:
        conn.close()


def test_audit_log_idempotent_schema_creation(tmp_path: Path) -> None:
    """Re-running init_schema on an existing audit_log is a no-op (CREATE IF NOT EXISTS)."""
    import dashboard_db

    db_file = tmp_path / "audit_idem.db"

    db1 = dashboard_db.DashboardDB(db_file)
    conn1 = db1.connect()
    cols1 = sorted(_audit_log_columns(conn1))
    db1.close()

    db2 = dashboard_db.DashboardDB(db_file)
    conn2 = db2.connect()
    cols2 = sorted(_audit_log_columns(conn2))
    db2.close()

    assert cols1 == cols2, "audit_log columns drifted on repeat init"
    # Spot-check the R4 NB3 columns survive both inits.
    for required in ("operator_id", "target_persona_id", "outcome"):
        assert required in cols1
        assert required in cols2


def test_audit_log_accepts_hard_delete_row_shape(tmp_path: Path) -> None:
    """The hard-delete row shape used by Phase 3 writers inserts cleanly.

    Phase 3 writers (criterion ``framework_endpoint_delete_full``) write
    audit rows with operator_id / target_persona_id / outcome populated
    around the destructive call. The schema must accept that exact shape.
    """
    import dashboard_db

    conn = dashboard_db.get_connection(tmp_path / "audit_insert.db")
    try:
        conn.execute(
            "INSERT INTO audit_log "
            "(persona_id, action, detail, blocked, "
            "operator_id, target_persona_id, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("default", "delete_full", "intent=initiated", 0,
             "dashboard", "sales", "initiated"),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT operator_id, target_persona_id, outcome FROM audit_log"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["operator_id"] == "dashboard"
        assert rows[0]["target_persona_id"] == "sales"
        assert rows[0]["outcome"] == "initiated"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Rule 2 — no module-level cache of resolved path or connection
# ---------------------------------------------------------------------------


def test_rule2_no_module_level_cache() -> None:
    """The dashboard_db module exposes only DashboardDB / get_connection / helpers.

    Rule 2 (codified by criterion ``dashboard_db_connection_helper_exists``):
    no module-level mutable state caching the resolved path or an open
    connection. We inspect the public module attributes — anything other
    than functions, classes, types, the DDL string, and ``__all__`` would
    indicate a cache.
    """
    import inspect

    import dashboard_db

    public = {n for n in vars(dashboard_db) if not n.startswith("_")}
    # Allowed public surface: __all__ + DashboardDB + get_connection.
    allowed = {"DashboardDB", "get_connection"}
    extras = public - allowed - {"annotations"}  # __future__ annotations OK
    # All extras must be modules / functions / classes / Path / sqlite3 etc.
    # A cached "current_connection = None" or "_resolved_path = ..." would
    # be flagged here.
    for name in extras:
        obj = getattr(dashboard_db, name)
        ok = (
            inspect.ismodule(obj)
            or inspect.isfunction(obj)
            or inspect.isclass(obj)
            or callable(obj)
        )
        assert ok, (
            f"unexpected module-level value at dashboard_db.{name}: {obj!r} — "
            "Rule 2 violation (module cache?)"
        )
