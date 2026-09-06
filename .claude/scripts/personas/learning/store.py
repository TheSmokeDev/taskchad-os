"""Profile-local immutable record journal and explicit mutable coordination.

Opening or reading a store never creates it. Missing stores are empty; broken
stores raise. Evidence and events are append-only. Only leases/settings mutate.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from personas.learning.models import (
    LearningError,
    LearningTarget,
    LearningValidationError,
    canonical_json,
    utc_now,
)

RECORD_KINDS = frozenset(
    {
        "experience",
        "expectation",
        "execution",
        "observation",
        "candidate",
        "evaluation",
        "activation",
        "context",
    }
)
SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 1_048_576
_SCHEMA = (
    "CREATE TABLE identity (id INTEGER PRIMARY KEY CHECK(id=1), persona_id TEXT NOT NULL)",
    (
        "CREATE TABLE records (seq INTEGER PRIMARY KEY AUTOINCREMENT, id "
        "TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, source_key TEXT NOT "
        "NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL, "
        "UNIQUE(kind,source_key))"
    ),
    "CREATE INDEX records_kind_seq ON records(kind,seq)",
    (
        "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, id "
        "TEXT NOT NULL UNIQUE, record_id TEXT NOT NULL REFERENCES "
        "records(id), event_type TEXT NOT NULL, source_key TEXT NOT NULL, "
        "created_at TEXT NOT NULL, payload TEXT NOT NULL, "
        "UNIQUE(record_id,source_key))"
    ),
    "CREATE INDEX events_record_seq ON events(record_id,seq)",
    (
        "CREATE TABLE claims (record_id TEXT NOT NULL REFERENCES "
        "records(id), operation TEXT NOT NULL, token TEXT NOT NULL, "
        "expires_at REAL NOT NULL, PRIMARY KEY(record_id,operation))"
    ),
    "CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
    (
        "CREATE TRIGGER records_no_update BEFORE UPDATE ON records BEGIN "
        "SELECT RAISE(ABORT,'immutable record'); END"
    ),
    (
        "CREATE TRIGGER records_no_delete BEFORE DELETE ON records BEGIN "
        "SELECT RAISE(ABORT,'immutable record'); END"
    ),
    (
        "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN "
        "SELECT RAISE(ABORT,'immutable event'); END"
    ),
    (
        "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN "
        "SELECT RAISE(ABORT,'immutable event'); END"
    ),
    (
        "CREATE TRIGGER identity_no_update BEFORE UPDATE ON identity "
        "BEGIN SELECT RAISE(ABORT,'immutable owner'); END"
    ),
    (
        "CREATE TRIGGER identity_no_delete BEFORE DELETE ON identity "
        "BEGIN SELECT RAISE(ABORT,'immutable owner'); END"
    ),
)


class LearningStore:
    def __init__(self, target: LearningTarget):
        self.target = target
        self._data_root = Path(os.path.abspath(target.data_dir))
        self.directory = self._data_root / "learning"
        self.path = self.directory / "learning.db"
        self._active_connection: ContextVar[sqlite3.Connection | None] = ContextVar(
            f"learning_transaction_{id(self)}", default=None
        )

    def _check_path(self) -> None:
        # Resolving two not-yet-created paths separately races directory creation
        # on Windows. Anchor lexically once, then reject physical redirection.
        if not self.path.is_relative_to(self._data_root):
            raise LearningError("learning store escaped target profile")
        for node in (self.path, *self.path.parents):
            try:
                metadata = node.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise LearningError("learning store cannot traverse a symbolic link or junction")

    def _build_initial_database(self, path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO identity(id,persona_id) VALUES(1,?)", (self.target.persona_id,)
                )
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        finally:
            connection.close()

    def _initialize_if_missing(self) -> None:
        if self.path.exists():
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        self._check_path()
        descriptor, filename = tempfile.mkstemp(
            prefix=".learning-init-", suffix=".sqlite3", dir=self.directory
        )
        os.close(descriptor)
        temporary = Path(filename)
        try:
            self._build_initial_database(temporary)
            # Windows rename refuses an existing destination. POSIX rename would
            # replace it, so use exclusive hard-link publication on that platform.
            try:
                if os.name == "nt":
                    os.rename(temporary, self.path)
                else:
                    os.link(temporary, self.path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

    @contextmanager
    def connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection | None]:
        active = self._active_connection.get()
        if active is not None:
            yield active
            return
        self._check_path()
        if not write and not self.path.exists():
            yield None
            return
        conn = None
        try:
            if write:
                self._initialize_if_missing()
                self._check_path()
                conn = sqlite3.connect(self.path, timeout=10)
            else:
                conn = sqlite3.connect(
                    f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10
                )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if write:
                conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise LearningError("unsupported learning database schema")
            owner = conn.execute("SELECT persona_id FROM identity WHERE id=1").fetchone()
            if owner is None or owner["persona_id"] != self.target.persona_id:
                raise LearningError("learning database belongs to another profile")
            yield conn
            if write:
                conn.commit()
        except sqlite3.Error as exc:
            if conn is not None:
                conn.rollback()
            raise LearningError("learning store operation failed") from exc
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        """Serialize multi-record validation/transitions without nested commits."""
        if self._active_connection.get() is not None:
            yield
            return
        with self.connection(write=True) as conn:
            token = self._active_connection.set(conn)
            try:
                yield
            finally:
                self._active_connection.reset(token)

    @staticmethod
    def _payload(payload: dict[str, Any]) -> str:
        if not isinstance(payload, dict):
            raise LearningError("record payload must be an object")
        if set(payload) & {"id", "kind", "persona_id", "created_at", "_seq"}:
            raise LearningError("record payload contains host-owned envelope fields")
        encoded = canonical_json(payload)
        if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise LearningError("learning payload exceeds record budget")
        return encoded

    def _record(self, conn: sqlite3.Connection, row: sqlite3.Row, *, events=None) -> dict[str, Any]:
        result = json.loads(row["payload"])
        result.update(
            id=row["id"],
            kind=row["kind"],
            persona_id=self.target.persona_id,
            created_at=row["created_at"],
        )
        if events is None:
            events = conn.execute(
                "SELECT event_type,payload,created_at FROM events WHERE record_id=? ORDER BY seq",
                (row["id"],),
            )
        for event in events:
            data = json.loads(event["payload"])
            if event["event_type"] == "status" and "status" in data:
                result["status"] = data["status"]
                result["status_reason"] = data.get("reason", "")
                result["updated_at"] = event["created_at"]
            elif event["event_type"] == "rollback":
                result["status"] = "rolled_back"
                result["rollback"] = data
        return result

    def _many_rows(self, conn, rows) -> list[dict[str, Any]]:
        """Batch event projection; context reads must not do one query per method."""
        rows = list(rows)
        grouped = {row["id"]: [] for row in rows}
        ids = list(grouped)
        for offset in range(0, len(ids), 500):
            batch = ids[offset : offset + 500]
            marks = ",".join("?" for _ in batch)
            for event in conn.execute(
                "SELECT record_id,event_type,payload,created_at FROM events "
                f"WHERE record_id IN ({marks}) ORDER BY seq",
                batch,
            ):
                grouped[event["record_id"]].append(event)
        return [self._record(conn, row, events=grouped[row["id"]]) for row in rows]

    def many(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        unique = list(dict.fromkeys(ids))
        with self.connection() as conn:
            if conn is None:
                return {}
            rows = []
            for offset in range(0, len(unique), 500):
                batch = unique[offset : offset + 500]
                marks = ",".join("?" for _ in batch)
                rows.extend(
                    conn.execute(f"SELECT * FROM records WHERE id IN ({marks})", batch).fetchall()
                )
            return {record["id"]: record for record in self._many_rows(conn, rows)}

    def active_pairs(self, task: str | None = None, *, limit: int = 128) -> list[dict[str, Any]]:
        """Select bounded current methods using indexed record/event relationships."""
        import re

        words = list(dict.fromkeys(re.findall(r"[\w-]{3,}", (task or "").casefold())))[:12]
        text = (
            "lower(coalesce(json_extract(c.payload,'$.title'),'') || ' ' || "
            "coalesce(json_extract(c.payload,'$.applicability'),'') || ' ' || "
            "coalesce(json_extract(c.payload,'$.domain'),''))"
        )
        score = "+".join(f"CASE WHEN instr({text},?)>0 THEN 1 ELSE 0 END" for _ in words) or "0"

        def status(alias):
            return (
                "coalesce((SELECT CASE WHEN e.event_type='rollback' THEN 'rolled_back' "
                "ELSE json_extract(e.payload,'$.status') END FROM events e "
                f"WHERE e.record_id={alias}.id AND e.event_type IN ('status','rollback') "
                f"ORDER BY e.seq DESC LIMIT 1),json_extract({alias}.payload,'$.status'))"
            )

        sql = (
            f"SELECT * FROM (SELECT a.*, ({score}) AS relevance, "
            "lower(json_extract(c.payload,'$.applicability')) AS applies FROM records a "
            "JOIN records c ON c.id=json_extract(a.payload,'$.candidate_id') "
            "AND c.kind='candidate' WHERE a.kind='activation' "
            f"AND {status('a')} IN ('active_provisional','active_supported') "
            f"AND {status('c')} IN ('active_provisional','active_supported'))"
        )
        if task is not None:
            sql += " WHERE relevance>0 OR applies IN ('always','all tasks','all turns')"
        sql += " ORDER BY relevance DESC,seq DESC LIMIT ?"
        with self.connection() as conn:
            if conn is None:
                return []
            activations = self._many_rows(conn, conn.execute(sql, (*words, limit)).fetchall())
            candidate_ids = list(dict.fromkeys(item["candidate_id"] for item in activations))
            if not candidate_ids:
                return []
            marks = ",".join("?" for _ in candidate_ids)
            candidates = {
                item["id"]: item
                for item in self._many_rows(
                    conn,
                    conn.execute(f"SELECT * FROM records WHERE id IN ({marks})", candidate_ids),
                )
            }
            return [item | {"candidate": candidates[item["candidate_id"]]} for item in activations]

    def put(self, kind: str, payload: dict[str, Any], *, key: str) -> dict[str, Any]:
        if (
            kind not in RECORD_KINDS
            or not isinstance(key, str)
            or not key.strip()
            or len(key) > 2048
        ):
            raise LearningError("invalid record kind or idempotency key")
        encoded = self._payload(payload)
        with self.connection(write=True) as conn:
            assert conn is not None
            prior = conn.execute(
                "SELECT * FROM records WHERE kind=? AND source_key=?", (kind, key)
            ).fetchone()
            if prior:
                if prior["payload"] != encoded:
                    raise LearningError("idempotency key was reused with different content")
                return self._record(conn, prior)
            record_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL, f"homie-learning:{self.target.persona_id}:{kind}:{key}"
                )
            )
            conn.execute(
                "INSERT INTO records(id,kind,source_key,created_at,payload) VALUES (?,?,?,?,?)",
                (record_id, kind, key, utc_now(), encoded),
            )
            return self._record(
                conn, conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            )

    def get(self, record_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            if conn is None:
                return None
            row = conn.execute("SELECT * FROM records WHERE id=?", (str(record_id),)).fetchone()
            return self._record(conn, row) if row else None

    def all(self, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is not None and kind not in RECORD_KINDS:
            raise LearningError("unknown learning record kind")
        with self.connection() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT * FROM records" + (" WHERE kind=?" if kind else "") + " ORDER BY seq DESC",
                (kind,) if kind else (),
            ).fetchall()
            return self._many_rows(conn, rows)

    def list(
        self, kind: str | None = None, *, limit: int = 50, cursor: str | None = None
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise LearningValidationError("limit must be between 1 and 200")
        if kind is not None and kind not in RECORD_KINDS:
            raise LearningError("unknown learning record kind")
        try:
            before = int(cursor) if cursor is not None else None
            if before is not None and before <= 0:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise LearningValidationError("invalid learning cursor") from exc
        clauses, params = [], []
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if before is not None:
            clauses.append("seq<?")
            params.append(before)
        sql = (
            "SELECT * FROM records"
            + (" WHERE " + " AND ".join(clauses) if clauses else "")
            + " ORDER BY seq DESC LIMIT ?"
        )
        with self.connection() as conn:
            if conn is None:
                return {"items": [], "next_cursor": None}
            rows = conn.execute(sql, (*params, limit + 1)).fetchall()
            return {
                "items": self._many_rows(conn, rows[:limit]),
                "next_cursor": str(rows[limit - 1]["seq"]) if len(rows) > limit else None,
            }

    def event(
        self, record_id: str, event_type: str, payload: dict[str, Any], *, key: str
    ) -> dict[str, Any]:
        encoded = canonical_json(payload)
        if not event_type or not key:
            raise LearningError("event type and idempotency key are required")
        with self.connection(write=True) as conn:
            assert conn is not None
            if conn.execute("SELECT id FROM records WHERE id=?", (record_id,)).fetchone() is None:
                raise LearningError("learning record is not owned by this profile")
            prior = conn.execute(
                "SELECT * FROM events WHERE record_id=? AND source_key=?", (record_id, key)
            ).fetchone()
            if prior:
                if prior["payload"] != encoded or prior["event_type"] != event_type:
                    raise LearningError("event key was reused with different content")
                return dict(prior) | {"payload": json.loads(prior["payload"])}
            event_id = str(uuid.uuid4())
            stamp = utc_now()
            conn.execute(
                (
                    "INSERT INTO events(id,record_id,event_type,source_key,created_at,"
                    "payload) VALUES(?,?,?,?,?,?)"
                ),
                (event_id, record_id, event_type, key, stamp, encoded),
            )
            return {
                "id": event_id,
                "record_id": record_id,
                "event_type": event_type,
                "created_at": stamp,
                "payload": payload,
            }

    def events(self, record_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            if conn is None:
                return []
            return [
                dict(row) | {"payload": json.loads(row["payload"])}
                for row in conn.execute(
                    "SELECT * FROM events WHERE record_id=? ORDER BY seq", (record_id,)
                )
            ]

    def claim(self, record_id: str, operation: str, *, ttl_seconds: float = 900) -> str | None:
        if not operation or not 0 < ttl_seconds <= 86400:
            raise LearningError("invalid learning claim")
        with self.connection(write=True) as conn:
            assert conn is not None
            if conn.execute("SELECT id FROM records WHERE id=?", (record_id,)).fetchone() is None:
                raise LearningError("claim record is not owned by this profile")
            old = conn.execute(
                "SELECT expires_at FROM claims WHERE record_id=? AND operation=?",
                (record_id, operation),
            ).fetchone()
            if old and old["expires_at"] > time.time():
                return None
            token = str(uuid.uuid4())
            conn.execute(
                (
                    "INSERT INTO claims(record_id,operation,token,expires_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(record_id,operation) DO UPDATE SET "
                    "token=excluded.token,expires_at=excluded.expires_at"
                ),
                (record_id, operation, token, time.time() + ttl_seconds),
            )
            return token

    def release_claim(self, record_id: str, operation: str, token: str) -> bool:
        with self.connection(write=True) as conn:
            assert conn is not None
            return (
                conn.execute(
                    "DELETE FROM claims WHERE record_id=? AND operation=? AND token=?",
                    (record_id, operation, token),
                ).rowcount
                == 1
            )

    def setting(self, name: str, default: Any = None) -> Any:
        with self.connection() as conn:
            if conn is None:
                return default
            row = conn.execute("SELECT value FROM settings WHERE name=?", (name,)).fetchone()
            return json.loads(row["value"]) if row else default

    def set_setting(self, name: str, value: Any) -> None:
        encoded = canonical_json(value)
        with self.connection(write=True) as conn:
            assert conn is not None
            conn.execute(
                (
                    "INSERT INTO settings(name,value) VALUES(?,?) ON CONFLICT(name) "
                    "DO UPDATE SET value=excluded.value"
                ),
                (name, encoded),
            )
