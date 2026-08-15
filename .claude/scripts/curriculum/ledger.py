"""Profile-local SQLite ledger for curriculum discovery and learning."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

VIDEO_STATES = frozenset(
    {
        "discovered",
        "rejected",
        "skimmed",
        "skimming",
        "admitted",
        "studying",
        "studied",
        "failed",
    }
)
MAX_OPERATION_ATTEMPTS = 3
RETRY_BACKOFF_HOURS = 24
CLAIM_LEASE_HOURS = 2


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class CurriculumLedger:
    """Single-owner persistence surface below a persona's data root."""

    def __init__(self, db_path: Path | str, persona_id: str) -> None:
        self.db_path = Path(db_path)
        self.persona_id = persona_id
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    url TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    channel_id TEXT NOT NULL DEFAULT '',
                    watermark TEXT NOT NULL DEFAULT '',
                    last_polled_at TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS videos (
                    video_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT '',
                    upload_date TEXT NOT NULL DEFAULT '',
                    duration_s REAL,
                    state TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT 'other',
                    score REAL NOT NULL DEFAULT 0,
                    decision TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    decision_method TEXT NOT NULL DEFAULT '',
                    transcript_source TEXT NOT NULL DEFAULT '',
                    raw_path TEXT NOT NULL DEFAULT '',
                    dossier_path TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    runtime_lane TEXT NOT NULL DEFAULT '',
                    cost_usd REAL,
                    error TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT NOT NULL DEFAULT '',
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    studied_at TEXT NOT NULL DEFAULT ''
                    ,skimmed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_videos_state
                    ON videos(state, score DESC, discovered_at ASC);
                CREATE INDEX IF NOT EXISTS idx_videos_source
                    ON videos(source_id, upload_date DESC);

                CREATE TABLE IF NOT EXISTS proposals (
                    proposal_id TEXT PRIMARY KEY,
                    video_id TEXT NOT NULL REFERENCES videos(video_id),
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    routed_at TEXT NOT NULL DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS grades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
                    grade TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL REFERENCES videos(video_id),
                    operation TEXT NOT NULL CHECK(operation IN ('skim', 'study')),
                    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_attempts_daily
                    ON attempts(operation, started_at);
                CREATE INDEX IF NOT EXISTS idx_attempts_video
                    ON attempts(video_id, operation, id DESC);

                CREATE TABLE IF NOT EXISTS runtime_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation TEXT NOT NULL,
                    video_id TEXT NOT NULL DEFAULT '',
                    success INTEGER NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL DEFAULT '',
                    lane TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    cost_usd REAL,
                    tool_calls INTEGER NOT NULL DEFAULT 0,
                    execution_time_ms INTEGER,
                    calls_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_receipts_created
                    ON runtime_receipts(operation, created_at);

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._ensure_column(connection, "videos", "attempts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(connection, "videos", "last_attempt_at", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(connection, "videos", "skimmed_at", "TEXT NOT NULL DEFAULT ''")
            connection.execute(
                """
                INSERT INTO meta(key, value) VALUES ('schema_version', '1')
                ON CONFLICT(key) DO NOTHING
                """
            )
            owner = connection.execute(
                "SELECT value FROM meta WHERE key='owner_persona_id'"
            ).fetchone()
            if owner is None:
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('owner_persona_id', ?)",
                    (self.persona_id,),
                )
            elif str(owner["value"]) != self.persona_id:
                raise ValueError(
                    "Curriculum ledger owner mismatch: "
                    f"expected {self.persona_id!r}, found {owner['value']!r}"
                )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def upsert_source(
        self,
        source_id: str,
        *,
        kind: str,
        url: str,
        policy: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO sources(source_id, kind, url, policy, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    kind=excluded.kind,
                    url=excluded.url,
                    policy=excluded.policy,
                    metadata_json=excluded.metadata_json
                """,
                (
                    source_id,
                    kind,
                    url,
                    policy,
                    json.dumps(metadata or {}, sort_keys=True),
                ),
            )

    def update_source_poll(
        self,
        source_id: str,
        *,
        channel_id: str = "",
        watermark: str = "",
        error: str = "",
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE sources
                SET channel_id=CASE WHEN ? <> '' THEN ? ELSE channel_id END,
                    watermark=CASE WHEN ? <> '' THEN ? ELSE watermark END,
                    last_polled_at=?,
                    last_error=?
                WHERE source_id=?
                """,
                (
                    channel_id,
                    channel_id,
                    watermark,
                    watermark,
                    _now(),
                    error,
                    source_id,
                ),
            )

    def list_sources(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM sources ORDER BY source_id").fetchall()
        return [dict(row) for row in rows]

    def discover_video(self, video: dict[str, Any]) -> bool:
        """Insert a discovered video. Return True only for a new ID."""
        now = _now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO videos(
                    video_id, source_id, url, title, channel, upload_date,
                    duration_s, state, discovered_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?)
                """,
                (
                    video["video_id"],
                    video["source_id"],
                    video["url"],
                    video["title"],
                    video.get("channel", ""),
                    video.get("upload_date", ""),
                    video.get("duration_s"),
                    now,
                    now,
                ),
            )
        return cursor.rowcount == 1

    def import_studied_video(
        self,
        *,
        video_id: str,
        source_id: str,
        url: str,
        title: str,
        channel: str,
        upload_date: str,
        dossier_path: str,
    ) -> bool:
        """Register an already-synthesized seed page as studied.

        This is the manifest bridge: later channel discovery uses the same
        immutable video ID and therefore inserts only true deltas.
        """
        now = _now()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT state FROM videos WHERE video_id=?", (video_id,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO videos(
                    video_id, source_id, url, title, channel, upload_date,
                    state, topic, score, decision, reason, decision_method,
                    transcript_source, dossier_path, discovered_at, updated_at,
                    studied_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'studied', 'seed', 100, 'deep',
                          'imported private synthesized seed', 'seed-import',
                          'vendor-seed', ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    source_id=excluded.source_id,
                    url=CASE WHEN excluded.url <> '' THEN excluded.url ELSE videos.url END,
                    title=excluded.title,
                    channel=excluded.channel,
                    upload_date=excluded.upload_date,
                    state='studied',
                    decision='deep',
                    reason='imported private synthesized seed',
                    decision_method='seed-import',
                    transcript_source='vendor-seed',
                    dossier_path=excluded.dossier_path,
                    updated_at=excluded.updated_at,
                    studied_at=excluded.studied_at
                """,
                (
                    video_id,
                    source_id,
                    url,
                    title,
                    channel,
                    upload_date,
                    dossier_path,
                    now,
                    now,
                    now,
                ),
            )
        return existing is None

    def prune_seed_imports(self, source_id: str, valid_video_ids: set[str]) -> list[dict[str, Any]]:
        """Remove manifest rows previously imported from this seed but now absent."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM videos
                WHERE source_id=? AND decision_method='seed-import'
                """,
                (source_id,),
            ).fetchall()
            stale = [dict(row) for row in rows if str(row["video_id"]) not in valid_video_ids]
            if stale:
                connection.executemany(
                    """
                    DELETE FROM videos
                    WHERE video_id=? AND source_id=? AND decision_method='seed-import'
                    """,
                    [(row["video_id"], source_id) for row in stale],
                )
        return stale

    def set_admission(
        self,
        video_id: str,
        *,
        decision: str,
        score: float,
        topic: str,
        reason: str,
        method: str,
    ) -> bool:
        state = {
            "reject": "rejected",
            "skim": "skimmed",
            "deep": "admitted",
        }[decision]
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE videos
                SET state=?, decision=?, score=?, topic=?, reason=?,
                    decision_method=?, updated_at=?, error=''
                WHERE video_id=? AND state IN ('discovered', 'failed')
                """,
                (
                    state,
                    decision,
                    score,
                    topic,
                    reason,
                    method,
                    _now(),
                    video_id,
                ),
            )
        return cursor.rowcount == 1

    def pre_admit_operator_drop(
        self,
        video_id: str,
        *,
        topic: str,
        reason: str,
        method: str,
    ) -> bool:
        """Force one row to deep-study admission on the operator's imperative.

        Unlike `set_admission` this overrides a prior automatic decision — an
        operator drop must beat a metadata reject or a skim deferral. It never
        touches an in-flight claim or immutable studied evidence; the WHERE
        clause is the physical guard (Rule 2), so a concurrent claim wins and
        the caller sees `False`.
        """
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE videos
                SET state='admitted', decision='deep', score=100, topic=?,
                    reason=?, decision_method=?, updated_at=?, error=''
                WHERE video_id=?
                  AND state IN ('discovered', 'rejected', 'skimmed', 'admitted', 'failed')
                """,
                (topic, reason, method, _now(), video_id),
            )
        return cursor.rowcount == 1

    def claim_study(self, video_id: str) -> int | None:
        """Claim for deep study. Returns a FENCING TOKEN (the attempt id) or None.

        The token identifies THIS claim. Hold it and pass it to `release_claim`
        so a worker that resumed after its lease expired cannot release a claim
        that now belongs to someone else.
        """
        return self._claim_operation(
            video_id,
            operation="study",
            in_progress_state="studying",
            ready_state="admitted",
            decision="deep",
        )

    def claim_skim(self, video_id: str) -> int | None:
        """Claim for skim. Returns a FENCING TOKEN (the attempt id) or None."""
        return self._claim_operation(
            video_id,
            operation="skim",
            in_progress_state="skimming",
            ready_state="skimmed",
            decision="skim",
        )

    def _claim_operation(
        self,
        video_id: str,
        *,
        operation: str,
        in_progress_state: str,
        ready_state: str,
        decision: str,
    ) -> int | None:
        now = datetime.now(UTC)
        now_text = now.isoformat(timespec="seconds")
        retry_cutoff = (now - timedelta(hours=RETRY_BACKOFF_HOURS)).isoformat(timespec="seconds")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state, decision FROM videos WHERE video_id=?",
                (video_id,),
            ).fetchone()
            if row is None:
                return None
            if not (
                row["state"] == ready_state
                or (row["state"] == "failed" and row["decision"] == decision)
            ):
                return None
            history = connection.execute(
                """
                SELECT COUNT(*) AS count, MAX(started_at) AS last_started
                FROM attempts WHERE video_id=? AND operation=?
                """,
                (video_id, operation),
            ).fetchone()
            attempt_count = int(history["count"] or 0)
            last_started = str(history["last_started"] or "")
            if attempt_count >= MAX_OPERATION_ATTEMPTS:
                connection.execute(
                    """
                    UPDATE videos SET error=?, updated_at=?
                    WHERE video_id=? AND state='failed'
                    """,
                    (
                        f"{operation} reached the maximum retry count; manual review required",
                        now_text,
                        video_id,
                    ),
                )
                return None
            if row["state"] == "failed" and last_started > retry_cutoff:
                return None
            cursor = connection.execute(
                """
                UPDATE videos SET state=?, updated_at=?, error='',
                    attempts=attempts + 1, last_attempt_at=?
                WHERE video_id=? AND state=?
                """,
                (
                    in_progress_state,
                    now_text,
                    now_text,
                    video_id,
                    str(row["state"]),
                ),
            )
            if cursor.rowcount != 1:
                return None
            inserted = connection.execute(
                """
                INSERT INTO attempts(video_id, operation, status, started_at)
                VALUES (?, ?, 'running', ?)
                """,
                (video_id, operation, now_text),
            )
            # The attempt id IS the fencing token — unique per claim, so a
            # later claim can never be mistaken for this one.
            return int(inserted.lastrowid)

    def complete_skim(
        self,
        video_id: str,
        *,
        promote: bool,
        score: float,
        reason: str,
        method: str,
        transcript_source: str,
        raw_path: str,
        provider: str,
        model: str,
        runtime_lane: str,
        cost_usd: float | None,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE videos
                SET state=?, decision=?, score=?, reason=?,
                    decision_method=?, transcript_source=?, raw_path=?,
                    provider=?, model=?, runtime_lane=?, cost_usd=?,
                    error='', updated_at=?, skimmed_at=?
                WHERE video_id=? AND state='skimming' AND decision='skim'
                """,
                (
                    "admitted" if promote else "rejected",
                    "deep" if promote else "reject",
                    score,
                    reason[:1000],
                    method,
                    transcript_source,
                    raw_path,
                    provider,
                    model,
                    runtime_lane,
                    cost_usd,
                    _now(),
                    _now(),
                    video_id,
                ),
            )
            if cursor.rowcount == 1:
                self._complete_latest_attempt(connection, video_id, "skim")
        if cursor.rowcount != 1:
            raise RuntimeError("Skim state changed before completion.")

    def complete_study(
        self,
        video_id: str,
        *,
        transcript_source: str,
        raw_path: str,
        dossier_path: str,
        provider: str,
        model: str,
        runtime_lane: str,
        cost_usd: float | None,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE videos
                SET state='studied', transcript_source=?, raw_path=?,
                    dossier_path=?, provider=?, model=?, runtime_lane=?,
                    cost_usd=?, error='', updated_at=?, studied_at=?
                WHERE video_id=? AND state='studying'
                """,
                (
                    transcript_source,
                    raw_path,
                    dossier_path,
                    provider,
                    model,
                    runtime_lane,
                    cost_usd,
                    _now(),
                    _now(),
                    video_id,
                ),
            )
            if cursor.rowcount == 1:
                self._complete_latest_attempt(connection, video_id, "study")
        if cursor.rowcount != 1:
            raise RuntimeError("Study state changed before completion.")

    def release_claim(
        self,
        video_id: str,
        *,
        operation: str,
        in_progress_state: str,
        ready_state: str,
        attempt_id: int | None,
    ) -> bool:
        """Undo MY claim so the item stays retryable with NO attempt consumed.

        For a refusal that is not the item's fault — an operator kill switch
        that turned the capability off AFTER the row was claimed. `fail_video`
        is the wrong shape there: it burns one of `MAX_OPERATION_ATTEMPTS` and
        parks the row in `failed` behind a retry backoff, so an operator who
        flips the switch back on finds a budget the refusal already spent.

        `attempt_id` is the FENCING TOKEN the claim returned, and it is what
        makes this safe under lease expiry. State is not ownership: a worker
        whose lease expired, whose claim `recover_stale_claims` reaped, and
        whose row a SECOND worker then re-claimed would still see
        `state == in_progress_state` on its way out — and would release the new
        worker's claim, decrementing their counter and deleting their running
        attempt. Matching the token means only the claim that took the row can
        give it back.

        Restores the pre-claim state exactly: the row returns to `ready_state`,
        the counter is decremented, and the `attempts` row THIS claim inserted
        is deleted so it never counts against the cap.
        """
        if attempt_id is None:
            return False
        with self._connection() as connection:
            owner = connection.execute(
                """
                SELECT id FROM attempts
                WHERE video_id=? AND operation=? AND status='running'
                ORDER BY id DESC LIMIT 1
                """,
                (video_id, operation),
            ).fetchone()
            if owner is None or int(owner["id"]) != int(attempt_id):
                # The running claim belongs to someone else, or is already done.
                return False
            cursor = connection.execute(
                """
                UPDATE videos
                SET state=?, updated_at=?, error='',
                    attempts=CASE WHEN attempts > 0 THEN attempts - 1 ELSE 0 END
                WHERE video_id=? AND state=?
                """,
                (ready_state, _now(), video_id, in_progress_state),
            )
            if cursor.rowcount != 1:
                # Not ours to release — already completed, failed, or reclaimed.
                return False
            connection.execute("DELETE FROM attempts WHERE id=?", (int(attempt_id),))
            return True

    def fail_video(self, video_id: str, error: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE videos SET state='failed', error=?, updated_at=?
                WHERE video_id=?
                """,
                (error[:2000], _now(), video_id),
            )
            connection.execute(
                """
                UPDATE attempts SET status='failed', completed_at=?, error=?
                WHERE id=(
                    SELECT id FROM attempts
                    WHERE video_id=? AND status='running'
                    ORDER BY id DESC LIMIT 1
                )
                """,
                (_now(), error[:2000], video_id),
            )

    @staticmethod
    def _complete_latest_attempt(
        connection: sqlite3.Connection, video_id: str, operation: str
    ) -> None:
        connection.execute(
            """
            UPDATE attempts SET status='completed', completed_at=?, error=''
            WHERE id=(
                SELECT id FROM attempts
                WHERE video_id=? AND operation=? AND status='running'
                ORDER BY id DESC LIMIT 1
            )
            """,
            (_now(), video_id, operation),
        )

    def recover_stale_claims(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(hours=CLAIM_LEASE_HOURS)).isoformat(
            timespec="seconds"
        )
        now = _now()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT video_id FROM videos
                WHERE state IN ('skimming', 'studying') AND updated_at < ?
                """,
                (cutoff,),
            ).fetchall()
            video_ids = [str(row["video_id"]) for row in rows]
            for video_id in video_ids:
                connection.execute(
                    """
                    UPDATE videos
                    SET state='failed', error='stale curriculum claim recovered',
                        updated_at=?
                    WHERE video_id=? AND state IN ('skimming', 'studying')
                    """,
                    (now, video_id),
                )
                connection.execute(
                    """
                    UPDATE attempts
                    SET status='failed', completed_at=?,
                        error='stale curriculum claim recovered'
                    WHERE id=(
                        SELECT id FROM attempts
                        WHERE video_id=? AND status='running'
                        ORDER BY id DESC LIMIT 1
                    )
                    """,
                    (now, video_id),
                )
        return len(video_ids)

    def get_video(self, video_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM videos WHERE video_id=?", (video_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_videos(
        self,
        *,
        states: tuple[str, ...] | None = None,
        source_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        clauses: list[str] = []
        if states:
            invalid = set(states) - VIDEO_STATES
            if invalid:
                raise ValueError(f"Unknown curriculum states: {sorted(invalid)}")
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            params.extend(states)
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 10_000)))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM videos {where}
                ORDER BY score DESC, upload_date DESC, discovered_at ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def rebalance_admission(
        self,
        video_id: str,
        *,
        decision: str,
        score: float,
        topic: str,
        reason: str,
        method: str,
    ) -> bool:
        state = {
            "reject": "rejected",
            "skim": "skimmed",
            "deep": "admitted",
        }[decision]
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE videos
                SET state=?, decision=?, score=?, topic=?, reason=?,
                    decision_method=?, updated_at=?, error=''
                WHERE video_id=? AND state IN ('admitted', 'skimmed', 'rejected')
                """,
                (
                    state,
                    decision,
                    score,
                    topic,
                    reason,
                    method,
                    _now(),
                    video_id,
                ),
            )
        return cursor.rowcount == 1

    def delete_unstudied_video(self, video_id: str) -> bool:
        """Delete one malformed discovery row; studied evidence is immutable."""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                DELETE FROM videos
                WHERE video_id=? AND state <> 'studied'
                  AND decision_method <> 'seed-import'
                """,
                (video_id,),
            )
        return cursor.rowcount == 1

    def state_counts(self) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS count FROM videos GROUP BY state"
            ).fetchall()
        return {str(row["state"]): int(row["count"]) for row in rows}

    def count_videos(
        self,
        *,
        source_id: str | None = None,
        states: tuple[str, ...] | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if source_id:
            clauses.append("source_id=?")
            params.append(source_id)
        if states:
            invalid = set(states) - VIDEO_STATES
            if invalid:
                raise ValueError(f"Unknown curriculum states: {sorted(invalid)}")
            clauses.append("state IN (" + ",".join("?" for _ in states) + ")")
            params.extend(states)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM videos {where}", params
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def count_active_canon(self, source_ids: tuple[str, ...]) -> int:
        if not source_ids:
            return 0
        placeholders = ",".join("?" for _ in source_ids)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM videos
                WHERE source_id IN ({placeholders})
                  AND decision IN ('skim', 'deep')
                  AND state IN (
                      'skimmed', 'skimming', 'admitted', 'studying',
                      'studied', 'failed'
                  )
                """,
                list(source_ids),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def studies_today(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM attempts
                WHERE operation='study' AND substr(started_at, 1, 10)=?
                """,
                (today,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def skims_today(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM attempts
                WHERE operation='skim' AND substr(started_at, 1, 10)=?
                """,
                (today,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def add_proposal(
        self,
        video_id: str,
        *,
        title: str,
        body: str,
        target: str = "",
    ) -> str:
        proposal_id = f"cur-{uuid.uuid4().hex[:12]}"
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO proposals(
                    proposal_id, video_id, title, body, target, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (proposal_id, video_id, title, body, target, _now()),
            )
        return proposal_id

    def record_runtime_receipt(
        self,
        operation: str,
        runtime: dict[str, Any],
        *,
        video_id: str = "",
    ) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runtime_receipts(
                    operation, video_id, success, error, session_id, lane,
                    provider, model, cost_usd, tool_calls, execution_time_ms,
                    calls_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation,
                    video_id,
                    int(bool(runtime.get("success", False))),
                    str(runtime.get("error") or "")[:2000],
                    str(runtime.get("session_id") or ""),
                    str(runtime.get("lane") or ""),
                    str(runtime.get("provider") or ""),
                    str(runtime.get("model") or ""),
                    runtime.get("cost_usd"),
                    int(runtime.get("tool_calls") or 0),
                    runtime.get("execution_time_ms"),
                    json.dumps(runtime.get("calls") or [], sort_keys=True),
                    _now(),
                ),
            )
        return int(cursor.lastrowid)

    def list_runtime_receipts(
        self, *, operation: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if operation:
            where = "WHERE operation=?"
            params.append(operation)
        params.append(max(1, min(limit, 1000)))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM runtime_receipts {where}
                ORDER BY id DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_proposals(
        self, *, status: str | None = "pending", limit: int = 50
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if status:
            where = "WHERE status=?"
            params.append(status)
        params.append(max(1, min(limit, 500)))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM proposals {where}
                ORDER BY created_at DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def route_proposal(self, proposal_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE proposals SET status='routed', routed_at=?
                WHERE proposal_id=? AND status='pending'
                """,
                (_now(), proposal_id),
            )
        return cursor.rowcount == 1

    def add_grade(self, proposal_id: str, grade: str, note: str = "") -> None:
        normalized = grade.strip().upper()
        if normalized not in {"A", "B", "C", "D", "F"}:
            raise ValueError("Grade must be A, B, C, D, or F.")
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO grades(proposal_id, grade, note, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (proposal_id, normalized, note[:2000], _now()),
            )

    def list_grades(
        self, proposal_id: str | None = None, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if proposal_id:
            where = "WHERE proposal_id=?"
            params.append(proposal_id)
        params.append(max(1, min(limit, 500)))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM grades {where}
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def set_meta(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO meta(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (key, value),
            )

    def get_meta(self, key: str, default: str = "") -> str:
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default
