"""Phase 4 / PRP-7d source tagging — comprehensive integration test suite.

Covers WS1-WS4 contracts now on disk:

- WS1 schema + helpers + dataclasses (chat/session.py)
- WS2 source plumbing (cli.py, cli_adapter.py, models.py, engine.py, router.py,
  core_handlers.py, ws_client.py, blog/scheduler.py)
- WS3 thehomie session list/show/resume Click group (chat/cli_session.py)
- WS4 quiet JSON envelope (cli_adapter.py format_final_output +
  build_quiet_error_envelope)

Test categories (matching PRP §"Acceptance criteria" + R1/R2/R3 dispositions):

A. Schema migration (SQLite + Postgres static parity)
B. Helpers + dataclass (normalize_source, Session.source, list_recent,
   get_by_session_id, SessionSummary)
C. CLI surface --source on `thehomie chat` (Choice + persistence)
D. Source plumbing for non-engine writer paths (router, /plan, ws_client,
   blog scheduler)
E. `thehomie session list/show/resume` group (filters, JSON shape, dry-run)
F. Quiet JSON envelope (success / error path field order, profile resolver)
G. SQL injection defense (Click rejection + parameterized SQL)

Sign off: YourAgent (Phase 4 WS5 executor).
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

# Ensure scripts + chat dirs on sys.path (mirrors conftest.py)
_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
for p in [str(_SCRIPTS_DIR), str(_CHAT_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from session import (  # noqa: E402
    SOURCE_HIDDEN_BY_DEFAULT,
    SOURCE_VALUES,
    Session,
    SessionSummary,
    SQLiteSessionStore,
    _assert_source_column_shape,
    _run_source_migration,
    normalize_source,
)
from session_keys import build_session_key  # noqa: E402


# ============================================================================
# Shared fixtures
# ============================================================================


def _legacy_chat_sessions_ddl() -> str:
    """SQL to create a pre-Phase-4 chat_sessions schema WITHOUT the source col.

    Used by migration tests that need to seed an existing database that has
    not yet been touched by the Phase 4 migration. The shape mirrors the
    pre-migration DDL in session.py (line 285-306) minus the source column.
    """
    return (
        "CREATE TABLE chat_sessions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "  session_id TEXT NOT NULL UNIQUE, "
        "  agent_session_id TEXT NOT NULL, "
        "  runtime_session_id TEXT DEFAULT '', "
        "  runtime_provider TEXT DEFAULT 'claude', "
        "  runtime_model TEXT DEFAULT '', "
        "  runtime_profile_key TEXT DEFAULT '', "
        "  platform TEXT NOT NULL, "
        "  channel_id TEXT NOT NULL, "
        "  thread_id TEXT NOT NULL, "
        "  user_id TEXT NOT NULL, "
        "  created_at TEXT NOT NULL, "
        "  updated_at TEXT NOT NULL, "
        "  message_count INTEGER DEFAULT 0, "
        "  total_cost_usd REAL DEFAULT 0.0, "
        "  status TEXT DEFAULT 'active', "
        "  mode TEXT DEFAULT 'execute', "
        "  runtime_lane TEXT DEFAULT 'claude_native', "
        "  tool_call_count INTEGER DEFAULT 0, "
        "  runtime_tool_calls_json TEXT DEFAULT '[]'"
        ")"
    )


def _legacy_chat_sessions_with_nullable_source_ddl() -> str:
    """DDL for a chat_sessions table where source is malformed (nullable, no default).

    Tests use this to seed the malformed-pre-existing-column scenario so the
    migration's post-check (_assert_source_column_shape) trips on NOT NULL,
    default, or NULL/empty rows.
    """
    return (
        "CREATE TABLE chat_sessions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "  session_id TEXT NOT NULL UNIQUE, "
        "  agent_session_id TEXT NOT NULL, "
        "  runtime_session_id TEXT DEFAULT '', "
        "  runtime_provider TEXT DEFAULT 'claude', "
        "  runtime_model TEXT DEFAULT '', "
        "  runtime_profile_key TEXT DEFAULT '', "
        "  platform TEXT NOT NULL, "
        "  channel_id TEXT NOT NULL, "
        "  thread_id TEXT NOT NULL, "
        "  user_id TEXT NOT NULL, "
        "  created_at TEXT NOT NULL, "
        "  updated_at TEXT NOT NULL, "
        "  message_count INTEGER DEFAULT 0, "
        "  total_cost_usd REAL DEFAULT 0.0, "
        "  status TEXT DEFAULT 'active', "
        "  mode TEXT DEFAULT 'execute', "
        "  runtime_lane TEXT DEFAULT 'claude_native', "
        "  tool_call_count INTEGER DEFAULT 0, "
        "  runtime_tool_calls_json TEXT DEFAULT '[]', "
        "  source TEXT"  # NULLABLE, no default — malformed
        ")"
    )


def _legacy_seed_row(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    channel_id: str,
    source_value: str | None = "__omit__",
) -> None:
    """Insert one pre-migration row. ``source_value="__omit__"`` skips the column."""
    now = datetime.now().isoformat()
    if source_value == "__omit__":
        conn.execute(
            "INSERT INTO chat_sessions ("
            "  session_id, agent_session_id, platform, channel_id, thread_id, "
            "  user_id, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, "agent-x", "cli", channel_id, channel_id,
             "user-x", now, now),
        )
    else:
        conn.execute(
            "INSERT INTO chat_sessions ("
            "  session_id, agent_session_id, platform, channel_id, thread_id, "
            "  user_id, created_at, updated_at, source"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, "agent-x", "cli", channel_id, channel_id,
             "user-x", now, now, source_value),
        )


def _patch_chat_db_path(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    """Patch CHAT_DB_PATH everywhere a Phase 4 caller might consume it.

    cli_session does ``from config import CHAT_DB_PATH`` at module-import
    time, so it captures the constant by value. We must patch BOTH
    ``cli_session.CHAT_DB_PATH`` AND ``config.CHAT_DB_PATH`` so:

      - ``store = get_session_store(CHAT_DB_PATH)`` inside cli_session.py
        reads our temp path
      - any ``from config import CHAT_DB_PATH`` inside a function body
        (e.g. cli_adapter.py) also picks up the temp path

    Pure helper — no fixture machinery — so each test is self-contained.
    """
    import cli_session
    import config

    monkeypatch.setattr(cli_session, "CHAT_DB_PATH", db_path)
    monkeypatch.setattr(config, "CHAT_DB_PATH", db_path)


def _make_session(
    *,
    session_id: str | None = None,
    channel_id: str = "chan-1",
    thread_id: str = "thread-1",
    platform: str = "cli",
    source: str = "interactive",
    runtime_session_id: str = "",
) -> Session:
    """Construct a minimal Session for store.create()."""
    if session_id is None:
        session_id = build_session_key(platform, channel_id, thread_id)
    now = datetime.now()
    sess = Session(
        session_id=session_id,
        agent_session_id=runtime_session_id or session_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        user_id="user-x",
        created_at=now,
        updated_at=now,
        source=source,
    )
    return sess


# ============================================================================
# A. Schema migration (WS1)
# ============================================================================


class TestSchemaMigration:
    """Migration applies cleanly, is idempotent, asserts physical shape."""

    def test_fresh_db_adds_source_column_and_index(self, tmp_path):
        """Fresh DB: source column added with TEXT NOT NULL DEFAULT 'interactive'
        + composite index on (source, updated_at DESC)."""
        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)

        with sqlite3.connect(db_path) as conn:
            cols = {r[1]: r for r in conn.execute(
                "PRAGMA table_info(chat_sessions)"
            ).fetchall()}
            assert "source" in cols, "migration must add source column"
            row = cols["source"]
            assert row[2].upper() == "TEXT", f"expected TEXT, got {row[2]!r}"
            assert row[3] == 1, "source must be NOT NULL"
            assert "interactive" in str(row[4] or ""), \
                f"default missing 'interactive': {row[4]!r}"

            indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='chat_sessions'"
                ).fetchall()
            }
            assert "idx_chat_sessions_source_updated" in indexes, (
                "composite index on (source, updated_at DESC) missing"
            )

    def test_existing_db_without_source_is_migrated_and_rows_backfilled(self, tmp_path):
        """Pre-existing rows from a pre-Phase-4 schema get backfilled to 'interactive'.

        REAL pre-migration fixture (R1 M7 / R2 M7 — NOT a "not possible"
        comment). Manually create chat_sessions WITHOUT the source column,
        seed 3 rows, then construct SQLiteSessionStore (triggers _init_db).
        SQLite's ADD COLUMN ... DEFAULT 'interactive' backfills.
        """
        db_path = tmp_path / "chat.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(_legacy_chat_sessions_ddl())
            for i in range(3):
                _legacy_seed_row(
                    conn,
                    session_id=f"cli:c{i}:t{i}",
                    channel_id=f"c{i}",
                )

        SQLiteSessionStore(db_path)

        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT session_id, source FROM chat_sessions ORDER BY session_id"
            ).fetchall()
            assert len(rows) == 3
            for r in rows:
                assert r["source"] == "interactive", (
                    f"existing row {r['session_id']!r} must backfill to "
                    f"'interactive', got {r['source']!r}"
                )

    def test_migration_is_idempotent_on_rerun(self, tmp_path):
        """Constructing the store twice must not raise; schema unchanged."""
        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)
        with sqlite3.connect(db_path) as conn:
            snap1 = conn.execute(
                "PRAGMA table_info(chat_sessions)"
            ).fetchall()
        # Second construction — must succeed and be a no-op.
        SQLiteSessionStore(db_path)
        with sqlite3.connect(db_path) as conn:
            snap2 = conn.execute(
                "PRAGMA table_info(chat_sessions)"
            ).fetchall()
        assert snap1 == snap2, "PRAGMA table_info changed across re-runs"

    def test_malformed_existing_column_with_null_row_raises(self, tmp_path):
        """Pre-existing chat_sessions.source column that allows NULL with a NULL
        row in it must trigger _assert_source_column_shape RuntimeError."""
        db_path = tmp_path / "chat.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(_legacy_chat_sessions_with_nullable_source_ddl())
            _legacy_seed_row(
                conn, session_id="cli:c:t", channel_id="c", source_value=None
            )

        with pytest.raises(RuntimeError) as exc_info:
            SQLiteSessionStore(db_path)
        assert "manual repair" in str(exc_info.value).lower()

    def test_malformed_existing_column_with_empty_string_row_raises(self, tmp_path):
        """Empty-string source row must independently trip the post-check.

        R3 NNM3 paired case: catches a regression where someone deletes
        the `OR source = ''` clause from _assert_source_column_shape.
        """
        db_path = tmp_path / "chat.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(_legacy_chat_sessions_with_nullable_source_ddl())
            _legacy_seed_row(
                conn, session_id="cli:c:t", channel_id="c", source_value=""
            )

        with pytest.raises(RuntimeError) as exc_info:
            SQLiteSessionStore(db_path)
        assert "manual repair" in str(exc_info.value).lower()

    def test_concurrent_alter_race_is_accepted(self, tmp_path):
        """Pure-Python seam: simulate the concurrent-first-boot race.

        R2 NB3 + R3 NNM2 — _alter_executor injection avoids monkeypatching
        sqlite3.Connection.execute (immutable in Python 3.14). Test passes a
        callable that simulates the loser side: it ALTERs the column itself
        (so subsequent post-check sees the column) then raises
        OperationalError("duplicate column name: source"). The migration
        helper must swallow the duplicate-column error and return cleanly.
        """
        db_path = tmp_path / "chat.db"
        # First create the legacy table so source column is missing.
        with sqlite3.connect(db_path) as conn:
            conn.execute(_legacy_chat_sessions_ddl())

        # Open a new connection for the migration helper.
        with sqlite3.connect(db_path) as conn:
            def race_alter(sql: str) -> None:
                # Simulate loser: another process already added the column.
                # Add it ourselves first via a different cursor, then raise.
                conn.execute(sql)  # our ALTER; column now present
                raise sqlite3.OperationalError(
                    "duplicate column name: source"
                )

            # Should NOT raise — the duplicate-column case is the only race
            # the helper is allowed to tolerate.
            _run_source_migration(conn, _alter_executor=race_alter)

            # Post-check would normally run after _run_source_migration —
            # since the race-side ALTER actually added the column, the shape
            # is valid.
            _assert_source_column_shape(conn, backend="sqlite")

    def test_other_alter_error_propagates(self, tmp_path):
        """Locked-DB / wrong-table errors must NOT be silently swallowed.

        The duplicate-column catch is narrow — anything else propagates.
        """
        db_path = tmp_path / "chat.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(_legacy_chat_sessions_ddl())

        with sqlite3.connect(db_path) as conn:
            def locked_alter(_sql: str) -> None:
                raise sqlite3.OperationalError("database is locked")

            with pytest.raises(sqlite3.OperationalError) as exc_info:
                _run_source_migration(conn, _alter_executor=locked_alter)
            assert "locked" in str(exc_info.value).lower()

    # ------------------------------------------------------------------
    # Post-build iteration 1 — F2: duplicate-column race acceptance must
    # cover the alternate spellings ("column already exists: source") that
    # SQLite forks emit for the same benign concurrent-first-boot race.
    # The unconditional post-check (_assert_source_column_shape) already
    # validates the final shape, so the catch should accept all benign
    # variants and ONLY propagate genuinely-different OperationalErrors.
    # ------------------------------------------------------------------

    def test_concurrent_alter_race_accepted_with_already_exists_message(
        self, tmp_path
    ):
        """Race wording variant: ``column already exists: source``.

        Some SQLite forks emit this phrasing for the same benign race the
        mainline ``"duplicate column"`` wording covers. ``_run_source_migration``
        must accept it cleanly and let the post-check validate shape.
        """
        db_path = tmp_path / "chat.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(_legacy_chat_sessions_ddl())

        with sqlite3.connect(db_path) as conn:
            def race_alter(sql: str) -> None:
                # Loser side: another process already added the column.
                # We add it ourselves first so the post-check's PRAGMA
                # sees the column, then raise with the alternate wording.
                conn.execute(sql)
                raise sqlite3.OperationalError("column already exists: source")

            # Must NOT raise — the alternate wording is the same race.
            _run_source_migration(conn, _alter_executor=race_alter)
            # Post-check validates the final shape regardless of wording.
            _assert_source_column_shape(conn, backend="sqlite")

    def test_concurrent_alter_race_accepted_with_duplicate_column_uppercase(
        self, tmp_path
    ):
        """Race classification is case-insensitive (DUPLICATE COLUMN: source)."""
        db_path = tmp_path / "chat.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(_legacy_chat_sessions_ddl())

        with sqlite3.connect(db_path) as conn:
            def race_alter(sql: str) -> None:
                conn.execute(sql)
                raise sqlite3.OperationalError("DUPLICATE COLUMN: source")

            # Must NOT raise — classification lower-cases the message.
            _run_source_migration(conn, _alter_executor=race_alter)
            _assert_source_column_shape(conn, backend="sqlite")

    def test_other_alter_error_not_misidentified_as_race(self, tmp_path):
        """Non-race OperationalErrors must propagate (regression guard).

        ``"table does not exist"`` is a real failure (not a race) — it must
        NOT be swallowed by the broadened race classifier. This is the paired
        counter-test to the two acceptance tests above: prove the broadened
        catch did NOT widen too far.
        """
        db_path = tmp_path / "chat.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute(_legacy_chat_sessions_ddl())

        with sqlite3.connect(db_path) as conn:
            def bad_alter(_sql: str) -> None:
                raise sqlite3.OperationalError("table does not exist")

            with pytest.raises(sqlite3.OperationalError) as exc_info:
                _run_source_migration(conn, _alter_executor=bad_alter)
            assert "table does not exist" in str(exc_info.value).lower()

    def test_is_duplicate_source_column_error_classifier_matrix(self):
        """Direct unit test of the helper's classification matrix.

        Locks the explicit accept/reject list so future rewording or refactor
        cannot drift behavior silently.
        """
        from session import _is_duplicate_source_column_error

        # Accept (benign race wordings):
        assert _is_duplicate_source_column_error(
            sqlite3.OperationalError("duplicate column name: source")
        )
        assert _is_duplicate_source_column_error(
            sqlite3.OperationalError("DUPLICATE COLUMN: source")
        )
        assert _is_duplicate_source_column_error(
            sqlite3.OperationalError("column already exists: source")
        )
        assert _is_duplicate_source_column_error(
            sqlite3.OperationalError("column 'source' already exists")
        )

        # Reject (real failures — must NOT be swallowed):
        assert not _is_duplicate_source_column_error(
            sqlite3.OperationalError("database is locked")
        )
        assert not _is_duplicate_source_column_error(
            sqlite3.OperationalError("syntax error")
        )
        assert not _is_duplicate_source_column_error(
            sqlite3.OperationalError("no such table: chat_sessions")
        )
        assert not _is_duplicate_source_column_error(
            sqlite3.OperationalError("table does not exist")
        )

    def test_postgres_static_parity_in_create_sql(self):
        """Static parity check (R2 M1 / R1 M1) — non-skipped: read the actual
        Python source of PostgresSessionStore and assert the source column +
        index DDL strings appear, and _row_to_session reads row[20]."""
        import inspect
        import re

        from session import PostgresSessionStore

        src = inspect.getsource(PostgresSessionStore)
        # CREATE TABLE / ALTER must contain the new column DDL. The actual
        # implementation splits the literal across two adjacent string literals
        # for line-length, so collapse whitespace before substring checking.
        src_collapsed = re.sub(r"\s+", " ", src)
        assert (
            "ADD COLUMN source TEXT NOT NULL DEFAULT 'interactive'"
            in src_collapsed
        ), "Postgres migration must emit the source ALTER TABLE DDL"
        # The ALTER TABLE prefix must also be present.
        assert "ALTER TABLE chat_sessions" in src, (
            "Postgres migration must use ALTER TABLE chat_sessions"
        )
        # Composite index required for Phase 4.
        assert "idx_chat_sessions_source_updated" in src, (
            "Postgres migration must create the composite index"
        )
        # _row_to_session must reference row[20] for source.
        assert "row[20]" in src, (
            "_row_to_session must read source from positional row[20]"
        )
        # The shared shape post-check must run on Postgres path too.
        assert '_assert_source_column_shape(cur, backend="postgres")' in src, (
            "Postgres _init_db must call shared _assert_source_column_shape"
        )

    def test_postgres_row_to_session_synthetic_tuple(self):
        """Hand-craft a 21-element tuple, pass through the unbound
        _row_to_session, assert source comes from t[20]. No live DB needed.

        Static parity test (R1 M1) — the Postgres implementation is the same
        codebase, so synthesizing a tuple proves the positional contract.
        """
        import inspect

        from session import PostgresSessionStore

        # Build a 21-tuple whose layout matches the docstring in
        # _row_to_session (id, session_id, agent_session_id, runtime_session_id,
        # runtime_provider, runtime_model, runtime_profile_key, platform,
        # channel_id, thread_id, user_id, created_at, updated_at,
        # message_count, total_cost_usd, status, mode, runtime_lane,
        # tool_call_count, runtime_tool_calls_json, source).
        now = datetime.now()
        synthetic = (
            1,                       # 0  id
            "cli:chan:thread",       # 1  session_id
            "agent-x",               # 2  agent_session_id
            "rt-12345",              # 3  runtime_session_id
            "claude",                # 4  runtime_provider
            "opus",                  # 5  runtime_model
            "default",               # 6  runtime_profile_key
            "cli",                   # 7  platform
            "chan",                  # 8  channel_id
            "thread",                # 9  thread_id
            "user-x",                # 10 user_id
            now,                     # 11 created_at
            now,                     # 12 updated_at
            5,                       # 13 message_count
            0.5,                     # 14 total_cost_usd
            "active",                # 15 status
            "execute",               # 16 mode
            "claude_native",         # 17 runtime_lane
            2,                       # 18 tool_call_count
            "[]",                    # 19 runtime_tool_calls_json
            "tool",                  # 20 source ← under test
        )

        # Call the unbound method against a stub; we don't need a real
        # connection because _row_to_session does not touch self.
        result = inspect.getattr_static(
            PostgresSessionStore, "_row_to_session"
        )(None, synthetic)
        assert result.source == "tool", (
            f"row[20] must drive Session.source; got {result.source!r}"
        )

    def test_postgres_row_to_session_short_tuple_falls_back_to_interactive(self):
        """If the row is short (< 21 columns), source defaults to 'interactive'."""
        import inspect

        from session import PostgresSessionStore

        now = datetime.now()
        # 20-element tuple — no source column.
        short = (
            1, "cli:c:t", "agent", "rt", "claude", "opus", "default",
            "cli", "c", "t", "user", now, now, 0, 0.0, "active",
            "execute", "claude_native", 0, "[]",
        )
        result = inspect.getattr_static(
            PostgresSessionStore, "_row_to_session"
        )(None, short)
        assert result.source == "interactive"

    # ------------------------------------------------------------------
    # Post-build iteration 1 — F3: expand Postgres static parity coverage.
    # The previous test only inspected ``create`` partially. R2 M1 / R1 M1
    # contract requires ``create``, ``list_recent``, and ``get_by_session_id``
    # all use parameterized binding (``%s``) and reference the source column
    # consistently — so a SQL-injection via a non-Click caller is impossible
    # on the Postgres path the same way it is on the SQLite path.
    # ------------------------------------------------------------------

    def test_postgres_static_parity_create_list_recent_get_by_id_sql(self):
        """Inspect ``create``, ``list_recent``, ``get_by_session_id`` Python
        sources and assert the parity contract holds on each.

        Asserts (per method):

        - ``create``: column name ``source`` AND ``normalize_source`` reference
          AND ``%s`` placeholder bound for source value (parameterized — no
          f-string interpolation).
        - ``list_recent``: ``source = %s`` exact-filter binding AND ``%s``
          placeholder list assembly for ``source IN (...)`` AND
          ``source NOT IN (...)`` for the hidden-by-default exclusion.
        - ``get_by_session_id``: both composite-id and runtime-id lookup paths
          bind via ``%s`` (no f-string interpolation of either operand).

        This matches the runtime-checked contract enforced on SQLite by
        ``TestSqlInjectionDefense`` — the static-parity assertion is the
        Postgres equivalent without requiring a live psycopg connection.
        """
        import inspect
        import re

        from session import PostgresSessionStore

        # --- create ----------------------------------------------------
        create_src = inspect.getsource(PostgresSessionStore.create)
        create_collapsed = re.sub(r"\s+", " ", create_src)
        # source must appear in the INSERT column list.
        assert "source" in create_collapsed, (
            "Postgres create SQL must reference the source column"
        )
        # normalize_source must wrap the bound value (R1 M4 — no enum bypass).
        assert "normalize_source(session.source)" in create_collapsed, (
            "Postgres create must bind normalize_source(session.source)"
        )
        # Parameterized binding only — count %s placeholders.
        # The INSERT has 20 columns → exactly 20 %s placeholders.
        assert create_collapsed.count("%s") >= 20, (
            "Postgres create must bind every value via %s placeholder, got "
            f"{create_collapsed.count('%s')} placeholders"
        )
        # No f-string interpolation of the source value.
        assert "f\"" not in create_src and "f'" not in create_src, (
            "Postgres create SQL must not f-string-interpolate any value"
        )

        # --- list_recent -----------------------------------------------
        list_recent_src = inspect.getsource(PostgresSessionStore.list_recent)
        list_recent_collapsed = re.sub(r"\s+", " ", list_recent_src)
        # Exact-filter binding (single-source path).
        assert "source = %s" in list_recent_collapsed, (
            "Postgres list_recent must use parameterized 'source = %s' filter"
        )
        # IN-list assembly idiom: ','.join(['%s'] * len(...)) — proves the
        # placeholders are bound through psycopg, not interpolated as values.
        assert '"%s"' in list_recent_collapsed, (
            "Postgres list_recent must build IN-list placeholders via %s "
            "tokens (','.join(['%s'] * len(...))), not by f-string-injecting "
            "raw values"
        )
        # NOT IN clause for hidden filter must exist (default-hide path).
        assert "source NOT IN" in list_recent_collapsed, (
            "Postgres list_recent must implement source NOT IN (...) for "
            "hidden-by-default exclusion"
        )
        # IN clause for the explicit ``sources=...`` plural path.
        assert "source IN" in list_recent_collapsed, (
            "Postgres list_recent must implement source IN (...) for the "
            "explicit sources= plural filter"
        )
        # Parameter-extension idiom (not value-interpolation): the
        # ``params.extend(sources)`` call binds values through psycopg's
        # parameter machinery, never by SQL-text interpolation.
        assert "params.extend(sources)" in list_recent_collapsed, (
            "Postgres list_recent must extend params via psycopg binding, "
            "not interpolate values into the SQL string"
        )
        # Sanity: the LIMIT placeholder is also %s, never an f-string int.
        assert "LIMIT %s" in list_recent_collapsed, (
            "Postgres list_recent LIMIT must bind via %s, not f-string"
        )

        # --- get_by_session_id -----------------------------------------
        get_by_id_src = inspect.getsource(
            PostgresSessionStore.get_by_session_id
        )
        get_by_id_collapsed = re.sub(r"\s+", " ", get_by_id_src)
        # Both lookup arms (composite + runtime) are bound via %s.
        assert "session_id = %s" in get_by_id_collapsed, (
            "Postgres get_by_session_id must bind composite session_id via %s"
        )
        assert "runtime_session_id = %s" in get_by_id_collapsed, (
            "Postgres get_by_session_id must bind runtime_session_id via %s"
        )
        # The OR fallback shape must exist (one parameter passed twice).
        assert " OR " in get_by_id_collapsed, (
            "Postgres get_by_session_id must implement composite-OR-runtime "
            "lookup shape"
        )
        # And no f-string interpolation of the operand. f-strings appear as
        # ``f"`` or ``f'`` literal prefix.
        assert 'f"' not in get_by_id_src and "f'" not in get_by_id_src, (
            "Postgres get_by_session_id must not f-string-interpolate"
        )


# ============================================================================
# B. Helpers + dataclass (WS1)
# ============================================================================


class TestNormalizeSource:
    """normalize_source fail-OPEN matrix."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, "interactive"),
            ("", "interactive"),
            ("interactive", "interactive"),
            ("tool", "tool"),
            ("cron", "cron"),
            ("hook", "hook"),
            ("  tool  ", "tool"),  # trim
            ("bogus", "interactive"),
            (123, "interactive"),  # non-string -> interactive
            (object(), "interactive"),
        ],
    )
    def test_normalize_source_matrix(self, raw, expected):
        assert normalize_source(raw) == expected


class TestSessionDataclass:
    """Session.source default + set-once invariant."""

    def test_default_source_is_interactive(self, tmp_path):
        sess = _make_session()
        assert sess.source == "interactive"

    def test_set_once_invariant_update_does_not_overwrite_source(self, tmp_path):
        """Create a session with source='tool', mutate Session.source then
        call store.update — re-fetch must show the original value 'tool'."""
        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)

        sess = _make_session(source="tool", channel_id="c", thread_id="t")
        store.create(sess)

        # Mutate the in-memory dataclass and try to update.
        sess.source = "cron"
        sess.message_count = 5
        store.update(sess)

        refreshed = store.get("cli", "c", "t")
        assert refreshed is not None
        assert refreshed.source == "tool", (
            "update() must NOT overwrite source — set-once invariant violated"
        )
        # Sanity: update did persist other mutable fields.
        assert refreshed.message_count == 5

    def test_create_normalizes_invalid_source(self, tmp_path):
        """Non-Click writers cannot bypass the four-value enum (R1 M4).

        store.create writes normalize_source(session.source), so a Session
        constructed with source='not-a-value' lands as 'interactive' on disk.
        """
        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)

        sess = _make_session(source="bogus-injection", channel_id="c", thread_id="t")
        store.create(sess)
        refreshed = store.get("cli", "c", "t")
        assert refreshed is not None
        assert refreshed.source == "interactive"


class TestSessionSummary:
    """SessionSummary dataclass field order is the consumer contract."""

    def test_field_order(self):
        """Pinned dataclass field order — JSON consumers depend on it."""
        # Use __dataclass_fields__ which preserves declaration order.
        names = list(SessionSummary.__dataclass_fields__.keys())
        expected = [
            "internal_id",
            "session_id",
            "platform",
            "source",
            "message_count",
            "updated_at",
            "runtime_session_id",
        ]
        assert names == expected


class TestListRecent:
    """list_recent contract — None sentinel + return type."""

    def test_returns_list_of_session_summary(self, tmp_path):
        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        store.create(_make_session(source="interactive", channel_id="c1", thread_id="t1"))
        store.create(_make_session(source="tool", channel_id="c2", thread_id="t2"))

        rows = store.list_recent(all_sources=True, limit=10)
        assert len(rows) == 2
        assert all(isinstance(r, SessionSummary) for r in rows)

    def test_none_sentinel_observes_monkeypatched_default(
        self, tmp_path, monkeypatch
    ):
        """Rule 1: list_recent(hidden=None) resolves SOURCE_HIDDEN_BY_DEFAULT
        AT CALL TIME (not at def time)."""
        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        store.create(_make_session(source="tool", channel_id="c1", thread_id="t1"))
        store.create(_make_session(source="hook", channel_id="c2", thread_id="t2"))
        store.create(_make_session(source="cron", channel_id="c3", thread_id="t3"))
        store.create(_make_session(source="interactive", channel_id="c4", thread_id="t4"))

        # Monkeypatch the module-level constant to ONLY hide cron.
        import session as session_mod
        monkeypatch.setattr(session_mod, "SOURCE_HIDDEN_BY_DEFAULT", ("cron",))

        # Call without explicit hidden kwarg — must observe the patched value.
        rows = store.list_recent(limit=20)
        sources = sorted(r.source for r in rows)
        # cron hidden, others visible (tool, hook, interactive present).
        assert "cron" not in sources, (
            "list_recent must resolve SOURCE_HIDDEN_BY_DEFAULT at call time, "
            f"not capture default at def time; got {sources}"
        )
        assert {"tool", "hook", "interactive"}.issubset(set(sources))


class TestGetBySessionId:
    """get_by_session_id accepts composite OR runtime form."""

    def test_accepts_composite_session_id(self, tmp_path):
        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        composite = build_session_key("cli", "c", "t")
        sess = _make_session(
            session_id=composite,
            channel_id="c",
            thread_id="t",
            runtime_session_id="rt-abc-123",
        )
        store.create(sess)

        result = store.get_by_session_id(composite)
        assert result is not None
        assert result.session_id == composite

    def test_accepts_runtime_session_id_fallback(self, tmp_path):
        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        composite = build_session_key("cli", "c", "t")
        sess = _make_session(
            session_id=composite,
            channel_id="c",
            thread_id="t",
            runtime_session_id="rt-fallback-xyz",
        )
        store.create(sess)

        # Lookup by runtime id — must fall back via OR clause.
        result = store.get_by_session_id("rt-fallback-xyz")
        assert result is not None
        assert result.session_id == composite

    def test_nonexistent_returns_none(self, tmp_path):
        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        assert store.get_by_session_id("does-not-exist") is None


# ============================================================================
# C. CLI surface --source on `thehomie chat` (WS2)
# ============================================================================


class TestChatSourceFlag:
    """`thehomie chat --source ...` Click choice + persistence."""

    def test_chat_help_shows_source_option(self):
        from cli import main as cli_main
        runner = CliRunner()
        result = runner.invoke(cli_main, ["chat", "--help"])
        assert result.exit_code == 0
        assert "--source" in result.output

    def test_chat_source_uppercase_is_rejected_by_click(self):
        """case_sensitive=True — 'TOOL' must hit BadParameter, exit 2."""
        from cli import main as cli_main
        runner = CliRunner()
        result = runner.invoke(
            cli_main, ["chat", "-q", "x", "-Q", "--source", "TOOL"]
        )
        # Click rejects with exit code 2 + 'invalid choice' message.
        assert result.exit_code == 2
        # Click renders the error on stderr/output combined.
        combined = result.output + (result.stderr if result.stderr_bytes else "")
        assert "is not one of" in combined or "Invalid value" in combined or \
            "TOOL" in combined

    def test_chat_source_bogus_value_is_rejected_by_click(self):
        """Non-enum value → Click BadParameter."""
        from cli import main as cli_main
        runner = CliRunner()
        result = runner.invoke(
            cli_main, ["chat", "-q", "x", "-Q", "--source", "bogus"]
        )
        assert result.exit_code == 2

    def test_cli_adapter_accepts_source_kwarg_and_propagates(self, tmp_path, monkeypatch):
        """CLIAdapter.source attribute is set from the kwarg and propagates
        into IncomingMessage via listen()."""
        import asyncio

        from adapters.cli_adapter import CLIAdapter

        # Point the adapter at a temp DB so list_active() doesn't hit prod.
        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)
        _patch_chat_db_path(monkeypatch, db_path)

        adapter = CLIAdapter(query="hello", source="tool", quiet=True)
        assert adapter.source == "tool"

        async def collect():
            messages = []
            async for m in adapter.listen():
                messages.append(m)
            return messages

        messages = asyncio.run(collect())
        assert len(messages) == 1
        assert messages[0].source == "tool"

    def test_cli_adapter_default_source_is_interactive(self, tmp_path, monkeypatch):
        """Adapter without source kwarg defaults to 'interactive'."""
        import asyncio

        from adapters.cli_adapter import CLIAdapter

        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)
        _patch_chat_db_path(monkeypatch, db_path)

        adapter = CLIAdapter(query="hello", quiet=True)
        assert adapter.source == "interactive"

        async def collect():
            messages = []
            async for m in adapter.listen():
                messages.append(m)
            return messages

        messages = asyncio.run(collect())
        assert len(messages) == 1
        assert messages[0].source == "interactive"

    # ------------------------------------------------------------------
    # Post-build iteration 1 — F3: full CLI plumbing persistence test.
    # Existing tests in this class stop at the IncomingMessage boundary.
    # This one drives the full chain Click → CLIAdapter → IncomingMessage
    # → engine → Session.source → DB row, which is the contract WS5 must
    # prove (R1 M4 + R2 M2). A spy engine writes ``Session(... source=
    # incoming.source)`` so the persisted row is the real assertion.
    # ------------------------------------------------------------------

    def test_chat_source_tool_persists_to_db_row(self, tmp_path, monkeypatch):
        """End-to-end: ``thehomie chat --source tool -q "hello" -Q`` must
        result in a DB row with ``source = "tool"``.

        Strategy: monkeypatch ``ChatRouter._handle`` to a spy that writes
        the session via ``store.create(Session(... source=incoming.source))``
        — the same shape production routers/engines use. Then verify the row.

        We patch the router (not the engine) because the engine instance is
        already wired into the router by the time we'd need to swap it; the
        router's ``_handle`` is the single async dispatch point that receives
        the IncomingMessage with the operator's ``--source`` value attached.
        """
        from cli import main as cli_main
        import cli as cli_mod

        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)
        _patch_chat_db_path(monkeypatch, db_path)
        # cli.py does ``from config import CHAT_DB_PATH`` at module-import
        # time and ``store = get_session_store(CHAT_DB_PATH)`` re-uses that
        # cached value — so we must also patch ``cli.CHAT_DB_PATH`` directly.
        monkeypatch.setattr(cli_mod, "CHAT_DB_PATH", db_path)

        import router as router_mod

        async def spy_handle(self, adapter, incoming, *_a, **_kw):
            """Persist source through the production write shape."""
            channel_id = incoming.channel.platform_id
            thread_id = (
                incoming.thread.thread_id
                if incoming.thread is not None
                else channel_id
            )
            composite = build_session_key(
                incoming.platform.value, channel_id, thread_id
            )
            now = datetime.now()
            self.engine.session_store.create(Session(
                session_id=composite,
                agent_session_id=composite,
                platform=incoming.platform.value,
                channel_id=channel_id,
                thread_id=thread_id,
                user_id=incoming.user.platform_id,
                created_at=now,
                updated_at=now,
                source=incoming.source,
            ))

        monkeypatch.setattr(router_mod.ChatRouter, "_handle", spy_handle)

        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["chat", "--source", "tool", "-q", "hello", "-Q"],
        )
        assert result.exit_code == 0, (
            f"chat exit non-zero: {result.exit_code}; output={result.output!r}"
        )

        # Read the persisted row directly.
        store = SQLiteSessionStore(db_path)
        sessions = store.list_active()
        assert len(sessions) == 1, (
            f"expected 1 persisted session, got {len(sessions)}: {sessions}"
        )
        assert sessions[0].source == "tool", (
            f"end-to-end --source tool must persist source='tool', "
            f"got {sessions[0].source!r}"
        )


# ============================================================================
# D. Source plumbing — non-engine writer paths (WS2)
# ============================================================================


class TestSourcePlumbingNonEngineWriters:
    """source flows from incoming → store.create on every writer path."""

    def test_router_persist_turn_uses_incoming_source(self, tmp_path, monkeypatch):
        """ChatRouter._persist_router_turn reads incoming.source on create."""
        import asyncio

        from router import ChatRouter
        from models import Channel, IncomingMessage, Platform, User

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)

        class FakeEngine:
            def __init__(self, store):
                self.session_store = store

        class FakeManager:
            def detect_intents(self, _):
                return []
            commands = {}

        router = ChatRouter(FakeEngine(store), FakeManager())

        user = User(Platform.CLI, "u1", "user")
        channel = Channel(Platform.CLI, "chan-rt-1", is_dm=True)
        incoming = IncomingMessage(
            text="hi",
            user=user,
            channel=channel,
            platform=Platform.CLI,
            source="tool",
        )
        # Direct call to the writer.
        router._persist_router_turn(incoming, "reply")

        # Look up by composite session_id.
        platform_str = "cli"
        sess = store.get(platform_str, "chan-rt-1", "chan-rt-1")
        assert sess is not None
        assert sess.source == "tool"

    def test_handle_plan_uses_incoming_source(self, tmp_path, monkeypatch):
        """core_handlers.handle_plan reads incoming.source on create."""
        import asyncio

        import core_handlers
        from models import Channel, IncomingMessage, Platform, User

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)

        class FakeEngine:
            def __init__(self, s):
                self.session_store = s

        # Patch _get_session to use the temp store. handle_plan calls it.
        core_handlers.set_context(
            engine=FakeEngine(store),
            adapters={},
            bot_start_time=datetime.now(),
        )

        user = User(Platform.CLI, "u1", "user")
        channel = Channel(Platform.CLI, "chan-plan-1", is_dm=True)
        incoming = IncomingMessage(
            text="/plan",
            user=user,
            channel=channel,
            platform=Platform.CLI,
            source="tool",
        )

        asyncio.run(core_handlers.handle_plan(None, incoming, ""))

        sess = store.get("cli", "chan-plan-1", "chan-plan-1")
        assert sess is not None
        assert sess.source == "tool"
        assert sess.mode == "plan"

    def _make_relay_ws_client(self):
        """Construct a RelayWSClient with stubbed router + adapter."""
        from ws_client import RelayWSClient

        class StubEngine:
            session_store = None

        class StubRouter:
            engine = StubEngine()
            adapters: dict = {}

        class StubAdapter:
            pass

        return RelayWSClient(
            relay_url="ws://stub",
            relay_token="token",
            router=StubRouter(),
            adapter=StubAdapter(),
        )

    def test_ws_client_propagates_source_from_frame(self):
        """ws_client._build_incoming pulls source from the relay frame."""
        client = self._make_relay_ws_client()

        frame = {
            "request_id": "req-1",
            "session_key": "web:user-1:thread-1",
            "message": "hi",
            "user": {"user_id": "user-1", "email": "a@b.c", "role": "admin"},
            "agent_type": "thehomie",
            "source": "tool",
        }
        _request_id, incoming = client._build_incoming(frame)
        assert incoming.source == "tool"

    def test_ws_client_default_source_when_frame_lacks_key(self):
        """Frame WITHOUT 'source' key → IncomingMessage.source == 'interactive'."""
        client = self._make_relay_ws_client()

        frame = {
            "request_id": "req-2",
            "session_key": "web:user-2:thread-2",
            "message": "hi",
            "user": {"user_id": "user-2", "email": "a@b.c", "role": "admin"},
            "agent_type": "thehomie",
        }
        _request_id, incoming = client._build_incoming(frame)
        assert incoming.source == "interactive"

    def test_blog_scheduler_synthetic_message_tagged_cron(self):
        """Blog scheduler builds IncomingMessage(source='cron') for a synthetic engine call.

        R2 NB1 — capture the IncomingMessage handed to engine.handle_message.
        ``extensions/`` lives at ``.claude/extensions/`` so we add the
        ``.claude/`` parent to sys.path before importing.
        """
        import asyncio

        # Add `.claude/` to sys.path so `extensions.blog` resolves. The
        # extension lives at `.claude/extensions/blog/scheduler.py`.
        claude_dir = _SCRIPTS_DIR.parent
        if str(claude_dir) not in sys.path:
            sys.path.insert(0, str(claude_dir))

        # Import lazily — extension may pull heavy deps.
        from extensions.blog import scheduler as scheduler_mod

        captured: dict = {}

        class CaptureEngine:
            async def handle_message(self, incoming, progress=None):
                captured["incoming"] = incoming
                # Yield no outgoings — minimal stub.
                if False:
                    yield None

        sched = scheduler_mod.BlogAutoScheduler(
            engine=CaptureEngine(),
            discord_adapter=None,
        )

        asyncio.run(sched._generate_one("test-topic"))

        assert "incoming" in captured, "scheduler did not call engine"
        assert captured["incoming"].source == "cron", (
            "blog scheduler must tag synthetic message source='cron' (R2 NB1)"
        )

    # ------------------------------------------------------------------
    # Post-build iteration 1 — F3: end-to-end persistence proof for the
    # blog scheduler path. The earlier ``_tagged_cron`` test stops at the
    # IncomingMessage → engine boundary; this one drives the full chain
    # all the way to a persisted DB row so the contract scheduler →
    # IncomingMessage → engine → store.create → DB is verified end-to-end.
    # ------------------------------------------------------------------

    def test_blog_scheduler_persisted_session_has_source_cron(self, tmp_path):
        """Scheduler-to-store: IncomingMessage from scheduler must persist
        as ``Session(source='cron')`` when an engine actually writes it.

        Builds the same scheduler IncomingMessage the production path uses,
        hands it to a spy engine that calls ``store.create(Session(...
        source=incoming.source))`` with a real ``SQLiteSessionStore`` against
        a tmp DB, then reads the row back and asserts ``source == "cron"``.
        Proves the full path the production scheduler relies on without
        spinning up the real engine.
        """
        import asyncio

        claude_dir = _SCRIPTS_DIR.parent
        if str(claude_dir) not in sys.path:
            sys.path.insert(0, str(claude_dir))
        from extensions.blog import scheduler as scheduler_mod

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)

        class PersistingEngine:
            """Spy engine: persist source through the production write
            shape (``store.create(Session(... source=incoming.source))``)."""

            def __init__(self, s):
                self.session_store = s

            async def handle_message(self, incoming, progress=None):
                # Mirror the production write shape — Channel uses
                # ``platform_id`` (not ``id``); thread is optional, fall
                # back to channel.platform_id when missing.
                channel_id = incoming.channel.platform_id
                thread_id = (
                    incoming.thread.thread_id
                    if incoming.thread is not None
                    else channel_id
                )
                composite = build_session_key(
                    incoming.platform.value, channel_id, thread_id
                )
                now = datetime.now()
                sess = Session(
                    session_id=composite,
                    agent_session_id=composite,
                    platform=incoming.platform.value,
                    channel_id=channel_id,
                    thread_id=thread_id,
                    user_id=incoming.user.platform_id,
                    created_at=now,
                    updated_at=now,
                    source=incoming.source,
                )
                self.session_store.create(sess)
                if False:
                    yield None  # pragma: no cover — generator marker

        sched = scheduler_mod.BlogAutoScheduler(
            engine=PersistingEngine(store),
            discord_adapter=None,
        )
        asyncio.run(sched._generate_one("test-topic-persist"))

        # Read row back — source must be 'cron' on disk.
        sessions = store.list_active()
        assert len(sessions) == 1, (
            f"expected 1 persisted session, got {len(sessions)}"
        )
        assert sessions[0].source == "cron", (
            f"persisted source must be 'cron', got {sessions[0].source!r}"
        )


# ============================================================================
# E. `thehomie session` group — list / show / resume (WS3)
# ============================================================================


def _seed_four_source_sessions(store: SQLiteSessionStore) -> None:
    """Seed one session per SOURCE_VALUES tag for filter tests."""
    for src in SOURCE_VALUES:
        store.create(_make_session(
            source=src,
            channel_id=f"c-{src}",
            thread_id=f"t-{src}",
        ))


class TestSessionList:
    """`thehomie session list` filters + JSON shape."""

    def test_list_default_hides_tool_and_hook(self, tmp_path, monkeypatch):
        """PRD §7.10 default — tool + hook hidden, interactive + cron visible."""
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        _seed_four_source_sessions(store)
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(session_group, ["list", "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        sources = sorted(r["source"] for r in rows)
        # tool + hook hidden by default; interactive + cron visible.
        assert sources == ["cron", "interactive"], (
            f"default must hide tool+hook only; got {sources}"
        )

    def test_list_all_shows_everything(self, tmp_path, monkeypatch):
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        _seed_four_source_sessions(store)
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(session_group, ["list", "--all", "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        sources = sorted(r["source"] for r in rows)
        assert sources == sorted(SOURCE_VALUES)

    def test_list_source_filter_narrows_to_exact(self, tmp_path, monkeypatch):
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        _seed_four_source_sessions(store)
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(
            session_group, ["list", "--source", "tool", "--json"]
        )
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert len(rows) == 1
        assert rows[0]["source"] == "tool"

    def test_list_source_filter_rejects_uppercase(self, tmp_path, monkeypatch):
        """case_sensitive=True on session list --source too."""
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(
            session_group, ["list", "--source", "TOOL"]
        )
        assert result.exit_code == 2

    def test_list_json_field_order_matches_session_summary(
        self, tmp_path, monkeypatch
    ):
        """JSON keys must match SessionSummary dataclass order verbatim."""
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        store.create(_make_session(source="interactive", channel_id="c1", thread_id="t1"))
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(session_group, ["list", "--json"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert len(rows) == 1
        keys = list(rows[0].keys())
        assert keys == [
            "internal_id",
            "session_id",
            "platform",
            "source",
            "message_count",
            "updated_at",
            "runtime_session_id",
        ]

    def test_list_json_multi_row_preserves_field_order(
        self, tmp_path, monkeypatch
    ):
        """All rows have the same key order."""
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        store.create(_make_session(source="interactive", channel_id="c1", thread_id="t1"))
        store.create(_make_session(source="cron", channel_id="c2", thread_id="t2"))
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(session_group, ["list", "--json", "--all"])
        assert result.exit_code == 0, result.output
        rows = json.loads(result.output)
        assert len(rows) >= 2
        first_keys = list(rows[0].keys())
        for r in rows:
            assert list(r.keys()) == first_keys


class TestSessionShow:
    """`thehomie session show` accepts string composite + runtime forms."""

    def test_show_accepts_composite_session_id(self, tmp_path, monkeypatch):
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        composite = build_session_key("cli", "c-show", "t-show")
        store.create(_make_session(
            session_id=composite,
            channel_id="c-show",
            thread_id="t-show",
            source="interactive",
            runtime_session_id="rt-show-1",
        ))
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(session_group, ["show", composite, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["session_id"] == composite
        assert payload["source"] == "interactive"

    def test_show_accepts_runtime_session_id(self, tmp_path, monkeypatch):
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        composite = build_session_key("cli", "c-rt", "t-rt")
        store.create(_make_session(
            session_id=composite,
            channel_id="c-rt",
            thread_id="t-rt",
            runtime_session_id="rt-fallback-xyz",
        ))
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(
            session_group, ["show", "rt-fallback-xyz", "--json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["session_id"] == composite

    def test_show_nonexistent_exits_nonzero(self, tmp_path, monkeypatch):
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(session_group, ["show", "nope-not-real"])
        assert result.exit_code != 0


class TestSessionResume:
    """`thehomie session resume --dry-run` JSON envelope shape."""

    def test_resume_dry_run_emits_json_envelope(self, tmp_path, monkeypatch):
        """{'resume_argv': [...], 'target': '<runtime_session_id>'}.

        R2 minor — JSON envelope (not a bare command string) so synthetic /
        malicious session ids cannot masquerade as flags in display.
        """
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        composite = build_session_key("cli", "c-rs", "t-rs")
        store.create(_make_session(
            session_id=composite,
            channel_id="c-rs",
            thread_id="t-rs",
            runtime_session_id="rt-12345",
        ))
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(
            session_group, ["resume", composite, "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert set(payload.keys()) == {"resume_argv", "target"}
        assert payload["target"] == "rt-12345"
        assert isinstance(payload["resume_argv"], list)
        assert payload["resume_argv"][-1] == "rt-12345"
        # The argv must be the canonical resume shape.
        assert "--resume" in payload["resume_argv"]

    def test_resume_dry_run_target_falls_back_to_session_id_when_runtime_empty(
        self, tmp_path, monkeypatch
    ):
        """If runtime_session_id is empty, use composite session_id as target."""
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        composite = build_session_key("cli", "c-fb", "t-fb")
        # _make_session sets runtime_session_id = session_id when not given,
        # so to get an empty runtime_session_id we have to write the row
        # directly. Use raw SQL.
        SQLiteSessionStore(db_path)  # ensure schema
        with sqlite3.connect(db_path) as conn:
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT INTO chat_sessions ("
                "  session_id, agent_session_id, runtime_session_id, "
                "  platform, channel_id, thread_id, user_id, "
                "  created_at, updated_at, source"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (composite, "", "", "cli", "c-fb", "t-fb", "u",
                 now, now, "interactive"),
            )
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(
            session_group, ["resume", composite, "--dry-run"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        # Falls back to composite session_id when runtime is empty.
        assert payload["target"] == composite


class TestSessionGroupRegistration:
    """Module-level registration — `python -m chat.cli session --help` works."""

    def test_session_help_succeeds_via_click_runner(self):
        from cli import main as cli_main
        runner = CliRunner()
        result = runner.invoke(cli_main, ["session", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "show" in result.output
        assert "resume" in result.output

    def test_session_list_help_succeeds_via_click_runner(self):
        from cli import main as cli_main
        runner = CliRunner()
        result = runner.invoke(cli_main, ["session", "list", "--help"])
        assert result.exit_code == 0
        assert "--all" in result.output
        assert "--source" in result.output


# ============================================================================
# F. Quiet JSON envelope (WS4)
# ============================================================================


class TestQuietJsonEnvelope:
    """11-field success / 12-field error envelope locked-order contract."""

    def test_format_final_output_success_path_eleven_fields_locked_order(self):
        """11 always-present fields in the locked order, no error key."""
        from adapters.cli_adapter import CLIAdapter

        adapter = CLIAdapter(query="x", quiet=True, source="interactive")
        output = adapter.format_final_output(
            "sess-1",
            {
                "lane": "claude_native",
                "provider": "claude",
                "model": "opus",
                "cost_usd": 0.05,
                "tool_calls": 2,
                "source": "interactive",
            },
        )
        payload = json.loads(output)
        assert list(payload.keys()) == [
            "success",
            "response",
            "session_id",
            "lane",
            "provider",
            "model",
            "cost_usd",
            "tool_calls",
            "execution_time_ms",
            "profile",
            "source",
        ]
        assert "error" not in payload
        assert payload["success"] is True

    @pytest.mark.asyncio
    async def test_format_final_output_adapter_error_path_twelve_fields(self):
        """has_error=True (set via send(is_error=True)) → 11 + error last."""
        from adapters.cli_adapter import CLIAdapter
        from models import Channel, OutgoingMessage, Platform

        adapter = CLIAdapter(query="x", quiet=True, source="tool")
        channel = Channel(Platform.CLI, "cli-test", is_dm=True)
        await adapter.send(
            OutgoingMessage(
                text="boom",
                channel=channel,
                is_error=True,
            )
        )

        output = adapter.format_final_output("sess-err", {"source": "tool"})
        payload = json.loads(output)
        assert list(payload.keys()) == [
            "success",
            "response",
            "session_id",
            "lane",
            "provider",
            "model",
            "cost_usd",
            "tool_calls",
            "execution_time_ms",
            "profile",
            "source",
            "error",
        ]
        assert payload["success"] is False
        assert payload["error"] == "boom"

    def test_build_quiet_error_envelope_twelve_fields_locked_order(self):
        """build_quiet_error_envelope emits the 12-field locked-order JSON."""
        from adapters.cli_adapter import build_quiet_error_envelope

        envelope = build_quiet_error_envelope(RuntimeError("forced for test"))
        payload = json.loads(envelope)
        assert list(payload.keys()) == [
            "success",
            "response",
            "session_id",
            "lane",
            "provider",
            "model",
            "cost_usd",
            "tool_calls",
            "execution_time_ms",
            "profile",
            "source",
            "error",
        ]

    def test_build_quiet_error_envelope_default_values(self):
        """Default values are JSON-clean: success=False, 0/0.0/empty strings."""
        from adapters.cli_adapter import build_quiet_error_envelope

        envelope = build_quiet_error_envelope(ValueError("oops"))
        payload = json.loads(envelope)
        assert payload["success"] is False
        assert payload["response"] == ""
        assert payload["session_id"] == ""
        assert payload["lane"] == ""
        assert payload["provider"] == ""
        assert payload["model"] == ""
        assert payload["cost_usd"] == 0.0
        assert payload["tool_calls"] == 0
        assert payload["execution_time_ms"] == 0
        assert payload["source"] == "interactive"
        assert "oops" in payload["error"]

    def test_resolver_failure_returns_unknown_in_envelope(self, monkeypatch):
        """R3 NNM1: when personas.get_active_profile_name raises, profile == 'unknown'.

        NOT 'default' — that would misattribute the failure to the actual
        default profile. cli_adapter._resolve_active_profile_name imports
        personas INSIDE the function body, so monkeypatching personas-level
        attributes propagates.
        """
        import personas
        import personas.activity as personas_activity

        def boom():
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(personas, "get_active_profile_name", boom)
        monkeypatch.setattr(
            personas_activity, "get_active_profile_name", boom
        )

        from adapters.cli_adapter import build_quiet_error_envelope

        envelope = build_quiet_error_envelope(RuntimeError("e"))
        payload = json.loads(envelope)
        assert payload["profile"] == "unknown", (
            f"resolver failure must map to 'unknown' (R3 NNM1); "
            f"got {payload['profile']!r}"
        )

    def test_resolver_failure_returns_unknown_in_format_final_output_success(
        self, monkeypatch
    ):
        """Mirror assertion on the success path of format_final_output."""
        import personas
        import personas.activity as personas_activity
        from adapters.cli_adapter import CLIAdapter

        def boom():
            raise RuntimeError("resolver exploded")

        monkeypatch.setattr(personas, "get_active_profile_name", boom)
        monkeypatch.setattr(
            personas_activity, "get_active_profile_name", boom
        )

        adapter = CLIAdapter(query="x", quiet=True, source="interactive")
        output = adapter.format_final_output("sess-ok", {})
        payload = json.loads(output)
        assert payload["profile"] == "unknown"

    def test_envelope_source_field_defaults_to_interactive(self):
        """When result dict has no source, envelope falls back to adapter.source."""
        from adapters.cli_adapter import CLIAdapter

        adapter = CLIAdapter(query="x", quiet=True, source="cron")
        output = adapter.format_final_output("sess", {})
        payload = json.loads(output)
        # adapter.source is the per-instance fallback.
        assert payload["source"] in ("cron", "interactive")
        # Specifically: result has no source key, so fall back to self.source.
        assert payload["source"] == "cron"

    # ------------------------------------------------------------------
    # Post-build iteration 1 — F1: build_quiet_error_envelope must accept
    # a ``source`` kwarg and echo the operator's --source value (run
    # through normalize_source). Previously it always emitted
    # "interactive" regardless of the parsed flag, so a Paperclip-style
    # ``thehomie chat --source tool -q "x" -Q`` that failed during engine
    # setup was misclassified.
    # ------------------------------------------------------------------

    def test_build_quiet_error_envelope_accepts_source_kwarg(self):
        """Direct call: ``build_quiet_error_envelope(exc, source='tool')``
        must echo ``"tool"`` — NOT downgrade to ``"interactive"``."""
        from adapters.cli_adapter import build_quiet_error_envelope

        envelope = build_quiet_error_envelope(
            RuntimeError("forced for test"), source="tool"
        )
        payload = json.loads(envelope)
        assert payload["source"] == "tool", (
            "build_quiet_error_envelope must echo the explicit source kwarg, "
            f"got {payload['source']!r}"
        )

    def test_build_quiet_error_envelope_default_source_still_interactive(self):
        """Without ``source=`` kwarg the default stays ``"interactive"`` so
        existing callers (none in production after F1, but the sub-protocol
        contract for direct callers) keep their behavior."""
        from adapters.cli_adapter import build_quiet_error_envelope

        envelope = build_quiet_error_envelope(RuntimeError("oops"))
        payload = json.loads(envelope)
        assert payload["source"] == "interactive"

    @pytest.mark.parametrize(
        "raw_source,expected",
        [
            ("interactive", "interactive"),
            ("tool", "tool"),
            ("cron", "cron"),
            ("hook", "hook"),
            ("bogus", "interactive"),  # fail-OPEN via normalize_source
            ("TOOL", "interactive"),   # case-sensitive — uppercase rejected
            ("", "interactive"),
        ],
    )
    def test_build_quiet_error_envelope_normalizes_source_kwarg(
        self, raw_source, expected
    ):
        """``source`` is run through ``normalize_source`` so a bogus value
        from a non-Click caller cannot pollute the envelope (R1 M4 mirror)."""
        from adapters.cli_adapter import build_quiet_error_envelope

        envelope = build_quiet_error_envelope(
            RuntimeError("e"), source=raw_source
        )
        payload = json.loads(envelope)
        assert payload["source"] == expected, (
            f"normalize_source({raw_source!r}) should map to {expected!r}, "
            f"envelope got {payload['source']!r}"
        )

    def test_chat_post_parse_exception_preserves_source_tool(
        self, tmp_path, monkeypatch
    ):
        """Force a post-parse exception INSIDE the wrapped ``_run()`` block
        and assert ``payload["source"] == "tool"`` when invoked with
        ``--source tool``.

        This is the integration test that proves the F1 fix end-to-end:
        click parses ``--source tool``, the chat handler raises during
        the wrapped run, the exception path emits the 12-field envelope
        with the operator's source echoed (NOT silently downgraded).

        We monkeypatch ``ChatRouter._handle`` to raise — that's inside
        the ``try: asyncio.run(_run())`` block where the
        ``build_quiet_error_envelope`` call lives.
        """
        from cli import main as cli_main

        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)
        _patch_chat_db_path(monkeypatch, db_path)

        # Force the dispatch step inside _run() to raise — this is the
        # closest analog to a real engine/runtime failure during chat
        # handling. Click has parsed --source by now and the value is
        # in scope in chat() (so the exception handler can pass it to
        # build_quiet_error_envelope).
        import router as router_mod

        async def boom(self, *_a, **_kw):
            raise RuntimeError("forced dispatch failure")

        monkeypatch.setattr(router_mod.ChatRouter, "_handle", boom)

        runner = CliRunner()
        result = runner.invoke(
            cli_main,
            ["chat", "--source", "tool", "-q", "hello", "-Q"],
        )
        # Quiet-mode error path exits with code 1, prints JSON envelope.
        assert result.exit_code == 1, (
            f"expected exit 1 from quiet-mode error path, got "
            f"{result.exit_code}; output={result.output!r}"
        )
        # Parse the JSON envelope from stdout.
        lines = [
            ln for ln in result.output.splitlines() if ln.strip().startswith("{")
        ]
        assert lines, (
            f"no JSON line in output: {result.output!r}"
        )
        payload = json.loads(lines[-1])
        assert payload["success"] is False
        assert payload["source"] == "tool", (
            f"F1 fix: --source tool must round-trip to envelope source; "
            f"got {payload['source']!r}"
        )
        assert "forced dispatch failure" in payload["error"]

    def test_chat_post_parse_exception_default_source_is_interactive(
        self, tmp_path, monkeypatch
    ):
        """Paired baseline: WITHOUT --source flag, envelope source defaults
        to ``"interactive"`` (Click's default value). Proves the F1 fix
        didn't regress the default path."""
        from cli import main as cli_main

        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)
        _patch_chat_db_path(monkeypatch, db_path)

        import router as router_mod

        async def boom(self, *_a, **_kw):
            raise RuntimeError("forced dispatch failure")

        monkeypatch.setattr(router_mod.ChatRouter, "_handle", boom)

        runner = CliRunner()
        result = runner.invoke(
            cli_main, ["chat", "-q", "hello", "-Q"]
        )
        assert result.exit_code == 1, (
            f"expected exit 1 quiet-mode error, got {result.exit_code}; "
            f"output={result.output!r}"
        )
        lines = [
            ln for ln in result.output.splitlines() if ln.strip().startswith("{")
        ]
        assert lines, f"no JSON line: {result.output!r}"
        payload = json.loads(lines[-1])
        assert payload["source"] == "interactive"


# ============================================================================
# G. SQL-injection defense (R1 M3)
# ============================================================================


class TestSqlInjectionDefense:
    """Click rejection of bogus values + parameterized SQL at the store."""

    def test_session_list_source_filter_rejects_sql_injection_attempt(
        self, tmp_path, monkeypatch
    ):
        """Click's Choice(SOURCE_VALUES) rejects any non-enum value, including
        SQL-injection payloads, before they reach the store."""
        from cli_session import session as session_group

        db_path = tmp_path / "chat.db"
        SQLiteSessionStore(db_path)
        _patch_chat_db_path(monkeypatch, db_path)

        runner = CliRunner()
        result = runner.invoke(
            session_group,
            ["list", "--source", "tool';DROP TABLE chat_sessions;--"],
        )
        # Click rejects with exit 2 (BadParameter).
        assert result.exit_code == 2
        # And the table still exists.
        with sqlite3.connect(db_path) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "chat_sessions" in tables

    def test_list_recent_with_injection_payload_returns_empty_no_corruption(
        self, tmp_path
    ):
        """Store-level: list_recent(source=...) is parameterized — even if a
        non-Click caller passes a malicious string, SQLite binds it as a
        literal value (so the WHERE clause sees no matching rows) and the
        schema is unchanged."""
        db_path = tmp_path / "chat.db"
        store = SQLiteSessionStore(db_path)
        store.create(_make_session(
            source="interactive", channel_id="c", thread_id="t"
        ))

        # Direct call bypassing Click — store should NOT crash and should
        # return zero rows (no source value matches the literal payload).
        rows = store.list_recent(source="tool';DROP TABLE chat_sessions;--")
        assert rows == []

        # Schema must be intact.
        with sqlite3.connect(db_path) as conn:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "chat_sessions" in tables


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
