"""Durable cursor, deduplication, and signal-outbox state for Buzz."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS buzz_channel_cursors (
    scope TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    last_created_at INTEGER NOT NULL DEFAULT 0,
    same_second_ids TEXT NOT NULL DEFAULT '[]',
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (scope, channel_id)
);
CREATE TABLE IF NOT EXISTS buzz_seen_events (
    scope TEXT NOT NULL,
    event_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    seen_at INTEGER NOT NULL,
    PRIMARY KEY (scope, event_id)
);
CREATE INDEX IF NOT EXISTS idx_buzz_seen_scope_time
    ON buzz_seen_events(scope, seen_at DESC);
CREATE TABLE IF NOT EXISTS buzz_receipt_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    platform_event_id TEXT,
    last_error TEXT,
    created_at INTEGER NOT NULL,
    sent_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_buzz_receipt_pending
    ON buzz_receipt_outbox(status, next_attempt_at, id);
"""


class BuzzStateStore:
    def __init__(self, path: Path, *, max_seen_ids: int = 4096):
        self.path = Path(path)
        self.max_seen_ids = max(128, int(max_seen_ids))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def cursor(self, scope: str, channel_id: str) -> tuple[int, tuple[str, ...]] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_created_at, same_second_ids FROM buzz_channel_cursors "
                "WHERE scope = ? AND channel_id = ?",
                (scope, channel_id),
            ).fetchone()
        if not row:
            return None
        try:
            identifiers = tuple(str(value) for value in json.loads(row["same_second_ids"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            identifiers = ()
        return int(row["last_created_at"]), identifiers

    def seed_cursor(
        self, scope: str, channel_id: str, created_at: int, event_ids: list[str] | tuple[str, ...]
    ) -> bool:
        """Create a first-run high-water mark without replaying existing history."""
        now = int(time.time())
        identifiers = list(dict.fromkeys(str(value) for value in event_ids))[-256:]
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO buzz_channel_cursors "
                "(scope, channel_id, last_created_at, same_second_ids, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (scope, channel_id, int(created_at), json.dumps(identifiers), now),
            )
            for event_id in identifiers:
                conn.execute(
                    "INSERT OR IGNORE INTO buzz_seen_events "
                    "(scope, event_id, created_at, seen_at) VALUES (?, ?, ?, ?)",
                    (scope, event_id, int(created_at), now),
                )
        return bool(cur.rowcount)

    def record_event_if_new(
        self, scope: str, channel_id: str, event_id: str, created_at: int
    ) -> bool:
        """Atomically dedupe and advance a same-second-safe channel cursor."""
        now = int(time.time())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM buzz_seen_events WHERE scope = ? AND event_id = ?",
                (scope, event_id),
            ).fetchone():
                return False

            row = conn.execute(
                "SELECT last_created_at, same_second_ids FROM buzz_channel_cursors "
                "WHERE scope = ? AND channel_id = ?",
                (scope, channel_id),
            ).fetchone()
            if row:
                last_created_at = int(row["last_created_at"])
                try:
                    same_second = list(json.loads(row["same_second_ids"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    same_second = []
                if created_at < last_created_at:
                    return False
                if created_at == last_created_at:
                    if event_id in same_second:
                        return False
                    same_second = (same_second + [event_id])[-256:]
                else:
                    same_second = [event_id]
                conn.execute(
                    "UPDATE buzz_channel_cursors SET last_created_at = ?, "
                    "same_second_ids = ?, updated_at = ? WHERE scope = ? AND channel_id = ?",
                    (created_at, json.dumps(same_second), now, scope, channel_id),
                )
            else:
                conn.execute(
                    "INSERT INTO buzz_channel_cursors "
                    "(scope, channel_id, last_created_at, same_second_ids, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (scope, channel_id, created_at, json.dumps([event_id]), now),
                )

            conn.execute(
                "INSERT INTO buzz_seen_events (scope, event_id, created_at, seen_at) "
                "VALUES (?, ?, ?, ?)",
                (scope, event_id, created_at, now),
            )
            conn.execute(
                "DELETE FROM buzz_seen_events WHERE scope = ? AND event_id NOT IN "
                "(SELECT event_id FROM buzz_seen_events WHERE scope = ? "
                "ORDER BY seen_at DESC, rowid DESC LIMIT ?)",
                (scope, scope, self.max_seen_ids),
            )
        return True

    def enqueue_receipt(self, idempotency_key: str, payload: dict[str, Any]) -> bool:
        now = int(time.time())
        serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO buzz_receipt_outbox "
                "(idempotency_key, payload_json, created_at) VALUES (?, ?, ?)",
                (idempotency_key, serialized, now),
            )
        return bool(cur.rowcount)

    def claim_receipts(self, *, limit: int = 20) -> list[dict[str, Any]]:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id, idempotency_key, payload_json, attempts FROM buzz_receipt_outbox "
                "WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY id LIMIT ?",
                (now, max(1, min(int(limit), 100))),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    "UPDATE buzz_receipt_outbox SET status = 'sending' "
                    f"WHERE id IN ({placeholders})",
                    ids,
                )
        claimed: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {}
            claimed.append(
                {
                    "id": int(row["id"]),
                    "idempotency_key": row["idempotency_key"],
                    "payload": payload,
                    "attempts": int(row["attempts"]),
                }
            )
        return claimed

    def mark_receipt_sent(self, row_id: int, platform_event_id: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE buzz_receipt_outbox SET status = 'sent', platform_event_id = ?, "
                "sent_at = ?, last_error = NULL WHERE id = ?",
                (platform_event_id, int(time.time()), row_id),
            )

    def release_receipt(self, row_id: int, error: str, attempts: int) -> None:
        delay = min(300, 2 ** min(max(attempts, 1), 8))
        with self._connect() as conn:
            conn.execute(
                "UPDATE buzz_receipt_outbox SET status = 'pending', attempts = ?, "
                "next_attempt_at = ?, last_error = ? WHERE id = ?",
                (attempts, int(time.time()) + delay, error[:240], row_id),
            )

    def recover_sending_receipts(self) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE buzz_receipt_outbox SET status = 'pending' WHERE status = 'sending'"
            )
        return int(cur.rowcount)
