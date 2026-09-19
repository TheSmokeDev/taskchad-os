"""Explicit, backed-up reconciliation of split learning stores.

No startup migration guesses a winning checkout. Operators supply all source
LearningTargets and the canonical destination after quiescing their writers.
Only learning records, events, settings, queue checkpoints and local evidence
files are copied. Original stores and absolute evidence references stay intact;
no memories, installed methods, domain databases or live leases are transplanted.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from .models import LearningError, LearningTarget, canonical_json
from .queue import LearningQueue
from .store import LearningStore


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _read_database(path: Path, identity_table: str, persona_id: str) -> dict:
    if not path.exists():
        return {"version": None, "tables": {}, "active_claims": 0}
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise LearningError("corrupt learning migration source")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        versions = {1} if identity_table == "queue_identity" else {1, 2, 3}
        if version not in versions:
            raise LearningError("unsupported learning migration schema")
        owner = db.execute(f"SELECT persona_id FROM {identity_table} WHERE id=1").fetchone()
        if owner is None or owner[0] != persona_id:
            raise LearningError("learning migration ownership conflict")
        tables = {}
        names = (
            ["learning_jobs"]
            if identity_table == "queue_identity"
            else ["records", "events", "settings", "claims"]
        )
        for name in names:
            tables[name] = [dict(row) for row in db.execute(f"SELECT * FROM {name}")]
        if identity_table == "queue_identity":
            active = sum(
                row.get("token") is not None and (row.get("expires_at") or 0) > time.time()
                for row in tables["learning_jobs"]
            )
            if any(row["persona_id"] != persona_id for row in tables["learning_jobs"]):
                raise LearningError("learning migration contains foreign queue jobs")
        else:
            active = sum(row["expires_at"] > time.time() for row in tables["claims"])
        return {"version": version, "tables": tables, "active_claims": active}


def _snapshot(target: LearningTarget) -> dict:
    store = LearningStore(target)
    store._check_path()
    queue = LearningQueue(SimpleNamespace(target=target))
    queue._check_path()
    records = _read_database(store.path, "identity", target.persona_id)
    jobs = _read_database(queue.path, "queue_identity", target.persona_id)
    files = {}
    if store.directory.exists():
        for path in store.directory.rglob("*"):
            if path.is_symlink() or (getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
                raise LearningError("learning migration cannot follow evidence links")
            if not path.is_file():
                continue
            if path.suffix in {".db", ".sqlite3"} or path.name.endswith(
                ("-wal", "-shm", "-journal")
            ):
                continue
            relative = path.relative_to(store.directory).as_posix()
            files[relative] = {
                "path": path,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    return {"target": target, "store": records, "queue": jobs, "files": files}


def inventory_learning_store(target: LearningTarget) -> dict:
    """Read-only inventory: no directory, schema or queue initialization."""
    snapshot = _snapshot(target)
    tables = snapshot["store"]["tables"]
    jobs = snapshot["queue"]["tables"].get("learning_jobs", [])
    return {
        "persona_id": target.persona_id,
        "data_dir": str(target.data_dir),
        "schema_version": snapshot["store"]["version"],
        "records": len(tables.get("records", [])),
        "events": len(tables.get("events", [])),
        "queue_jobs": len(jobs),
        "evidence_files": len(snapshot["files"]),
        "active_claims": snapshot["store"]["active_claims"] + snapshot["queue"]["active_claims"],
    }


def _immutable(row: dict) -> dict:
    return {key: value for key, value in row.items() if key != "seq"}


def _queue_payload(row: dict) -> dict:
    # Tokens are process-specific. An expired in-flight stage resumes its saved
    # checkpoint in the destination; it never inherits the previous lease.
    result = dict(row)
    result["token"] = None
    result["expires_at"] = None
    if result["status"] in {"running", "claimed"}:
        result["status"] = "queued"
    return result


def reconcile_learning_stores(
    destination: LearningTarget,
    sources: list[LearningTarget],
    *,
    backup_dir: Path | None = None,
    apply: bool = False,
    quiesced: bool = False,
) -> dict:
    """Reconcile identical/disjoint content; refuse ambiguity before mutation.

    Default is a dry run. Applying requires explicitly quiesced writers and a
    backup directory. Active leases are a hard refusal even with quiesced=True.
    Every input SQLite file receives an online backup before any target write.
    Existing destination queues may progress after migration: stored source-row
    fingerprints make replay of unchanged source rows idempotent without rewinds.
    """
    if any(source.persona_id != destination.persona_id for source in sources):
        raise LearningError("cannot reconcile different learning personas")
    unique = []
    seen = {Path(destination.data_dir).resolve()}
    for source in sources:
        path = Path(source.data_dir).resolve()
        if path not in seen:
            unique.append(source)
            seen.add(path)
    snapshots = [_snapshot(target) for target in [destination, *unique]]
    base = snapshots[0]
    if apply and (not quiesced or backup_dir is None):
        raise LearningError("apply requires quiesced writers and an explicit backup directory")
    active = sum(s["store"]["active_claims"] + s["queue"]["active_claims"] for s in snapshots)
    if apply and active:
        raise LearningError("learning migration refused while active claims exist")

    records = {row["id"]: _immutable(row) for row in base["store"]["tables"].get("records", [])}
    events = {row["id"]: _immutable(row) for row in base["store"]["tables"].get("events", [])}
    settings = {row["name"]: row["value"] for row in base["store"]["tables"].get("settings", [])}
    jobs = {row["id"]: row for row in base["queue"]["tables"].get("learning_jobs", [])}
    files = dict(base["files"])
    additions = {"records": [], "events": [], "settings": {}, "jobs": [], "files": {}}
    record_keys = {(r["kind"], r["source_key"]): r["id"] for r in records.values()}
    event_keys = {(r["record_id"], r["source_key"]): r["id"] for r in events.values()}
    for source in snapshots[1:]:
        origin = _digest(str(Path(source["target"].data_dir).resolve()))
        for table, registry, keys, key_fields in (
            ("records", records, record_keys, ("kind", "source_key")),
            ("events", events, event_keys, ("record_id", "source_key")),
        ):
            for original in source["store"]["tables"].get(table, []):
                row = _immutable(original)
                row_id = row["id"]
                key = tuple(row[field] for field in key_fields)
                if row_id in registry:
                    if registry[row_id] != row:
                        raise LearningError(f"conflicting learning {table} id: {row_id}")
                    continue
                if key in keys:
                    raise LearningError(f"conflicting learning {table} source key")
                registry[row_id] = row
                keys[key] = row_id
                additions[table].append(row)
        for row in source["store"]["tables"].get("settings", []):
            name, value = row["name"], row["value"]
            if name in settings and settings[name] != value:
                raise LearningError(f"conflicting learning setting: {name}")
            if name not in settings:
                settings[name] = value
                additions["settings"][name] = value
        for original in source["queue"]["tables"].get("learning_jobs", []):
            row = _queue_payload(original)
            mark = f"reconciled_queue:{origin}:{row['id']}"
            fingerprint = canonical_json(_digest(row))
            if mark in settings and settings[mark] == fingerprint:
                continue
            if row["id"] in jobs and _queue_payload(jobs[row["id"]]) != row:
                raise LearningError(f"conflicting learning queue id: {row['id']}")
            if row["id"] not in jobs:
                jobs[row["id"]] = row
                additions["jobs"].append(row)
            additions["settings"][mark] = fingerprint
            settings[mark] = fingerprint
        for name, evidence in source["files"].items():
            if name in files and files[name]["sha256"] != evidence["sha256"]:
                raise LearningError(f"conflicting learning evidence file: {name}")
            if name not in files:
                files[name] = evidence
                additions["files"][name] = evidence
    latest_events = {}
    for event in base["store"]["tables"].get("events", []):
        latest_events[event["record_id"]] = max(
            latest_events.get(event["record_id"], ""), event["created_at"]
        )
    # Projection uses append order. Never append an older transition after a
    # newer transition already in the canonical store and silently rewind it.
    for event in additions["events"]:
        if event["created_at"] <= latest_events.get(event["record_id"], ""):
            raise LearningError("conflicting learning event order; explicit review required")
    additions["events"].sort(key=lambda event: (event["created_at"], event["id"]))
    if any(event["record_id"] not in records for event in events.values()):
        raise LearningError("learning migration contains orphan event references")
    reference_fields = {
        "experience_id",
        "expectation_id",
        "observation_id",
        "candidate_id",
        "evaluation_id",
        "activation_id",
        "cycle_id",
        "investigation_id",
        "prior_candidate_id",
        "prior_activation_id",
        "predecessor_id",
    }
    for record in records.values():
        payload = json.loads(record["payload"])
        for key, value in payload.items():
            references = (
                value
                if key in {"evidence_ids", "counterevidence_ids"}
                else ([value] if key in reference_fields and value else [])
            )
            if any(reference not in records for reference in references):
                raise LearningError(f"learning migration contains orphan record reference: {key}")
    # Validate queue identity and payload, and refuse obvious dangling references.
    queue = LearningQueue(SimpleNamespace(target=destination))
    for row in jobs.values():
        decoded = queue._row(row)
        for key, value in decoded["payload"].items():
            if (
                key
                in {
                    "experience_id",
                    "expectation_id",
                    "observation_id",
                    "candidate_id",
                    "activation_id",
                    "cycle_id",
                    "investigation_id",
                }
                and value
                and value not in records
            ):
                raise LearningError(f"learning migration contains orphan queue reference: {key}")
    result = {
        "applied": False,
        "persona_id": destination.persona_id,
        "destination": str(destination.data_dir),
        "active_claims": active,
        "sources": [str(target.data_dir) for target in unique],
        "added": {key: len(value) for key, value in additions.items()},
        "backups": [],
        "source_files_retained": True,
        "installed_methods_not_moved": True,
    }
    if not apply:
        return result

    backup_root = Path(backup_dir).expanduser().resolve() / f"learning-reconcile-{uuid4().hex}"
    backup_root.mkdir(parents=True, exist_ok=False)
    for index, snapshot in enumerate(snapshots):
        folder = backup_root / str(index)
        folder.mkdir()
        data = Path(snapshot["target"].data_dir) / "learning"
        for name in ("learning.db", "queue.db"):
            path = data / name
            if path.exists():
                with closing(
                    sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
                ) as src:
                    with closing(sqlite3.connect(folder / name)) as dst:
                        src.backup(dst)
        for name, evidence in snapshot["files"].items():
            path = folder / "evidence" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(evidence["path"], path)
        result["backups"].append(str(folder))
    (backup_root / "inventory.json").write_text(canonical_json(result), encoding="utf-8")

    # The caller promises quiescence, and we verify it remained true throughout
    # backup. Refuse drift before initializing or changing the destination.
    def signature(snapshot):
        return _digest(
            {
                "store": snapshot["store"]["tables"],
                "queue": snapshot["queue"]["tables"],
                "files": {name: value["sha256"] for name, value in snapshot["files"].items()},
            }
        )

    for previous in snapshots:
        if signature(previous) != signature(_snapshot(previous["target"])):
            raise LearningError("learning source changed during backup; quiesce writers and retry")

    store = LearningStore(destination)
    # Initialize validated schemas before attaching; both updates below then share
    # one rollback-journal SQLite transaction. No lease rows are copied.
    with store.connection(write=True):
        pass
    with queue._db():
        pass
    with closing(sqlite3.connect(store.path, timeout=10)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("ATTACH DATABASE ? AS queue_db", (str(queue.path),))
        db.execute("BEGIN IMMEDIATE")
        try:
            for table in ("records", "events"):
                for row in additions[table]:
                    columns = list(row)
                    db.execute(
                        f"INSERT INTO {table} ({','.join(columns)}) "
                        f"VALUES ({','.join('?' for _ in columns)})",
                        tuple(row[column] for column in columns),
                    )
            for name, value in additions["settings"].items():
                db.execute(
                    "INSERT INTO settings(name,value) VALUES(?,?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (name, value),
                )
            for row in additions["jobs"]:
                columns = list(row)
                db.execute(
                    f"INSERT INTO queue_db.learning_jobs ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    tuple(row[column] for column in columns),
                )
            for name, evidence in additions["files"].items():
                target = store.directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if hashlib.sha256(target.read_bytes()).hexdigest() != evidence["sha256"]:
                        raise LearningError("evidence changed during reconciliation")
                else:
                    with evidence["path"].open("rb") as src, target.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    result["applied"] = True
    (backup_root / "result.json").write_text(canonical_json(result), encoding="utf-8")
    return result
