"""SQLite persistence for social post queue.

Uses the existing orchestration.db — same DB, new table.
"""

from __future__ import annotations

import hashlib
import json
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
    supersede_reason TEXT,
    publisher_json TEXT
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
    "publisher_json": "TEXT",
}

_ALL_COLUMNS = tuple(SocialPost.__dataclass_fields__.keys())
_MIGRATION_TABLE = "social_post_queue__authority_migration"

_EDITORIAL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS authority_editorial_packages (
    post_id INTEGER NOT NULL REFERENCES social_post_queue(id),
    revision INTEGER NOT NULL CHECK (revision >= 1),
    schema_version TEXT NOT NULL,
    package_json TEXT NOT NULL,
    package_digest TEXT NOT NULL DEFAULT '',
    content_digest TEXT NOT NULL,
    media_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    PRIMARY KEY (post_id, revision)
);
"""

_RESOURCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS authority_resource_reservations (
    resource_week TEXT PRIMARY KEY,
    post_id INTEGER NOT NULL UNIQUE REFERENCES social_post_queue(id),
    created_at TEXT NOT NULL
);
"""


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
                if "'superseded'" not in table_sql or "'verification_required'" not in table_sql:
                    self._rebuild_for_authority_statuses(conn)
                else:
                    existing = {
                        row["name"] for row in conn.execute("PRAGMA table_info(social_post_queue)")
                    }
                    for column, definition in _NEW_COLUMNS.items():
                        if column not in existing:
                            conn.execute(
                                f"ALTER TABLE social_post_queue ADD COLUMN {column} {definition}"
                            )
            for statement in _INDEX_SQL:
                conn.execute(statement)
            conn.execute(_EDITORIAL_TABLE_SQL)
            editorial_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(authority_editorial_packages)")
            }
            if "package_digest" not in editorial_columns:
                # Do not bless previously unbound package metadata by backfilling.
                conn.execute(
                    "ALTER TABLE authority_editorial_packages "
                    "ADD COLUMN package_digest TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(_RESOURCE_TABLE_SQL)
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_authority_editorial_delivered
                   ON authority_editorial_packages(delivered_at, post_id)
                   WHERE delivered_at IS NOT NULL"""
            )
            self._verify_schema(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _rebuild_for_authority_statuses(conn: sqlite3.Connection) -> None:
        before = int(conn.execute("SELECT COUNT(*) FROM social_post_queue").fetchone()[0])
        conn.execute(f"DROP TABLE IF EXISTS {_MIGRATION_TABLE}")
        conn.execute(_TABLE_SQL.replace("social_post_queue", _MIGRATION_TABLE, 1))
        old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(social_post_queue)")}
        common = [column for column in _ALL_COLUMNS if column in old_columns]
        quoted = ", ".join(f'"{column}"' for column in common)
        conn.execute(
            f"INSERT INTO {_MIGRATION_TABLE} ({quoted}) SELECT {quoted} FROM social_post_queue"
        )
        copied = int(conn.execute(f"SELECT COUNT(*) FROM {_MIGRATION_TABLE}").fetchone()[0])
        if copied != before:
            raise RuntimeError(
                f"social queue migration row-count mismatch: before={before}, copied={copied}"
            )
        conn.execute("DROP TABLE social_post_queue")
        conn.execute(f"ALTER TABLE {_MIGRATION_TABLE} RENAME TO social_post_queue")

    @staticmethod
    def _verify_schema(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(social_post_queue)")}
        missing_columns = set(_ALL_COLUMNS) - columns
        if missing_columns:
            raise RuntimeError(
                "social queue migration missing columns: " + ", ".join(sorted(missing_columns))
            )
        table_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='social_post_queue'"
        ).fetchone()
        table_sql = str(table_row["sql"] or "") if table_row else ""
        if "'superseded'" not in table_sql or "'verification_required'" not in table_sql:
            raise RuntimeError("social queue migration did not install authority statuses")
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(social_post_queue)")}
        missing_indexes = _REQUIRED_INDEXES - indexes
        if missing_indexes:
            raise RuntimeError(
                "social queue migration missing indexes: " + ", ".join(sorted(missing_indexes))
            )

        editorial_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(authority_editorial_packages)")
        }
        required_editorial = {
            "post_id",
            "revision",
            "schema_version",
            "package_json",
            "package_digest",
            "content_digest",
            "media_digest",
            "created_at",
            "delivered_at",
        }
        if required_editorial - editorial_columns:
            raise RuntimeError("authority editorial migration missing columns")
        editorial_indexes = {
            row["name"] for row in conn.execute("PRAGMA index_list(authority_editorial_packages)")
        }
        if "idx_authority_editorial_delivered" not in editorial_indexes:
            raise RuntimeError("authority editorial migration missing delivery index")

    def insert(
        self,
        post: SocialPost,
        *,
        editorial_package: dict | None = None,
        resource_week: str | None = None,
    ) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """INSERT INTO social_post_queue
                   (channel, status, title, body, voice_profile, topic_source,
                    created_at, scheduled_for, audit_id, media_path, media_type,
                    source_packet_id, revision, content_digest, media_digest,
                    verification_state, receipt_json, supersede_reason, publisher_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    post.publisher_json,
                ),
            )
            post_id = int(cur.lastrowid)
            if editorial_package is not None:
                self._insert_editorial(
                    conn,
                    post_id,
                    post.revision,
                    editorial_package,
                    content_digest=post.content_digest,
                    media_digest=post.media_digest,
                    created_at=post.created_at,
                    resource_week=resource_week,
                )
            conn.commit()
            return post_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _insert_editorial(
        conn: sqlite3.Connection,
        post_id: int,
        revision: int,
        package: dict,
        *,
        content_digest: str,
        media_digest: str,
        created_at: str,
        resource_week: str | None,
    ) -> None:
        """Called only inside the queue mutation transaction; no independent commit."""
        payload = json.dumps(package, ensure_ascii=False, sort_keys=True, allow_nan=False)
        conn.execute(
            """INSERT INTO authority_editorial_packages
               (post_id, revision, schema_version, package_json, package_digest, content_digest,
                media_digest, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                post_id,
                revision,
                package["schema_version"],
                payload,
                hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                content_digest,
                media_digest,
                created_at,
            ),
        )
        if package.get("cta", {}).get("kind") != "resource_drop":
            return
        existing = conn.execute(
            "SELECT resource_week FROM authority_resource_reservations WHERE post_id = ?",
            (post_id,),
        ).fetchone()
        if existing is not None:
            # A revision is the same resource promise, not a new weekly allowance.
            return
        if not resource_week:
            raise ValueError("A resource-drop draft requires its scheduled ISO week")
        try:
            conn.execute(
                """INSERT INTO authority_resource_reservations
                   (resource_week, post_id, created_at) VALUES (?, ?, ?)""",
                (resource_week, post_id, created_at),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"Resource drop already queued for {resource_week}; choose a non-resource CTA"
            ) from exc

    def get_editorial_record(self, post_id: int, revision: int | None = None) -> dict | None:
        conn = self._connect()
        try:
            if revision is None:
                row = conn.execute(
                    """SELECT e.* FROM authority_editorial_packages e
                       JOIN social_post_queue p ON p.id = e.post_id AND p.revision = e.revision
                       WHERE p.id = ?""",
                    (post_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM authority_editorial_packages
                       WHERE post_id = ? AND revision = ?""",
                    (post_id, revision),
                ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_delivered_editorial(self, *, limit: int = 14) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT e.package_json, e.delivered_at AS sort_at, e.post_id,
                          NULL AS legacy_body, NULL AS legacy_posted_at
                   FROM authority_editorial_packages e
                   WHERE e.delivered_at IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM authority_editorial_packages newer
                       WHERE newer.post_id = e.post_id AND newer.delivered_at IS NOT NULL
                         AND newer.revision > e.revision
                   )
                   UNION ALL
                   SELECT NULL AS package_json, p.posted_at AS sort_at, p.id AS post_id,
                          p.body AS legacy_body, p.posted_at AS legacy_posted_at
                   FROM social_post_queue p
                   WHERE p.status = 'posted' AND lower(p.channel) IN ('linkedin', 'li')
                     AND p.posted_at IS NOT NULL
                     AND (p.post_url LIKE 'https://www.linkedin.com/%'
                          OR p.post_url LIKE 'https://linkedin.com/%')
                     AND NOT EXISTS (
                         SELECT 1 FROM authority_editorial_packages e
                         WHERE e.post_id = p.id AND e.delivered_at IS NOT NULL
                     )
                   ORDER BY sort_at DESC, post_id DESC LIMIT ?""",
                (max(0, limit),),
            ).fetchall()
            return [
                json.loads(row["package_json"])
                if row["package_json"] is not None
                else {
                    "public_body": row["legacy_body"],
                    "format": "legacy",
                    "delivery_provenance": "confirmed_posted_legacy",
                    "post_id": row["post_id"],
                    "posted_at": row["legacy_posted_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    def mark_editorial_delivered(
        self,
        post_id: int,
        revision: int,
        *,
        content_digest: str,
        media_digest: str,
        delivered_at: str,
    ) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                """UPDATE authority_editorial_packages
                   SET delivered_at = COALESCE(delivered_at, ?)
                   WHERE post_id = ? AND revision = ? AND content_digest = ? AND media_digest = ?
                     AND EXISTS (
                         SELECT 1 FROM social_post_queue p
                         WHERE p.id = post_id AND p.revision = authority_editorial_packages.revision
                           AND p.status = 'draft' AND p.content_digest = ? AND p.media_digest = ?
                     )""",
                (
                    delivered_at,
                    post_id,
                    revision,
                    content_digest,
                    media_digest,
                    content_digest,
                    media_digest,
                ),
            )
            conn.commit()
            return cur.rowcount == 1
        finally:
            conn.close()

    def resource_week_for_post(self, post_id: int) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT resource_week FROM authority_resource_reservations WHERE post_id = ?",
                (post_id,),
            ).fetchone()
            return str(row["resource_week"]) if row else None
        finally:
            conn.close()

    def update_editorial_draft(
        self,
        post_id: int,
        *,
        expected_revision: int,
        fields: dict[str, str | int | None],
        editorial_package: dict,
        created_at: str,
        resource_week: str | None = None,
    ) -> bool:
        allowed = {
            "title",
            "body",
            "media_path",
            "media_type",
            "content_digest",
            "media_digest",
            "verification_state",
            "receipt_json",
            "publisher_json",
        }
        if not fields or set(fields) - allowed:
            raise ValueError("Invalid editorial revision fields")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            sets = ["revision = revision + 1"] + [f"{field} = ?" for field in fields]
            cur = conn.execute(
                f"""UPDATE social_post_queue SET {", ".join(sets)}
                    WHERE id = ? AND status = 'draft' AND revision = ?""",
                [*fields.values(), post_id, expected_revision],
            )
            if cur.rowcount != 1:
                conn.rollback()
                return False
            row = conn.execute(
                "SELECT * FROM social_post_queue WHERE id = ?",
                (post_id,),
            ).fetchone()
            self._insert_editorial(
                conn,
                post_id,
                int(row["revision"]),
                editorial_package,
                content_digest=row["content_digest"],
                media_digest=row["media_digest"],
                created_at=created_at,
                resource_week=resource_week,
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
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

    def list_by_status(self, status: str, *, limit: int = 50) -> list[SocialPost]:
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
        expected_channel: str | None = None,
        expected_publisher_json: str | None = None,
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
        if expected_channel is not None:
            where.extend(["channel = ?", "publisher_json IS ?"])
            params.extend([expected_channel, expected_publisher_json])
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
                f"""UPDATE social_post_queue SET {", ".join(sets)}
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
