-- Source candidate only. Integrate and generate inspected Drizzle migrations
-- through the installed Sites starter before deploying. Never run at request time.
PRAGMA foreign_keys = ON;
CREATE TABLE projects (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, name TEXT NOT NULL,
 brief TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
 mutation_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(id, owner_id)
);
CREATE INDEX idx_projects_owner_updated ON projects(owner_id, updated_at DESC);
CREATE TABLE revisions (
 project_id TEXT NOT NULL, owner_id TEXT NOT NULL, revision INTEGER NOT NULL,
 brief TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(project_id, revision),
 FOREIGN KEY(project_id, owner_id) REFERENCES projects(id, owner_id)
);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY, project_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('website','geo')),
 revision INTEGER NOT NULL, brief TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','ready_for_review','failed')),
 claim_token TEXT NOT NULL, expires_at TEXT NOT NULL,
 error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(id, owner_id),
 FOREIGN KEY(project_id, owner_id) REFERENCES projects(id, owner_id)
);
CREATE INDEX idx_jobs_owner_project ON jobs(owner_id, project_id, created_at DESC);
CREATE TABLE artifacts (
 id TEXT PRIMARY KEY, job_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 filename TEXT NOT NULL, media_type TEXT NOT NULL, object_key TEXT NOT NULL UNIQUE,
 sha256 TEXT NOT NULL CHECK(length(sha256)=64), size_bytes INTEGER NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(id, owner_id),
 FOREIGN KEY(job_id, owner_id) REFERENCES jobs(id, owner_id)
);
CREATE INDEX idx_artifacts_owner_job ON artifacts(owner_id, job_id);
CREATE TABLE reviews (
 id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, owner_id TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('approved','changes_requested')),
 sha256 TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
 FOREIGN KEY(artifact_id, owner_id) REFERENCES artifacts(id, owner_id)
);
CREATE INDEX idx_reviews_owner_artifact ON reviews(owner_id, artifact_id, created_at DESC);
CREATE TRIGGER revisions_immutable BEFORE UPDATE ON revisions BEGIN
 SELECT RAISE(ABORT, 'immutable revision'); END;
CREATE TRIGGER job_inputs_immutable BEFORE UPDATE OF project_id,owner_id,kind,revision,brief,claim_token,expires_at ON jobs BEGIN
 SELECT RAISE(ABORT, 'immutable job inputs'); END;
CREATE TRIGGER artifacts_immutable BEFORE UPDATE ON artifacts BEGIN
 SELECT RAISE(ABORT, 'immutable artifact'); END;
CREATE TRIGGER reviews_immutable BEFORE UPDATE ON reviews BEGIN
 SELECT RAISE(ABORT, 'immutable review'); END;
