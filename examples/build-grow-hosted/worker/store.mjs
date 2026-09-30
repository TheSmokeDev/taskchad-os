import { normalizeBrief, normalizeSourceFiles, buildArtifacts, sha256 } from './artifacts.mjs';

export class Problem extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
export function text(value, label, max, required = false) {
  if (typeof value !== 'string' || value.trim().length > max ||
      (required && !value.trim()) || /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value))
    throw new Problem(400, `${label} must contain ${required ? 1 : 0}–${max} characters`);
  return value.trim();
}
function id(value) { return text(value, 'id', 100, true); }
function revision(value) {
  if (!Number.isSafeInteger(value) || value < 1) throw new Problem(400, 'expected_revision must be a positive integer');
  return value;
}
const now = () => new Date().toISOString();
const uuid = () => crypto.randomUUID();
const parsedProject = row => ({ id: row.id, name: row.name, brief: JSON.parse(row.brief),
  brief_revision: row.revision, created_at: row.created_at, updated_at: row.updated_at });
const changed = result => Number(result?.meta?.changes || 0);
function reviewMetadata(row) {
  const { review_id, review_decision, review_note, review_created_at, ...rest } = row;
  return { ...rest, latest_review: review_id ? { id: review_id, decision: review_decision,
    note: review_note, created_at: review_created_at, sha256: row.sha256 } : null };
}

// Identity comes exclusively from Sites dispatch. All queries bind it, including
// reads by globally unique IDs. No client-selected workspace or owner is accepted.
export class Store {
  constructor(env, owner) {
    if (!owner) throw new Problem(401, 'Sign in with ChatGPT to access your projects');
    if (!env.DB || !env.BUCKET) throw new Problem(503, 'Project storage is unavailable');
    this.db = env.DB; this.bucket = env.BUCKET; this.owner = owner;
  }
  stmt(sql, ...params) { return this.db.prepare(sql).bind(...params); }
  async project(projectId) {
    const row = await this.stmt('SELECT * FROM projects WHERE id=? AND owner_id=?', id(projectId), this.owner).first();
    if (!row) throw new Problem(404, 'Project not found');
    return row;
  }
  async listProjects() {
    const rows = await this.stmt('SELECT * FROM projects WHERE owner_id=? ORDER BY updated_at DESC LIMIT 100', this.owner).all();
    return { projects: rows.results.map(parsedProject) };
  }
  async getProject(projectId) {
    const row = await this.project(projectId);
    const jobs = await this.stmt('SELECT id,kind,revision,status,expires_at,error_code,created_at,updated_at FROM jobs WHERE project_id=? AND owner_id=? ORDER BY created_at DESC LIMIT 100', row.id, this.owner).all();
    return { ...parsedProject(row), jobs: jobs.results };
  }
  async createProject(args) {
    const brief = JSON.stringify(normalizeBrief(args.brief));
    const name = text(args.name, 'name', 160, true), projectId = uuid(), at = now();
    const result = await this.db.batch([
      this.stmt('INSERT INTO projects(id,owner_id,name,brief,revision,mutation_id,created_at,updated_at) SELECT ?,?,?,?,1,?,?,? WHERE (SELECT count(*) FROM projects WHERE owner_id=?)<100', projectId, this.owner, name, brief, uuid(), at, at, this.owner),
      this.stmt('INSERT INTO revisions SELECT id,owner_id,revision,brief,created_at FROM projects WHERE id=? AND owner_id=?', projectId, this.owner),
    ]);
    if (!changed(result[0])) throw new Problem(409, 'Project limit reached');
    return this.getProject(projectId);
  }
  async updateBrief(args) {
    await this.project(args.project_id);
    const expected = revision(args.expected_revision), brief = JSON.stringify(normalizeBrief(args.brief));
    const mutation = uuid(), at = now();
    const result = await this.db.batch([
      this.stmt('UPDATE projects SET brief=?,revision=revision+1,mutation_id=?,updated_at=? WHERE id=? AND owner_id=? AND revision=?', brief, mutation, at, args.project_id, this.owner, expected),
      this.stmt('INSERT INTO revisions SELECT id,owner_id,revision,brief,updated_at FROM projects WHERE id=? AND owner_id=? AND mutation_id=?', args.project_id, this.owner, mutation),
    ]);
    if (!changed(result[0])) throw new Problem(409, 'Brief changed; reload the current revision');
    return this.getProject(args.project_id);
  }
  async getJob(jobId) {
    const row = await this.stmt('SELECT j.id,j.project_id,j.kind,j.revision,j.status,j.expires_at,j.error_code,j.created_at,j.updated_at,p.revision AS current_brief_revision FROM jobs j JOIN projects p ON p.id=j.project_id AND p.owner_id=j.owner_id WHERE j.id=? AND j.owner_id=?', id(jobId), this.owner).first();
    if (!row) throw new Problem(404, 'Job not found');
    const assets = await this.stmt('SELECT a.id,a.filename,a.media_type,a.sha256,a.size_bytes,a.created_at,r.id AS review_id,r.decision AS review_decision,r.note AS review_note,r.created_at AS review_created_at FROM artifacts a LEFT JOIN reviews r ON r.id=(SELECT id FROM reviews WHERE artifact_id=a.id AND owner_id=a.owner_id ORDER BY rowid DESC LIMIT 1) WHERE a.job_id=? AND a.owner_id=?', row.id, this.owner).all();
    return { ...row, stale: row.revision !== row.current_brief_revision,
      lease_expired: row.status === 'running' && row.expires_at < now(), artifacts: assets.results.map(reviewMetadata) };
  }
  async artifact(artifactId) {
    const row = await this.stmt('SELECT a.*,j.project_id,j.revision,p.revision AS current_brief_revision,r.id AS review_id,r.decision AS review_decision,r.note AS review_note,r.created_at AS review_created_at FROM artifacts a JOIN jobs j ON j.id=a.job_id AND j.owner_id=a.owner_id JOIN projects p ON p.id=j.project_id AND p.owner_id=j.owner_id LEFT JOIN reviews r ON r.id=(SELECT id FROM reviews WHERE artifact_id=a.id AND owner_id=a.owner_id ORDER BY rowid DESC LIMIT 1) WHERE a.id=? AND a.owner_id=? AND j.status=?', id(artifactId), this.owner, 'ready_for_review').first();
    if (!row) throw new Problem(404, 'Artifact not found');
    return { ...reviewMetadata(row), stale: row.revision !== row.current_brief_revision };
  }
  async readArtifact(artifactId) {
    const row = await this.artifact(artifactId);
    const object = await this.bucket.get(row.object_key);
    if (!object) throw new Problem(503, 'Artifact content is unavailable');
    const bytes = new Uint8Array(await object.arrayBuffer());
    if (bytes.byteLength !== row.size_bytes || await sha256(bytes) !== row.sha256)
      throw new Problem(503, 'Artifact integrity check failed');
    return { row, bytes };
  }
  async generate(args, kind) {
    const project = await this.project(args.project_id), expected = revision(args.expected_revision);
    if (project.revision !== expected) throw new Problem(409, 'Brief changed; reload the current revision');
    let sources;
    if (kind === 'geo') sources = normalizeSourceFiles(args.source_files);
    const jobId = uuid(), claim = uuid(), at = now();
    const expires = new Date(Date.now() + 300000).toISOString();
    const inserted = await this.stmt('INSERT INTO jobs(id,project_id,owner_id,kind,revision,brief,status,claim_token,expires_at,created_at,updated_at) SELECT ?,id,owner_id,?,revision,brief,?,?,?,?,? FROM projects WHERE id=? AND owner_id=? AND revision=? AND (SELECT count(*) FROM jobs WHERE owner_id=?)<500', jobId, kind, 'running', claim, expires, at, at, project.id, this.owner, expected, this.owner).run();
    if (!changed(inserted)) throw new Problem(409, 'Brief changed or job limit reached');
    const objects = [];
    try {
      const artifacts = await buildArtifacts(kind, JSON.parse(project.brief), sources);
      if (sources) {
        const content = new TextEncoder().encode(JSON.stringify(sources));
        artifacts.push({ filename: 'input-sources.json', media_type: 'application/json', content,
          sha256: await sha256(content), size_bytes: content.byteLength });
      }
      const rows = [];
      for (const asset of artifacts) {
        const artifactId = uuid(), key = `artifacts/${jobId}/${artifactId}`;
        // Record attempted puts too: a transport error can arrive after R2 wrote.
        objects.push(key);
        await this.bucket.put(key, asset.content, { httpMetadata: { contentType: asset.media_type } });
        rows.push({ ...asset, id: artifactId, key });
      }
      const finished = now();
      const statements = rows.map(asset => this.stmt('INSERT INTO artifacts(id,job_id,owner_id,filename,media_type,object_key,sha256,size_bytes,created_at) SELECT ?,id,owner_id,?,?,?,?,?,? FROM jobs WHERE id=? AND owner_id=? AND status=? AND claim_token=? AND expires_at>?', asset.id, asset.filename, asset.media_type, asset.key, asset.sha256, asset.content.byteLength, finished, jobId, this.owner, 'running', claim, finished));
      statements.push(this.stmt('UPDATE jobs SET status=?,updated_at=? WHERE id=? AND owner_id=? AND status=? AND claim_token=? AND expires_at>?', 'ready_for_review', finished, jobId, this.owner, 'running', claim, finished));
      const results = await this.db.batch(statements);
      if (results.some(result => changed(result) !== 1)) throw new Problem(409, 'Generation expired; create another job');
      return this.getJob(jobId);
    } catch (error) {
      // Never delete committed objects if only the final read-back failed.
      const committed = await this.stmt('SELECT status FROM jobs WHERE id=? AND owner_id=?', jobId, this.owner).first().catch(() => null);
      // An unknown commit outcome must preserve blobs for later reconciliation.
      if (committed && committed.status !== 'ready_for_review') {
        await Promise.allSettled(objects.map(key => this.bucket.delete(key)));
        await this.stmt('UPDATE jobs SET status=?,error_code=?,updated_at=? WHERE id=? AND owner_id=? AND status=? AND claim_token=?', 'failed', 'generation_failed', now(), jobId, this.owner, 'running', claim).run().catch(() => {});
      }
      if (error instanceof Problem) throw error;
      throw new Problem(503, `Generation could not complete; job ${jobId}`);
    }
  }
  async review(args) {
    const asset = await this.artifact(args.artifact_id);
    if (!['approved','changes_requested'].includes(args.decision)) throw new Problem(400, 'Unsupported review decision');
    if (typeof args.expected_sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(args.expected_sha256)) throw new Problem(400, 'expected_sha256 must be a SHA-256 digest');
    const note = text(args.note ?? '', 'note', 4000), reviewId = uuid(), at = now();
    const result = await this.stmt('INSERT INTO reviews(id,artifact_id,owner_id,decision,sha256,note,created_at) SELECT ?,a.id,a.owner_id,?,?,?,? FROM artifacts a JOIN jobs j ON j.id=a.job_id AND j.owner_id=a.owner_id JOIN projects p ON p.id=j.project_id AND p.owner_id=j.owner_id WHERE a.id=? AND a.owner_id=? AND a.sha256=? AND p.revision=j.revision AND j.status=?', reviewId, args.decision, args.expected_sha256, note, at, asset.id, this.owner, args.expected_sha256, 'ready_for_review').run();
    if (!changed(result)) throw new Problem(409, 'Brief or artifact changed; review the current version');
    return { id: reviewId, artifact_id: asset.id, decision: args.decision, sha256: args.expected_sha256, note, created_at: at, publication: 'No website was published' };
  }
}
