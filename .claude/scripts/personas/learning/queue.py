"""Durable per-persona queue control state; evidence remains in LearningStore."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .models import LearningError, canonical_json, is_credential_key

PRIORITIES = {
    "observation": 10,
    "regression": 20,
    "correction": 30,
    "requalification": 35,
    "experience": 40,
    "candidate": 45,
    "practice": 50,
}


class LearningQueue:
    def __init__(self, service, *, path: Path | None = None):
        self.persona_id = service.target.persona_id
        self.path = (
            Path(path) if path is not None else service.target.data_dir / "learning" / "queue.db"
        )
        self.root = Path(service.target.data_dir)

    def _check_path(self):
        if not self.path.resolve().is_relative_to(self.root.resolve()):
            raise LearningError("learning queue escaped target profile")
        current = self.path
        while current != self.root and current != current.parent:
            if current.is_symlink() or (
                current.exists() and getattr(current.lstat(), "st_file_attributes", 0) & 0x400
            ):
                raise LearningError("learning queue cannot be a link")
            current = current.parent

    def _validate_owner(self, db):
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != 1:
            raise LearningError("unsupported learning queue schema")
        owner = db.execute("SELECT persona_id FROM queue_identity WHERE id=1").fetchone()
        if owner is None or owner[0] != self.persona_id:
            raise LearningError("learning queue belongs to another profile")
        if db.execute(
            "SELECT 1 FROM learning_jobs WHERE persona_id != ? LIMIT 1", (self.persona_id,)
        ).fetchone():
            raise LearningError("learning queue contains foreign profile jobs")

    @contextmanager
    def _db(self, *, readonly=False):
        self._check_path()
        if readonly:
            if not self.path.exists():
                yield None
                return
            # This queue only uses rollback journaling. Reject foreign WAL state
            # rather than create shared-memory sidecars during an operator GET.
            probe = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
            try:
                if probe.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
                    raise LearningError("unsupported learning queue journal mode")
            finally:
                probe.close()
            db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA query_only=ON")
                self._validate_owner(db)
                yield db
            finally:
                db.close()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_path()
        db = sqlite3.connect(self.path, timeout=2.0)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA busy_timeout=2000")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            existing = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if version == 0 and existing and existing != {"learning_jobs"}:
                raise LearningError("unrecognized learning queue database")
            if version not in {0, 1}:
                raise LearningError("unsupported learning queue schema")
            db.execute("""CREATE TABLE IF NOT EXISTS learning_jobs (
                id TEXT PRIMARY KEY, persona_id TEXT NOT NULL, kind TEXT NOT NULL,
                source_key TEXT NOT NULL, payload TEXT NOT NULL, priority INTEGER NOT NULL,
                stage TEXT NOT NULL, status TEXT NOT NULL, available_at REAL NOT NULL,
                failures INTEGER NOT NULL DEFAULT 0, token TEXT, expires_at REAL,
                last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(persona_id,kind,source_key))""")
            if version == 0:
                if db.execute(
                    "SELECT 1 FROM learning_jobs WHERE persona_id != ? LIMIT 1", (self.persona_id,)
                ).fetchone():
                    raise LearningError("learning queue belongs to another profile")
                db.execute(
                    "CREATE TABLE queue_identity (id INTEGER PRIMARY KEY CHECK(id=1), "
                    "persona_id TEXT NOT NULL)"
                )
                db.execute("INSERT INTO queue_identity VALUES(1,?)", (self.persona_id,))
                db.execute("PRAGMA user_version=1")
            self._validate_owner(db)
            db.commit()
            yield db
            db.commit()
        finally:
            db.close()

    def _row(self, row):
        if row is None:
            return None
        result = dict(row)
        expected_id = hashlib.sha256(
            f"{self.persona_id}\0{result['kind']}\0{result['source_key']}".encode()
        ).hexdigest()
        if (
            result["persona_id"] != self.persona_id
            or result["id"] != expected_id
            or result["kind"] not in PRIORITIES
            or not result["source_key"]
        ):
            raise LearningError("invalid learning queue job identity")
        result["payload"] = json.loads(result["payload"])
        self._payload(result["payload"])
        return result

    @staticmethod
    def _payload(values):
        if not isinstance(values, dict):
            raise LearningError("learning queue payload must be an object")

        def inspect(value):
            if isinstance(value, dict):
                if any(is_credential_key(str(key)) for key in value):
                    raise LearningError("learning queue cannot contain credentials")
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        inspect(values)
        encoded = canonical_json(values)
        if len(encoded.encode("utf-8")) > 1_048_576:
            raise LearningError("learning queue payload exceeds budget")
        return encoded

    def enqueue(
        self,
        kind: str,
        source_key: str,
        *,
        payload: dict | None = None,
        available_at: float | None = None,
        now: float | None = None,
    ) -> dict:
        if (
            kind not in PRIORITIES
            or not isinstance(source_key, str)
            or not source_key.strip()
            or len(source_key) > 4096
        ):
            raise ValueError("Queue job requires a supported kind and stable source key")
        instant = time.time() if now is None else now
        values = dict(payload or {})
        identifier = hashlib.sha256(f"{self.persona_id}\0{kind}\0{source_key}".encode()).hexdigest()
        stage = "observe" if kind == "observation" else "propose"
        if kind in {"candidate", "requalification", "regression"} and values.get("candidate_id"):
            stage = "design"
        encoded = self._payload(values)
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO learning_jobs "
                "(id,persona_id,kind,source_key,payload,priority,stage,status,"
                "available_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,'queued',?,?,?)",
                (
                    identifier,
                    self.persona_id,
                    kind,
                    source_key,
                    encoded,
                    PRIORITIES[kind],
                    stage,
                    instant if available_at is None else available_at,
                    instant,
                    instant,
                ),
            )
            return self._row(
                db.execute("SELECT * FROM learning_jobs WHERE id=?", (identifier,)).fetchone()
            )

    def list(self, *, include_finished: bool = False) -> list[dict]:
        # Inspection must not initialise an absent queue or a persona directory.
        if not self.path.exists():
            return []
        with self._db(readonly=True) as db:
            if db is None:
                return []
            sql = "SELECT * FROM learning_jobs WHERE persona_id=?"
            if not include_finished:
                sql += " AND status NOT IN ('completed','failed')"
            return [
                self._row(row)
                for row in db.execute(sql + " ORDER BY priority,created_at,id", (self.persona_id,))
            ]

    def claim(self, *, ttl_seconds: float | None = None, now: float | None = None) -> dict | None:
        instant = time.time() if now is None else now
        ttl = 90.0 if ttl_seconds is None else ttl_seconds
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE learning_jobs SET status='queued',token=NULL,expires_at=NULL "
                "WHERE persona_id=? AND status='running' AND expires_at<=?",
                (self.persona_id, instant),
            )
            row = db.execute(
                "SELECT * FROM learning_jobs WHERE persona_id=? "
                "AND status IN ('queued','deferred','retry') AND available_at<=? "
                "ORDER BY priority,created_at,id LIMIT 1",
                (self.persona_id, instant),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            db.execute(
                (
                    "UPDATE learning_jobs SET "
                    "status='running',token=?,expires_at=?,updated_at=? WHERE id=?"
                ),
                (token, instant + ttl, instant, row["id"]),
            )
            return self._row(
                db.execute("SELECT * FROM learning_jobs WHERE id=?", (row["id"],)).fetchone()
            )

    def renew(self, job: dict, *, ttl_seconds: float | None = None) -> bool:
        instant = time.time()
        ttl = 90.0 if ttl_seconds is None else ttl_seconds
        with self._db() as db:
            return (
                db.execute(
                    "UPDATE learning_jobs SET expires_at=? WHERE id=? AND persona_id=? "
                    "AND token=? AND status='running' AND expires_at>?",
                    (instant + ttl, job["id"], self.persona_id, job["token"], instant),
                ).rowcount
                == 1
            )

    def finish_stage(
        self,
        job: dict,
        *,
        stage: str | None = None,
        payload: dict | None = None,
        status: str = "queued",
        error: str = "",
        delay_seconds: float = 0,
        failed_attempt: bool = False,
        now: float | None = None,
    ) -> dict:
        instant = time.time() if now is None else now
        failures = int(job["failures"]) + int(failed_attempt)
        if failed_attempt and failures >= 3:
            status = "failed"
        if status not in {"queued", "deferred", "retry", "completed", "failed"}:
            raise ValueError("Invalid queue transition")
        encoded = self._payload(payload if payload is not None else job["payload"])
        with self._db() as db:
            changed = db.execute(
                "UPDATE learning_jobs SET stage=?,payload=?,status=?,failures=?,"
                "last_error=?,available_at=?,token=NULL,expires_at=NULL,updated_at=? "
                "WHERE id=? AND persona_id=? AND token=? AND status='running' AND expires_at>?",
                (
                    stage or job["stage"],
                    encoded,
                    status,
                    failures,
                    error[:500],
                    instant + delay_seconds,
                    instant,
                    job["id"],
                    self.persona_id,
                    job["token"],
                    instant,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError("Learning job claim lost; refusing stale checkpoint")
            return self._row(
                db.execute("SELECT * FROM learning_jobs WHERE id=?", (job["id"],)).fetchone()
            )


def enqueue(
    service,
    kind: str,
    *,
    source_key: str,
    payload: dict | None = None,
    available_at: float | None = None,
) -> dict | None:
    """Single trigger entrypoint, respecting the current persona's learning disable."""
    if not service.enabled():
        return None
    return LearningQueue(service).enqueue(
        kind, source_key, payload=payload, available_at=available_at
    )


def is_learning_source(experience: dict) -> bool:
    """Host-observed paper work is practice with external evidence, not model rehearsal."""
    metadata = experience.get("metadata", {})
    if metadata.get("learning_role"):
        return False
    return experience.get("mode") in {"real", "study", "backfill"} or (
        experience.get("mode") == "practice"
        and experience.get("surface") == "crypto_paper"
        and metadata.get("practice_origin") == "host_observed"
        and bool(metadata.get("source_receipt_id"))
    )


def is_observed_paper_outcome(experience: dict, observation: dict) -> bool:
    evidence = observation.get("evidence", {})
    return (
        is_learning_source(experience)
        and experience.get("mode") == "practice"
        and observation.get("status") == "resolved"
        and observation.get("quality") == "direct"
        and isinstance(evidence, dict)
        and evidence.get("simulated") is True
        and evidence.get("call_id") == experience.get("metadata", {}).get("source_receipt_id")
    )


def enqueue_observation_learning(service, observation: dict) -> None:
    """Reassess methods implicated by corrected evidence or actual later use."""
    used = set()
    for context in service.store.all("context"):
        if (
            context.get("experience_id") == observation["experience_id"]
            and context.get("phase", "executed") == "executed"
            and context.get("status") == "delivered"
        ):
            used.update(item.get("activation_id") for item in context.get("included", []))
    affected = 0
    for activation in service.store.all("activation"):
        if activation.get("status") not in {"active_provisional", "active_supported"}:
            continue
        candidate = service.get_record(activation.get("candidate_id", "")) or {}
        corrected = observation.get("supersedes") and observation["supersedes"] in (
            candidate.get("evidence_ids", []) + candidate.get("counterevidence_ids", [])
        )
        if corrected or activation["id"] in used:
            enqueue(
                service,
                "regression",
                source_key=f"{activation['id']}:{observation['id']}",
                payload={
                    "candidate_id": candidate["id"],
                    "activation_id": activation["id"],
                    "experience_id": observation["experience_id"],
                    "observation_id": observation["id"],
                },
            )
            affected += 1
    already_routed = any(
        job["payload"].get("observation_id") == observation["id"]
        for job in LearningQueue(service).list(include_finished=True)
    )
    if not affected and not already_routed:
        enqueue(
            service,
            "correction" if observation.get("supersedes") else "experience",
            source_key=observation["id"],
            payload={
                "experience_id": observation["experience_id"],
                "observation_id": observation["id"],
            },
        )


def notify_record(service, record: dict) -> None:
    """Record-persistence notification; no model calls and no process launches."""
    kind = record.get("kind")
    if kind == "expectation":
        try:
            instant = datetime.fromisoformat(record.get("check_by", "").replace("Z", "+00:00"))
            due = instant.timestamp() if instant.tzinfo is not None else None
        except (ValueError, TypeError):
            due = None
        if due is not None:
            enqueue(
                service,
                "observation",
                source_key=record["id"],
                payload={"expectation_id": record["id"], "experience_id": record["experience_id"]},
                available_at=due,
            )
    elif kind in {"execution", "observation"}:
        experience = service.get_record(record.get("experience_id", "")) or {}
        if not is_learning_source(experience):
            return
        if (
            kind == "observation"
            and experience.get("mode") == "practice"
            and not is_observed_paper_outcome(experience, record)
        ):
            return
        if kind == "execution" and record.get("model") and record.get("provider"):
            for activation_id in record.get("included_activation_ids", []):
                activation = service.get_record(activation_id) or {}
                candidate_id = activation.get("candidate_id")
                if not candidate_id or activation.get("status") not in {
                    "active_provisional",
                    "active_supported",
                }:
                    continue
                qualified = any(
                    e.get("candidate_id") == candidate_id
                    and e.get("passed") is True
                    and e.get("model") == record["model"]
                    and e.get("provider") == record["provider"]
                    for e in service.store.all("evaluation")
                )
                if not qualified:
                    enqueue(
                        service,
                        "requalification",
                        source_key=f"{activation_id}:{record['provider']}:{record['model']}",
                        payload={
                            "candidate_id": candidate_id,
                            "activation_id": activation_id,
                            "experience_id": experience["id"],
                            "target_runtime": {
                                key: record.get(key)
                                for key in ("model", "provider", "runtime_lane")
                            },
                        },
                    )
        # Host-observed paper application proves method/model use, so it may
        # trigger requalification above. It is not a settled market outcome and
        # must not create a new source-learning job until an observer resolves it.
        if kind == "execution" and experience.get("mode") == "practice":
            return
        if kind == "observation" and record.get("status") not in {"resolved", "partial"}:
            return
        if kind == "observation":
            enqueue_observation_learning(service, record)
            return
        if (
            kind == "execution"
            and record.get("expectation_id")
            and record.get("stage") == "outbound_observed"
        ):
            enqueue(
                service,
                "observation",
                source_key=f"execution:{record['id']}",
                payload={
                    "experience_id": experience["id"],
                    "expectation_id": record["expectation_id"],
                },
            )
        enqueue(
            service,
            "experience",
            source_key=record["id"],
            payload={"experience_id": experience["id"]},
        )
    elif kind == "candidate" and not record.get("worker_job_id"):
        enqueue(
            service, "candidate", source_key=record["id"], payload={"candidate_id": record["id"]}
        )
