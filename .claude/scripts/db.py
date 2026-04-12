"""
Database abstraction layer for The Homie memory search.

Provides a unified interface (MemoryDB) with two backends:
- SQLiteMemoryDB: Local SQLite + sqlite-vec + FTS5 (default)
- PostgresMemoryDB: PostgreSQL + pgvector (VPS deployment)

Factory function `get_memory_db()` picks the backend based on DATABASE_URL.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Protocol

import numpy as np
from numpy.typing import NDArray

from config import DATABASE_PATH, DATABASE_URL, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL


class MemoryDB(Protocol):
    """Domain-specific interface hiding SQL dialect differences."""

    def init_schema(self) -> None: ...
    def close(self) -> None: ...
    def upsert_meta(self, key: str, value: str) -> None: ...
    def get_meta(self, key: str) -> str | None: ...
    def upsert_file(self, path: str, content_hash: str, mtime_ns: int, size_bytes: int, epoch: int) -> None: ...
    def get_file_hash(self, path: str) -> str | None: ...
    def get_all_file_paths(self) -> list[str]: ...
    def delete_file(self, path: str) -> None: ...
    def get_chunk_ids_for_file(self, path: str) -> list[int]: ...
    def delete_chunks_for_file(self, path: str) -> None: ...
    def delete_vectors_for_chunk_ids(self, ids: list[int]) -> None: ...
    def insert_chunk(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        section_title: str,
        content: str,
        content_hash: str,
        created_at_epoch: int,
    ) -> int: ...
    def insert_vector(self, chunk_id: int, embedding: NDArray[np.float32]) -> None: ...
    def bulk_clear(self) -> None: ...
    def keyword_search(self, query: str, limit: int, path_prefix: str = "") -> list[dict[str, Any]]: ...
    def vector_search(self, embedding: NDArray[np.float32], limit: int, path_prefix: str = "") -> list[dict[str, Any]]: ...
    def get_stats(self) -> dict[str, Any]: ...
    def commit(self) -> None: ...


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

def _embedding_to_bytes(embedding: NDArray[np.float32]) -> bytes:
    """Serialize numpy embedding to raw bytes for sqlite-vec."""
    return embedding.tobytes()


def _quote_fts_query(query: str) -> str:
    """Quote each term for FTS5 AND search."""
    terms = query.strip().split()
    if not terms:
        return query
    quoted = [f'"{term}"' for term in terms]
    return " AND ".join(quoted)


class SQLiteMemoryDB:
    """SQLite + sqlite-vec + FTS5 backend."""

    def __init__(self, db_path: str | None = None) -> None:
        from pathlib import Path

        self._db_path = Path(db_path) if db_path else DATABASE_PATH
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            import sqlite_vec

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            # 30s lock wait — plenty for concurrent heartbeat/chat bot/recall
            # readers during a reindex. Default would be 5s which is not enough
            # when another process holds a rollback-journal exclusive lock.
            self._conn = sqlite3.connect(str(self._db_path), timeout=30.0)
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self._conn.row_factory = sqlite3.Row
            # WAL mode allows concurrent readers + single writer (no mutual
            # exclusion between them). journal_mode persists in the DB header
            # once set — safe to re-assert on every connection. synchronous
            # NORMAL is the sweet spot with WAL (no durability regression vs
            # rollback journal, ~2-3x faster writes).
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("PRAGMA busy_timeout = 30000")
        return self._conn

    def init_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                indexed_at_epoch INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                section_title TEXT DEFAULT '',
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at_epoch INTEGER NOT NULL,
                FOREIGN KEY (file_path) REFERENCES files(path)
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_file_path ON chunks(file_path);
        """)
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                content,
                section_title,
                file_path UNINDEXED,
                content='chunks',
                content_rowid='id'
            );
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, content, section_title, file_path)
                VALUES (new.id, new.content, new.section_title, new.file_path);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, content, section_title, file_path)
                VALUES ('delete', old.id, old.content, old.section_title, old.file_path);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, content, section_title, file_path)
                VALUES ('delete', old.id, old.content, old.section_title, old.file_path);
                INSERT INTO chunks_fts(rowid, content, section_title, file_path)
                VALUES (new.id, new.content, new.section_title, new.file_path);
            END;
        """)
        conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
                embedding float[{EMBEDDING_DIMENSIONS}]
            )
        """)
        self.upsert_meta("schema_version", "1")
        self.upsert_meta("embedding_model", EMBEDDING_MODEL)
        self.upsert_meta("embedding_dimensions", str(EMBEDDING_DIMENSIONS))
        conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def upsert_meta(self, key: str, value: str) -> None:
        self._get_conn().execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
        )

    def get_meta(self, key: str) -> str | None:
        row = self._get_conn().execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def upsert_file(
        self, path: str, content_hash: str, mtime_ns: int, size_bytes: int, epoch: int
    ) -> None:
        self._get_conn().execute(
            """INSERT OR REPLACE INTO files(path, content_hash, mtime_ns, size_bytes, indexed_at_epoch)
               VALUES (?, ?, ?, ?, ?)""",
            (path, content_hash, mtime_ns, size_bytes, epoch),
        )

    def get_file_hash(self, path: str) -> str | None:
        row = self._get_conn().execute(
            "SELECT content_hash FROM files WHERE path = ?", (path,)
        ).fetchone()
        return row[0] if row else None

    def get_all_file_paths(self) -> list[str]:
        rows = self._get_conn().execute("SELECT path FROM files").fetchall()
        return [r[0] for r in rows]

    def delete_file(self, path: str) -> None:
        self._get_conn().execute("DELETE FROM files WHERE path = ?", (path,))

    def get_chunk_ids_for_file(self, path: str) -> list[int]:
        rows = self._get_conn().execute(
            "SELECT id FROM chunks WHERE file_path = ?", (path,)
        ).fetchall()
        return [r[0] for r in rows]

    def delete_chunks_for_file(self, path: str) -> None:
        self._get_conn().execute("DELETE FROM chunks WHERE file_path = ?", (path,))

    def delete_vectors_for_chunk_ids(self, ids: list[int]) -> None:
        conn = self._get_conn()
        for chunk_id in ids:
            conn.execute("DELETE FROM vec_chunks WHERE rowid = ?", (chunk_id,))

    def insert_chunk(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        section_title: str,
        content: str,
        content_hash: str,
        created_at_epoch: int,
    ) -> int:
        cursor = self._get_conn().execute(
            """INSERT INTO chunks(file_path, start_line, end_line, section_title,
                                  content, content_hash, created_at_epoch)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (file_path, start_line, end_line, section_title, content, content_hash, created_at_epoch),
        )
        chunk_id = cursor.lastrowid
        if chunk_id is None:
            raise RuntimeError("Failed to get lastrowid after chunk insert")
        return chunk_id

    def insert_vector(self, chunk_id: int, embedding: NDArray[np.float32]) -> None:
        self._get_conn().execute(
            "INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
            (chunk_id, _embedding_to_bytes(embedding)),
        )

    def bulk_clear(self) -> None:
        # Drop + recreate vec_chunks so embedding dimension changes take effect.
        # sqlite-vec bakes the dimension into the virtual table schema; plain
        # DELETE leaves it at the old dim and new-dim inserts fail with a
        # shape mismatch. This is the path a model swap takes (MiniLM 384 →
        # EmbeddingGemma 512, etc.).
        conn = self._get_conn()
        conn.execute("DROP TABLE IF EXISTS vec_chunks")
        conn.execute(f"""
            CREATE VIRTUAL TABLE vec_chunks USING vec0(
                embedding float[{EMBEDDING_DIMENSIONS}]
            )
        """)
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM files")
        conn.commit()

    def keyword_search(self, query: str, limit: int, path_prefix: str = "") -> list[dict[str, Any]]:
        conn = self._get_conn()
        fts_query = _quote_fts_query(query)
        if path_prefix:
            sql = """
                SELECT c.file_path, c.start_line, c.end_line, c.content,
                       c.section_title, rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ? AND c.file_path LIKE ?
                ORDER BY rank
                LIMIT ?
            """
            params: tuple = (fts_query, path_prefix + "%", limit)
        else:
            sql = """
                SELECT c.file_path, c.start_line, c.end_line, c.content,
                       c.section_title, rank
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """
            params = (fts_query, limit)
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            fallback_params = (query, path_prefix + "%", limit) if path_prefix else (query, limit)
            try:
                rows = conn.execute(sql, fallback_params).fetchall()
            except sqlite3.OperationalError:
                return []

        results: list[dict[str, Any]] = []
        for row in rows:
            bm25_rank = float(row["rank"])
            score = 1.0 / (1.0 + abs(bm25_rank))
            results.append({
                "file_path": row["file_path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "content": row["content"],
                "section_title": row["section_title"] or "",
                "score": score,
            })
        return results

    def vector_search(self, embedding: NDArray[np.float32], limit: int, path_prefix: str = "") -> list[dict[str, Any]]:
        conn = self._get_conn()
        query_bytes = _embedding_to_bytes(embedding)
        # sqlite-vec doesn't support WHERE filters in MATCH queries,
        # so we over-fetch and filter in Python when path_prefix is set
        fetch_limit = limit * 5 if path_prefix else limit
        rows = conn.execute(
            """
            SELECT v.rowid, v.distance,
                   c.file_path, c.start_line, c.end_line, c.content, c.section_title
            FROM vec_chunks v
            JOIN chunks c ON c.id = v.rowid
            WHERE v.embedding MATCH ?
                AND k = ?
            ORDER BY v.distance
            """,
            (query_bytes, fetch_limit),
        ).fetchall()

        results: list[dict[str, Any]] = []
        for row in rows:
            if path_prefix and not row["file_path"].startswith(path_prefix):
                continue
            distance = float(row["distance"])
            score = 1.0 / (1.0 + distance)
            results.append({
                "file_path": row["file_path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "content": row["content"],
                "section_title": row["section_title"] or "",
                "score": score,
            })
            if len(results) >= limit:
                break
        return results

    def get_stats(self) -> dict[str, Any]:
        conn = self._get_conn()
        file_count: int = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        chunk_count: int = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        vec_count: int = conn.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        model_row = conn.execute("SELECT value FROM meta WHERE key = 'embedding_model'").fetchone()
        model_name = model_row[0] if model_row else "unknown"
        stats: dict[str, Any] = {
            "files": file_count,
            "chunks": chunk_count,
            "vectors": vec_count,
            "model": model_name,
            "backend": "sqlite",
        }
        if self._db_path.exists():
            stats["db_size_kb"] = self._db_path.stat().st_size / 1024
        return stats

    def commit(self) -> None:
        if self._conn:
            self._conn.commit()


# ---------------------------------------------------------------------------
# Postgres backend
# ---------------------------------------------------------------------------

class PostgresMemoryDB:
    """PostgreSQL + pgvector backend."""

    def __init__(self, database_url: str) -> None:
        self._url = database_url
        self._conn: Any = None

    def _get_conn(self) -> Any:
        if self._conn is None or self._conn.closed:
            import psycopg

            self._conn = psycopg.connect(self._url, autocommit=False)
        return self._conn

    def _register_vector(self) -> None:
        """Register pgvector types. Must be called after CREATE EXTENSION vector."""
        from pgvector.psycopg import register_vector

        register_vector(self._get_conn())

    def init_schema(self) -> None:
        conn = self._get_conn()
        cur = conn.cursor()

        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()

        # Register pgvector types now that the extension exists
        self._register_vector()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                mtime_ns BIGINT NOT NULL,
                size_bytes BIGINT NOT NULL,
                indexed_at_epoch BIGINT NOT NULL
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS chunks (
                id SERIAL PRIMARY KEY,
                file_path TEXT NOT NULL REFERENCES files(path),
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                section_title TEXT DEFAULT '',
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                created_at_epoch BIGINT NOT NULL,
                embedding vector({EMBEDDING_DIMENSIONS}),
                search_vector tsvector GENERATED ALWAYS AS (
                    to_tsvector('english', coalesce(content,'') || ' ' || coalesce(section_title,''))
                ) STORED
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_file_path ON chunks(file_path)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_search_vector ON chunks USING GIN(search_vector)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks
            USING hnsw(embedding vector_l2_ops)
            WHERE embedding IS NOT NULL
        """)

        conn.commit()

        self.upsert_meta("schema_version", "1")
        self.upsert_meta("embedding_model", EMBEDDING_MODEL)
        self.upsert_meta("embedding_dimensions", str(EMBEDDING_DIMENSIONS))
        conn.commit()

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

    def upsert_meta(self, key: str, value: str) -> None:
        self._get_conn().cursor().execute(
            "INSERT INTO meta(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = %s",
            (key, value, value),
        )

    def get_meta(self, key: str) -> str | None:
        cur = self._get_conn().cursor()
        cur.execute("SELECT value FROM meta WHERE key = %s", (key,))
        row = cur.fetchone()
        return row[0] if row else None

    def upsert_file(
        self, path: str, content_hash: str, mtime_ns: int, size_bytes: int, epoch: int
    ) -> None:
        self._get_conn().cursor().execute(
            """INSERT INTO files(path, content_hash, mtime_ns, size_bytes, indexed_at_epoch)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (path) DO UPDATE SET
                   content_hash = EXCLUDED.content_hash,
                   mtime_ns = EXCLUDED.mtime_ns,
                   size_bytes = EXCLUDED.size_bytes,
                   indexed_at_epoch = EXCLUDED.indexed_at_epoch""",
            (path, content_hash, mtime_ns, size_bytes, epoch),
        )

    def get_file_hash(self, path: str) -> str | None:
        cur = self._get_conn().cursor()
        cur.execute("SELECT content_hash FROM files WHERE path = %s", (path,))
        row = cur.fetchone()
        return row[0] if row else None

    def get_all_file_paths(self) -> list[str]:
        cur = self._get_conn().cursor()
        cur.execute("SELECT path FROM files")
        return [r[0] for r in cur.fetchall()]

    def delete_file(self, path: str) -> None:
        self._get_conn().cursor().execute("DELETE FROM files WHERE path = %s", (path,))

    def get_chunk_ids_for_file(self, path: str) -> list[int]:
        cur = self._get_conn().cursor()
        cur.execute("SELECT id FROM chunks WHERE file_path = %s", (path,))
        return [r[0] for r in cur.fetchall()]

    def delete_chunks_for_file(self, path: str) -> None:
        self._get_conn().cursor().execute(
            "DELETE FROM chunks WHERE file_path = %s", (path,)
        )

    def delete_vectors_for_chunk_ids(self, ids: list[int]) -> None:
        # In Postgres, embedding is ON the chunks table — no separate delete needed.
        # Deleting the chunk row deletes the embedding too.
        pass

    def insert_chunk(
        self,
        file_path: str,
        start_line: int,
        end_line: int,
        section_title: str,
        content: str,
        content_hash: str,
        created_at_epoch: int,
    ) -> int:
        cur = self._get_conn().cursor()
        cur.execute(
            """INSERT INTO chunks(file_path, start_line, end_line, section_title,
                                  content, content_hash, created_at_epoch)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (file_path, start_line, end_line, section_title, content, content_hash, created_at_epoch),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("Failed to get id from RETURNING after chunk insert")
        return row[0]

    def insert_vector(self, chunk_id: int, embedding: NDArray[np.float32]) -> None:
        self._get_conn().cursor().execute(
            "UPDATE chunks SET embedding = %s WHERE id = %s",
            (embedding.tolist(), chunk_id),
        )

    def bulk_clear(self) -> None:
        conn = self._get_conn()
        conn.cursor().execute("DELETE FROM chunks")
        conn.cursor().execute("DELETE FROM files")
        conn.commit()

    def keyword_search(self, query: str, limit: int, path_prefix: str = "") -> list[dict[str, Any]]:
        cur = self._get_conn().cursor()
        if path_prefix:
            cur.execute(
                """
                SELECT file_path, start_line, end_line, content, section_title,
                       ts_rank(search_vector, plainto_tsquery('english', %s)) AS score
                FROM chunks
                WHERE search_vector @@ plainto_tsquery('english', %s)
                  AND file_path LIKE %s
                ORDER BY score DESC
                LIMIT %s
                """,
                (query, query, path_prefix + "%", limit),
            )
        else:
            cur.execute(
                """
                SELECT file_path, start_line, end_line, content, section_title,
                       ts_rank(search_vector, plainto_tsquery('english', %s)) AS score
                FROM chunks
                WHERE search_vector @@ plainto_tsquery('english', %s)
                ORDER BY score DESC
                LIMIT %s
                """,
                (query, query, limit),
            )
        results: list[dict[str, Any]] = []
        for row in cur.fetchall():
            results.append({
                "file_path": row[0],
                "start_line": row[1],
                "end_line": row[2],
                "content": row[3],
                "section_title": row[4] or "",
                "score": float(row[5]),
            })
        return results

    def vector_search(self, embedding: NDArray[np.float32], limit: int, path_prefix: str = "") -> list[dict[str, Any]]:
        cur = self._get_conn().cursor()
        if path_prefix:
            cur.execute(
                """
                SELECT file_path, start_line, end_line, content, section_title,
                       embedding <-> %s::vector AS distance
                FROM chunks
                WHERE embedding IS NOT NULL AND file_path LIKE %s
                ORDER BY distance
                LIMIT %s
                """,
                (embedding.tolist(), path_prefix + "%", limit),
            )
        else:
            cur.execute(
                """
                SELECT file_path, start_line, end_line, content, section_title,
                       embedding <-> %s::vector AS distance
                FROM chunks
                WHERE embedding IS NOT NULL
                ORDER BY distance
                LIMIT %s
                """,
                (embedding.tolist(), limit),
            )
        results: list[dict[str, Any]] = []
        for row in cur.fetchall():
            distance = float(row[5])
            score = 1.0 / (1.0 + distance)
            results.append({
                "file_path": row[0],
                "start_line": row[1],
                "end_line": row[2],
                "content": row[3],
                "section_title": row[4] or "",
                "score": score,
            })
        return results

    def get_stats(self) -> dict[str, Any]:
        cur = self._get_conn().cursor()
        cur.execute("SELECT COUNT(*) FROM files")
        file_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM chunks")
        chunk_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL")
        vec_count = cur.fetchone()[0]
        cur.execute("SELECT value FROM meta WHERE key = 'embedding_model'")
        row = cur.fetchone()
        model_name = row[0] if row else "unknown"
        return {
            "files": file_count,
            "chunks": chunk_count,
            "vectors": vec_count,
            "model": model_name,
            "backend": "postgres",
        }

    def commit(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.commit()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_memory_db(database_url: str = "") -> SQLiteMemoryDB | PostgresMemoryDB:
    """Return the appropriate backend based on DATABASE_URL.

    If database_url is provided, use it. Otherwise fall back to
    the DATABASE_URL from config (env var). If neither is set, use SQLite.
    """
    url = database_url or DATABASE_URL
    if url:
        return PostgresMemoryDB(url)
    return SQLiteMemoryDB()
