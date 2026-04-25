"""Regression tests for #12: Postgres bulk_clear() must migrate the vector
column when EMBEDDING_DIMENSIONS changes.

Without a live Postgres + pgvector test container, these are mock-based
behavior tests asserting the SQL sequence. They guard against the original
bug where bulk_clear() only DELETEd rows but left vector(512) in place,
causing 768-d inserts to fail post-swap.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _executed_sql(mock_cursor) -> list[str]:
    return [call.args[0] for call in mock_cursor.execute.call_args_list]


def _make_db():
    from db import PostgresMemoryDB
    db = PostgresMemoryDB("postgresql://fake/fake")
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    mock_conn.closed = False
    db._conn = mock_conn
    return db, mock_conn, mock_cursor


def test_postgres_bulk_clear_drops_and_recreates_embedding_column():
    from db import EMBEDDING_DIMENSIONS
    db, mock_conn, mock_cursor = _make_db()
    with patch.object(db, "_register_vector"):
        db.bulk_clear()
    sql_log = _executed_sql(mock_cursor)
    assert any("DROP INDEX IF EXISTS idx_chunks_embedding" in s for s in sql_log)
    assert any("ALTER TABLE chunks DROP COLUMN IF EXISTS embedding" in s for s in sql_log)
    assert any(f"ADD COLUMN embedding vector({EMBEDDING_DIMENSIONS})" in s for s in sql_log)
    assert any("CREATE INDEX idx_chunks_embedding" in s and "hnsw" in s for s in sql_log)
    assert any("DELETE FROM chunks" in s for s in sql_log)
    assert any("DELETE FROM files" in s for s in sql_log)
    mock_conn.commit.assert_called_once()


def test_postgres_bulk_clear_orders_index_drop_before_column_drop():
    """DROP INDEX must come before DROP COLUMN -- HNSW index references the column."""
    db, _, mock_cursor = _make_db()
    with patch.object(db, "_register_vector"):
        db.bulk_clear()
    sql_log = _executed_sql(mock_cursor)
    drop_index_idx = next(i for i, s in enumerate(sql_log)
                          if "DROP INDEX" in s and "idx_chunks_embedding" in s)
    drop_column_idx = next(i for i, s in enumerate(sql_log)
                           if "DROP COLUMN" in s and "embedding" in s)
    assert drop_index_idx < drop_column_idx


def test_postgres_bulk_clear_orders_column_recreate_before_index_recreate():
    """ADD COLUMN must come before CREATE INDEX on that column."""
    db, _, mock_cursor = _make_db()
    with patch.object(db, "_register_vector"):
        db.bulk_clear()
    sql_log = _executed_sql(mock_cursor)
    add_column_idx = next(i for i, s in enumerate(sql_log) if "ADD COLUMN embedding" in s)
    create_index_idx = next(i for i, s in enumerate(sql_log)
                            if "CREATE INDEX idx_chunks_embedding" in s)
    assert add_column_idx < create_index_idx


def test_postgres_bulk_clear_reregisters_pgvector_types():
    """After ALTER TABLE recreates the column, pgvector's OID cache must refresh."""
    db, _, _ = _make_db()
    with patch.object(db, "_register_vector") as mock_register:
        db.bulk_clear()
    mock_register.assert_called_once()
