"""Durable workspace-scoped storage; every operation owns its SQLite connection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from build_grow.models import Artifact, ConflictError, NotFoundError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
 id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL,
 name TEXT NOT NULL, brief_revision INTEGER NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(id, workspace_id)
);
CREATE TABLE IF NOT EXISTS brief_revisions (
 project_id INTEGER NOT NULL, workspace_id INTEGER NOT NULL, revision INTEGER NOT NULL,
 brief_json TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'owner_provided', created_at TEXT NOT NULL,
 PRIMARY KEY(project_id, revision), UNIQUE(project_id, revision, workspace_id),
 FOREIGN KEY(project_id, workspace_id) REFERENCES projects(id, workspace_id)
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, project_id INTEGER NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('website', 'geo')),
 status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'ready_for_review', 'failed')),
 brief_revision INTEGER NOT NULL, brief_json TEXT NOT NULL, source_job_id INTEGER,
 source_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT,
 UNIQUE(id, workspace_id),
 FOREIGN KEY(project_id, workspace_id) REFERENCES projects(id, workspace_id),
 FOREIGN KEY(project_id, brief_revision, workspace_id)
 REFERENCES brief_revisions(project_id, revision, workspace_id),
 FOREIGN KEY(source_job_id, workspace_id) REFERENCES jobs(id, workspace_id)
);
CREATE TABLE IF NOT EXISTS deliverables (
 id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL, job_id INTEGER NOT NULL,
 filename TEXT NOT NULL, media_type TEXT NOT NULL, sha256 TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, content BLOB NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(id, workspace_id), UNIQUE(job_id, filename),
 FOREIGN KEY(job_id, workspace_id) REFERENCES jobs(id, workspace_id)
);
CREATE TABLE IF NOT EXISTS reviews (
 id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER NOT NULL,
 deliverable_id INTEGER NOT NULL, sha256 TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('approved', 'changes_requested')),
 note TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(deliverable_id, workspace_id) REFERENCES deliverables(id, workspace_id)
);
CREATE INDEX IF NOT EXISTS projects_workspace ON projects(workspace_id, id);
CREATE INDEX IF NOT EXISTS jobs_project ON jobs(workspace_id, project_id, id);
CREATE INDEX IF NOT EXISTS jobs_lease ON jobs(workspace_id, status, updated_at);
CREATE INDEX IF NOT EXISTS deliverables_job ON deliverables(workspace_id, job_id, id);
CREATE INDEX IF NOT EXISTS reviews_deliverable ON reviews(workspace_id, deliverable_id, id);
CREATE TRIGGER IF NOT EXISTS immutable_job_inputs
BEFORE UPDATE OF workspace_id,project_id,kind,brief_revision,brief_json,source_job_id,source_json
ON jobs BEGIN SELECT RAISE(ABORT, 'immutable job inputs'); END;
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class BuildGrowStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            # Review and source history cannot be rewritten, even by accidental SQL.
            for table in ("brief_revisions", "deliverables", "reviews"):
                for action in ("UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} "
                        f"BEFORE {action} ON {table} BEGIN "
                        "SELECT RAISE(ABORT, 'immutable history'); END"
                    )

    @contextmanager
    def _connection(self, *, write: bool = False):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _project_row(conn, project_id: int, workspace_id: int):
        row = conn.execute(
            "SELECT * FROM projects WHERE id=? AND workspace_id=?", (project_id, workspace_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("project not found")
        return row

    @staticmethod
    def _job_row(conn, job_id: int, workspace_id: int):
        row = conn.execute(
            "SELECT * FROM jobs WHERE id=? AND workspace_id=?", (job_id, workspace_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("job not found")
        return row

    @staticmethod
    def _project_payload(conn, row):
        revision = conn.execute(
            "SELECT brief_json FROM brief_revisions "
            "WHERE project_id=? AND revision=? AND workspace_id=?",
            (row["id"], row["brief_revision"], row["workspace_id"]),
        ).fetchone()
        return {
            name: row[name]
            for name in (
                "id",
                "name",
                "brief_revision",
                "created_at",
                "updated_at",
            )
        } | {"brief": json.loads(revision["brief_json"])}

    @staticmethod
    def _deliverable_payload(conn, row):
        latest = conn.execute(
            "SELECT decision FROM reviews WHERE deliverable_id=? AND workspace_id=? "
            "ORDER BY id DESC LIMIT 1",
            (row["id"], row["workspace_id"]),
        ).fetchone()
        return {
            name: row[name] for name in ("id", "filename", "media_type", "sha256", "size_bytes")
        } | {"review_status": latest["decision"] if latest else "pending"}

    def _job_payload(self, conn, row):
        result = {
            name: row[name]
            for name in (
                "id",
                "project_id",
                "kind",
                "status",
                "brief_revision",
                "source_job_id",
                "created_at",
                "updated_at",
                "error",
            )
        }
        result["deliverables"] = [
            self._deliverable_payload(conn, item)
            for item in conn.execute(
                "SELECT id,workspace_id,filename,media_type,sha256,size_bytes FROM deliverables "
                "WHERE job_id=? AND workspace_id=? ORDER BY id",
                (row["id"], row["workspace_id"]),
            )
        ]
        return result

    def create_project(self, name: str, brief: dict, *, workspace_id: int) -> dict:
        now = _now()
        with self._connection(write=True) as conn:
            cur = conn.execute(
                "INSERT INTO projects(workspace_id,name,brief_revision,created_at,updated_at) "
                "VALUES(?,?,1,?,?)",
                (workspace_id, name, now, now),
            )
            project_id = cur.lastrowid
            conn.execute(
                "INSERT INTO brief_revisions"
                "(project_id,workspace_id,revision,brief_json,created_at) "
                "VALUES(?,?,1,?,?)",
                (project_id, workspace_id, _json(brief), now),
            )
            return self._project_payload(conn, self._project_row(conn, project_id, workspace_id))

    def list_projects(self, *, workspace_id: int) -> list[dict]:
        with self._connection() as conn:
            return [
                self._project_payload(conn, row)
                for row in conn.execute(
                    "SELECT * FROM projects WHERE workspace_id=? ORDER BY id DESC LIMIT 100",
                    (workspace_id,),
                )
            ]

    def get_project(self, project_id: int, *, workspace_id: int) -> dict:
        with self._connection() as conn:
            return self._project_payload(conn, self._project_row(conn, project_id, workspace_id))

    def update_brief(
        self, project_id: int, brief: dict, expected_revision: int, *, workspace_id: int
    ) -> dict:
        with self._connection(write=True) as conn:
            row = self._project_row(conn, project_id, workspace_id)
            if row["brief_revision"] != expected_revision:
                raise ConflictError("business brief changed; reload before saving")
            revision, now = expected_revision + 1, _now()
            conn.execute(
                "INSERT INTO brief_revisions"
                "(project_id,workspace_id,revision,brief_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (project_id, workspace_id, revision, _json(brief), now),
            )
            conn.execute(
                "UPDATE projects SET brief_revision=?,updated_at=? WHERE id=? AND workspace_id=?",
                (revision, now, project_id, workspace_id),
            )
            return self._project_payload(conn, self._project_row(conn, project_id, workspace_id))

    def create_job(
        self,
        project_id: int,
        kind: str,
        source_files: dict,
        source_job_id: int | None,
        expected_revision: int,
        *,
        workspace_id: int,
    ) -> dict:
        with self._connection(write=True) as conn:
            project = self._project_payload(conn, self._project_row(conn, project_id, workspace_id))
            if project["brief_revision"] != expected_revision:
                raise ConflictError("business brief changed; reload before creating a job")
            if source_job_id is not None:
                source = self._job_row(conn, source_job_id, workspace_id)
                if source["project_id"] != project_id:
                    raise NotFoundError("source job not found in this project")
                if (
                    source["kind"] != "website"
                    or source["status"] != "ready_for_review"
                    or source["brief_revision"] != expected_revision
                ):
                    raise ConflictError("source website must be ready and use the current brief")
            now = _now()
            cur = conn.execute(
                "INSERT INTO jobs(workspace_id,project_id,kind,status,brief_revision,brief_json,"
                "source_job_id,source_json,created_at,updated_at) "
                "VALUES(?,?,?,'queued',?,?,?,?,?,?)",
                (
                    workspace_id,
                    project_id,
                    kind,
                    expected_revision,
                    _json(project["brief"]),
                    source_job_id,
                    _json(source_files),
                    now,
                    now,
                ),
            )
            return self._job_payload(conn, self._job_row(conn, cur.lastrowid, workspace_id))

    def list_jobs(self, project_id: int, *, workspace_id: int) -> list[dict]:
        self.recover_expired_jobs(workspace_id=workspace_id)
        with self._connection() as conn:
            self._project_row(conn, project_id, workspace_id)
            return [
                self._job_payload(conn, row)
                for row in conn.execute(
                    "SELECT * FROM jobs WHERE project_id=? AND workspace_id=? "
                    "ORDER BY id DESC LIMIT 100",
                    (project_id, workspace_id),
                )
            ]

    def get_job(self, job_id: int, *, workspace_id: int) -> dict:
        self.recover_expired_jobs(workspace_id=workspace_id)
        with self._connection() as conn:
            return self._job_payload(conn, self._job_row(conn, job_id, workspace_id))

    def latest_website_job(
        self, project_id: int, revision: int, *, workspace_id: int
    ) -> dict | None:
        self.recover_expired_jobs(workspace_id=workspace_id)
        with self._connection() as conn:
            self._project_row(conn, project_id, workspace_id)
            row = conn.execute(
                "SELECT * FROM jobs WHERE project_id=? AND workspace_id=? AND brief_revision=? "
                "AND kind='website' AND status='ready_for_review' ORDER BY id DESC LIMIT 1",
                (project_id, workspace_id, revision),
            ).fetchone()
            return self._job_payload(conn, row) if row else None

    def claim_job(self, job_id: int, *, workspace_id: int) -> dict:
        self.recover_expired_jobs(workspace_id=workspace_id)
        with self._connection(write=True) as conn:
            row = self._job_row(conn, job_id, workspace_id)
            if row["status"] != "queued":
                raise ConflictError("only queued jobs can be run; create a new job to try again")
            changed = conn.execute(
                "UPDATE jobs SET status='running',updated_at=? "
                "WHERE id=? AND workspace_id=? AND status='queued'",
                (_now(), job_id, workspace_id),
            ).rowcount
            if changed != 1:
                raise ConflictError("job already claimed")
            return {
                "kind": row["kind"],
                "brief": json.loads(row["brief_json"]),
                "source_files": json.loads(row["source_json"]),
            }

    def finish_job(self, job_id: int, artifacts: list[Artifact], *, workspace_id: int) -> dict:
        self.recover_expired_jobs(workspace_id=workspace_id)
        with self._connection(write=True) as conn:
            row = self._job_row(conn, job_id, workspace_id)
            if row["status"] != "running":
                raise ConflictError("job is not running")
            now = _now()
            for artifact in artifacts:
                conn.execute(
                    "INSERT INTO deliverables(workspace_id,job_id,filename,media_type,sha256,"
                    "size_bytes,content,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        workspace_id,
                        job_id,
                        artifact.filename,
                        artifact.media_type,
                        hashlib.sha256(artifact.content).hexdigest(),
                        len(artifact.content),
                        artifact.content,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE jobs SET status='ready_for_review',updated_at=? "
                "WHERE id=? AND workspace_id=?",
                (now, job_id, workspace_id),
            )
            return self._job_payload(conn, self._job_row(conn, job_id, workspace_id))

    def fail_job(self, job_id: int, *, workspace_id: int) -> dict:
        with self._connection(write=True) as conn:
            row = self._job_row(conn, job_id, workspace_id)
            if row["status"] == "failed":
                return self._job_payload(conn, row)
            if row["status"] != "running":
                raise ConflictError("job is not running")
            conn.execute(
                "UPDATE jobs SET status='failed',error=?,updated_at=? "
                "WHERE id=? AND workspace_id=?",
                ("Generation failed. Create a new job to try again.", _now(), job_id, workspace_id),
            )
            return self._job_payload(conn, self._job_row(conn, job_id, workspace_id))

    def recover_expired_jobs(self, *, workspace_id: int) -> int:
        """Expire this workspace's five-minute worker leases; never retry automatically.

        Recovery happens on scoped job reads/run and on completion, so a restart
        cannot strand a running row and a late worker cannot publish its result.
        Store initialization does not mutate jobs in any workspace.
        """
        cutoff = (
            (datetime.now(UTC) - timedelta(minutes=5))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        with self._connection(write=True) as conn:
            return conn.execute(
                "UPDATE jobs SET status='failed',error=?,updated_at=? "
                "WHERE workspace_id=? AND status='running' AND updated_at < ?",
                (
                    "Worker lease expired. Create a new job to try again.",
                    _now(),
                    workspace_id,
                    cutoff,
                ),
            ).rowcount

    def download_deliverable(self, deliverable_id: int, *, workspace_id: int) -> tuple[dict, bytes]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM deliverables WHERE id=? AND workspace_id=?",
                (deliverable_id, workspace_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("deliverable not found")
            return self._deliverable_payload(conn, row), bytes(row["content"])

    def review_deliverable(
        self,
        deliverable_id: int,
        decision: str,
        expected_sha256: str,
        note: str,
        *,
        workspace_id: int,
    ) -> dict:
        with self._connection(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM deliverables WHERE id=? AND workspace_id=?",
                (deliverable_id, workspace_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("deliverable not found")
            job = self._job_row(conn, row["job_id"], workspace_id)
            project = self._project_row(conn, job["project_id"], workspace_id)
            if row["sha256"] != expected_sha256:
                raise ConflictError(
                    "deliverable hash changed; download and review the exact artifact"
                )
            if project["brief_revision"] != job["brief_revision"]:
                raise ConflictError(
                    "business brief changed; generate a job from the current revision"
                )
            conn.execute(
                "INSERT INTO reviews(workspace_id,deliverable_id,sha256,decision,note,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (workspace_id, deliverable_id, expected_sha256, decision, note, _now()),
            )
            return self._deliverable_payload(conn, row)
