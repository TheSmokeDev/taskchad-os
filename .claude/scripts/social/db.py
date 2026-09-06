"""SQLite persistence for social post queue.

Uses the existing orchestration.db — same DB, new table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from social.models import SocialPost

_TABLE_SQL = """
CREATE TABLE social_post_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN (
            'draft', 'approved', 'posted', 'failed', 'rejected',
            'superseded', 'verification_required'
        )),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    voice_profile TEXT NOT NULL DEFAULT '',
    topic_source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')),
    scheduled_for TEXT,
    approved_at TEXT,
    posted_at TEXT,
    post_url TEXT,
    rejection_reason TEXT,
    error TEXT,
    audit_id TEXT,
    external_ref TEXT,
    media_path TEXT,
    media_type TEXT,
    claimed_at TEXT,
    source_packet_id TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    content_digest TEXT NOT NULL DEFAULT '',
    media_digest TEXT NOT NULL DEFAULT '',
    verification_state TEXT NOT NULL DEFAULT 'pending',
    receipt_json TEXT,
    supersede_reason TEXT
);
"""

_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_social_post_status ON social_post_queue(status)",
    "CREATE INDEX IF NOT EXISTS idx_social_post_channel ON social_post_queue(channel)",
    """CREATE INDEX IF NOT EXISTS idx_social_post_scheduled
       ON social_post_queue(scheduled_for) WHERE scheduled_for IS NOT NULL""",
)

_REQUIRED_INDEXES = {
    "idx_social_post_status",
    "idx_social_post_channel",
    "idx_social_post_scheduled",
}

_NEW_COLUMNS: dict[str, str] = {
    "external_ref": "TEXT",
    "media_path": "TEXT",
    "media_type": "TEXT",
    "claimed_at": "TEXT",
    "source_packet_id": "TEXT",
    "revision": "INTEGER NOT NULL DEFAULT 1",
    "content_digest": "TEXT NOT NULL DEFAULT ''",
    "media_digest": "TEXT NOT NULL DEFAULT ''",
    "verification_state": "TEXT NOT NULL DEFAULT 'pending'",
    "receipt_json": "TEXT",
    "supersede_reason": "TEXT",
}

_ALL_COLUMNS = tuple(SocialPost.__dataclass_fields__.keys())
_MIGRATION_TABLE = "social_post_queue__authority_migration"


def _row_to_post(row: sqlite3.Row) -> SocialPost:
    return SocialPost(**{k: row[k] for k in row.keys()})


class SocialPostDB:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._ensure_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_tables(self) -> None:
        conn = self._connect()
        try:
            # CHECK constraints cannot be extended with ALTER TABLE.  Keep the
            # entire status/column migration in one transaction, validate the
            # copied row count and indexes, then commit.  A failed verification
            # rolls back to the untouched legacy table.
            conn.execute("BEGIN IMMEDIATE")
            table_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='social_post_queue'"
            ).fetchone()
            if table_row is None:
                conn.execute(_TABLE_SQL)
            else:
                table_sql = str(table_row["sql"] or "")
                if (
                    "'superseded'" not in table_sql
                    or "'verification_required'" not in table_sql
                ):
                    self._rebuild_for_authority_statuses(conn)
                else:
                    existing = {
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(social_post_queue)")
                    }
                    for column, definition in _NEW_COLUMNS.items():
                        if column not in existing:
                            conn.execute(
                                f"ALTER TABLE social_post_queue ADD COLUMN {column} {definition}"
                            )
            for statement in _INDEX_SQL:
                conn.execute(statement)
            self._verify_schema(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _rebuild_for_authority_statuses(conn: sqlite3.Connection) -> None:
        before = int(
            conn.execute("SELECT COUNT(*) FROM social_post_queue").fetchone()[0]
        )
        conn.execute(f"DROP TABLE IF EXISTS {_MIGRATION_TABLE}")
        conn.execute(_TABLE_SQL.replace("social_post_queue", _MIGRATION_TABLE, 1))
        old_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(social_post_queue)")
        }
        common = [column for column in _ALL_COLUMNS if column in old_columns]
        quoted = ", ".join(f'"{column}"' for column in common)
        conn.execute(
            f"INSERT INTO {_MIGRATION_TABLE} ({quoted}) "
            f"SELECT {quoted} FROM social_post_queue"
        )
        copied = int(
            conn.execute(f"SELECT COUNT(*) FROM {_MIGRATION_TABLE}").fetchone()[0]
        )
        if copied != before:
            raise RuntimeError(
                f"social queue migration row-count mismatch: before={before}, copied={copied}"
            )
        conn.execute("DROP TABLE social_post_queue")
        conn.execute(
            f"ALTER TABLE {_MIGRATION_TABLE} RENAME TO social_post_queue"
        )

    @staticmethod
    def _verify_schema(conn: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(social_post_queue)")
        }
        missing_columns = set(_ALL_COLUMNS) - columns
        if missing_columns:
            raise RuntimeError(
                "social queue migration missing columns: "
                + ", ".join(sorted(missing_columns))
            )
        table_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='social_post_queue'"
        ).fetchone()
        table_sql = str(table_row["sql"] or "") if table_row else ""
        if "'superseded'" not in table_sql or "'verification_required'" not in table_sql:
            raise RuntimeError("social queue migration did not install authority statuses")
        indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(social_post_queue)")
        }
        missing_indexes = _REQUIRED_INDEXES - indexes
        if missing_indexes:
            raise RuntimeError(
                "social queue migration missing indexes: "
                + ", ".join(sorted(missing_indexes))
            )

    def insert(self, post: SocialPost) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT INTO social_post_queue
                   (channel, status, title, body, voice_profile, topic_source,
                    created_at, scheduled_for, audit_id, media_path, media_type,
                    source_packet_id, revision, content_digest, media_digest,
                    verification_state, receipt_json, supersede_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post.channel,
                    post.status,
                    post.title,
                    post.body,
                    post.voice_profile,
                    post.topic_source,
                    post.created_at,
                    post.scheduled_for,
                    post.audit_id,
                    post.media_path,
                    post.media_type,
                    post.source_packet_id,
                    post.revision,
                    post.content_digest,
                    post.media_digest,
                    post.verification_state,
                    post.receipt_json,
                    post.supersede_reason,
                ),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]
        finally:
            conn.close()

    def get(self, post_id: int) -> SocialPost | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM social_post_queue WHERE id = ?", (post_id,)
            ).fetchone()
            return _row_to_post(row) if row else None
        finally:
            conn.close()

    def list_by_status(
        self, status: str, *, limit: int = 50
    ) -> list[SocialPost]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM social_post_queue WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
            return [_row_to_post(r) for r in rows]
        finally:
            conn.close()

    def list_recent(self, *, limit: int = 20) -> list[SocialPost]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM social_post_queue ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [_row_to_post(r) for r in rows]
        finally:
            conn.close()

    def list_due(self, now_iso: str) -> list[SocialPost]:
        """Return approved posts whose scheduled_for is set and <= now.

        Posts without scheduled_for require explicit manual dispatch.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM social_post_queue
                   WHERE status = 'approved'
                     AND scheduled_for IS NOT NULL
                     AND scheduled_for <= ?
                   ORDER BY scheduled_for ASC""",
                (now_iso,),
            ).fetchall()
            return [_row_to_post(r) for r in rows]
        finally:
            conn.close()

    def claim_post(self, post_id: int, now_iso: str) -> bool:
        """Atomically claim an approved post for dispatch (CAS).

        Exactly one claimer wins: the UPDATE only fires while the row is
        still 'approved' and unclaimed. Every dispatch ingress (approve tap,
        /social post, cadence cron, runner) must claim before driving the
        browser — this is what makes a double-tap or a tap racing the cron a
        no-op instead of a double post.
        """
        conn = self._connect()
        try:
            cur = conn.execute(
                """UPDATE social_post_queue SET claimed_at = ?
                   WHERE id = ? AND status = 'approved' AND claimed_at IS NULL""",
                (now_iso, post_id),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def clear_claim(self, post_id: int) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE social_post_queue SET claimed_at = NULL WHERE id = ?",
                (post_id,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def list_stale_claims(self, cutoff_iso: str) -> list[SocialPost]:
        """Claimed rows still 'approved' past the cutoff — the runner died
        mid-flight (or never started). Terminal rows (posted/failed) keep
        their claim stamp as a receipt and are never considered stale.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM social_post_queue
                   WHERE status = 'approved'
                     AND claimed_at IS NOT NULL
                     AND claimed_at <= ?
                   ORDER BY claimed_at ASC""",
                (cutoff_iso,),
            ).fetchall()
            return [_row_to_post(r) for r in rows]
        finally:
            conn.close()

    def set_scheduled_for(self, post_id: int, scheduled_for: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE social_post_queue SET scheduled_for = ? WHERE id = ?",
                (scheduled_for, post_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def update_status(
        self,
        post_id: int,
        new_status: str,
        *,
        expected_status: str | None = None,
        expected_revision: int | None = None,
        expected_content_digest: str | None = None,
        expected_media_digest: str | None = None,
        **fields: str | int | None,
    ) -> bool:
        sets = ["status = ?"]
        params: list[str | int | None] = [new_status]
        for col, val in fields.items():
            sets.append(f"{col} = ?")
            params.append(val)
        where = ["id = ?"]
        params.append(post_id)
        for column, value in (
            ("status", expected_status),
            ("revision", expected_revision),
            ("content_digest", expected_content_digest),
            ("media_digest", expected_media_digest),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                params.append(value)
        conn = self._connect()
        try:
            cur = conn.execute(
                f"UPDATE social_post_queue SET {', '.join(sets)} WHERE {' AND '.join(where)}",
                params,
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def update_fields(self, post_id: int, **fields: str | int | None) -> bool:
        """Update non-status columns (reconcile fills post_url etc.).

        Status changes MUST go through the service transition table — this
        helper refuses them.
        """
        if not fields:
            return False
        if "status" in fields:
            raise ValueError("update_fields cannot change status — use update_status")
        sets = []
        params: list[str | int | None] = []
        for col, val in fields.items():
            sets.append(f"{col} = ?")
            params.append(val)
        params.append(post_id)
        conn = self._connect()
        try:
            cur = conn.execute(
                f"UPDATE social_post_queue SET {', '.join(sets)} WHERE id = ?",
                params,
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def update_draft_revision(
        self,
        post_id: int,
        *,
        expected_revision: int,
        fields: dict[str, str | int | None],
    ) -> bool:
        """Atomically mutate one draft and advance its exact-review revision."""

        if not fields:
            return False
        sets = ["revision = revision + 1"]
        params: list[str | int | None] = []
        for column, value in fields.items():
            if column in {"id", "status", "revision"}:
                raise ValueError(f"draft revision cannot directly update {column}")
            sets.append(f"{column} = ?")
            params.append(value)
        params.extend([post_id, expected_revision])
        conn = self._connect()
        try:
            cur = conn.execute(
                f"""UPDATE social_post_queue SET {', '.join(sets)}
                    WHERE id = ? AND status = 'draft' AND revision = ?""",
                params,
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def supersede_legacy_linkedin_drafts(self, reason: str) -> int:
        """One-shot, history-preserving cutover helper (never run implicitly)."""

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            before = int(
                conn.execute(
                    """SELECT COUNT(*) FROM social_post_queue
                       WHERE status = 'draft' AND lower(channel) IN ('linkedin', 'li')"""
                ).fetchone()[0]
            )
            cur = conn.execute(
                """UPDATE social_post_queue
                   SET status = 'superseded', supersede_reason = ?, claimed_at = NULL
                   WHERE status = 'draft' AND lower(channel) IN ('linkedin', 'li')""",
                (reason,),
            )
            remaining = int(
                conn.execute(
                    """SELECT COUNT(*) FROM social_post_queue
                       WHERE status = 'draft' AND lower(channel) IN ('linkedin', 'li')"""
                ).fetchone()[0]
            )
            if cur.rowcount != before or remaining != 0:
                raise RuntimeError(
                    "legacy LinkedIn supersede verification failed: "
                    f"before={before}, updated={cur.rowcount}, remaining={remaining}"
                )
            conn.commit()
            return cur.rowcount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def count_by_status(self, channel: str | None = None) -> dict[str, int]:
        conn = self._connect()
        try:
            if channel:
                rows = conn.execute(
                    """SELECT status, COUNT(*) as cnt FROM social_post_queue
                       WHERE channel = ? GROUP BY status""",
                    (channel,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT status, COUNT(*) as cnt FROM social_post_queue GROUP BY status"
                ).fetchall()
            return {r["status"]: r["cnt"] for r in rows}
        finally:
            conn.close()
