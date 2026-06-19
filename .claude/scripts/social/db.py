"""SQLite persistence for social post queue.

Uses the existing orchestration.db — same DB, new table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from social.models import SocialPost

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS social_post_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'approved', 'posted', 'failed', 'rejected')),
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
    audit_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_social_post_status ON social_post_queue(status);
CREATE INDEX IF NOT EXISTS idx_social_post_channel ON social_post_queue(channel);
CREATE INDEX IF NOT EXISTS idx_social_post_scheduled ON social_post_queue(scheduled_for)
    WHERE scheduled_for IS NOT NULL;
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
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        finally:
            conn.close()

    def insert(self, post: SocialPost) -> int:
        conn = self._connect()
        try:
            cur = conn.execute(
                """INSERT INTO social_post_queue
                   (channel, status, title, body, voice_profile, topic_source,
                    created_at, scheduled_for, audit_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        self, post_id: int, new_status: str, **fields: str | None
    ) -> bool:
        sets = ["status = ?"]
        params: list[str | int | None] = [new_status]
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

    def count_by_status(self, channel: str | None = None) -> dict[str, int]:
        conn = self._connect()
        try:
            if channel:
                rows = conn.execute(
                    "SELECT status, COUNT(*) as cnt FROM social_post_queue WHERE channel = ? GROUP BY status",
                    (channel,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT status, COUNT(*) as cnt FROM social_post_queue GROUP BY status"
                ).fetchall()
            return {r["status"]: r["cnt"] for r in rows}
        finally:
            conn.close()
