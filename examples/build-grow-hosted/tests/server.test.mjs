import test from 'node:test';
import assert from 'node:assert/strict';
import worker, { TOOLS } from '../worker/index.mjs';
import { Store } from '../worker/store.mjs';
import { environment, BRIEF } from './fixtures.mjs';

const request = (method, params, owner, otherHeaders = {}) => new Request('https://service.example/mcp', {
  method: 'POST', headers: { 'Content-Type': 'application/json',
    ...(owner ? { 'oai-authenticated-user-id': owner } : {}), ...otherHeaders },
  body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
});
const call = (name, args, owner, env) => worker.fetch(request('tools/call', { name, arguments: args }, owner), env);

test('anonymous discovery works but data calls require identity, including service access', async () => {
  const env = environment();
  const discover = await worker.fetch(request('tools/list', {}, null), env);
  assert.equal(discover.status, 200);
  assert.equal((await discover.json()).result.tools.length, 9);
  assert.equal((await call('list_projects', {}, null, env)).status, 401);
  const bypassOnly = await worker.fetch(request('tools/call', { name: 'list_projects', arguments: {} }, null,
    { 'OAI-Sites-Authorization': 'Bearer example-service-credential' }), env);
  assert.equal(bypassOnly.status, 401);
  assert.equal(env.DB.sqlite.prepare('SELECT count(*) AS n FROM projects').get().n, 0);
});

test('protocol negotiation is bounded, not advertised as latest', async () => {
  const env = environment();
  const response = await worker.fetch(request('initialize', { protocolVersion: '2026-07-28' }), env);
  assert.equal((await response.json()).result.protocolVersion, '2025-06-18');
  const unsupported = await worker.fetch(request('tools/list', {}, null, { 'mcp-protocol-version': '2026-07-28' }), env);
  assert.equal(unsupported.status, 400);
  assert.equal((await worker.fetch(request('tools/list', {}, null, { Origin: 'https://attacker.example' }), env)).status, 403);
});

test('cross-user project, job, artifact, download, and review IDs reveal no records', async () => {
  const env = environment(), a = new Store(env, 'a'), b = new Store(env, 'b');
  const project = await a.createProject({ name: 'Fixture', brief: BRIEF });
  const job = await a.generate({ project_id: project.id, expected_revision: 1 }, 'website');
  const asset = job.artifacts[0];
  assert.deepEqual((await b.listProjects()).projects, []);
  for (const action of [() => b.getProject(project.id), () => b.getJob(job.id), () => b.artifact(asset.id),
    () => b.readArtifact(asset.id), () => b.review({ artifact_id: asset.id, decision: 'approved', expected_sha256: asset.sha256 })])
    await assert.rejects(action, error => error.status === 404);
  const download = await worker.fetch(new Request(`https://service.example/api/artifacts/${asset.id}/download`,
    { headers: { 'oai-authenticated-user-id': 'b' } }), env);
  assert.equal(download.status, 404);
  assert.equal(env.DB.sqlite.prepare('SELECT count(*) AS n FROM reviews').get().n, 0);
});

test('tool arguments cannot override trusted owner and all annotations are boolean', async () => {
  const env = environment();
  const response = await call('list_projects', { owner_id: 'another-user' }, 'a', env);
  assert.equal((await response.json()).result.isError, true);
  for (const entry of TOOLS) for (const key of ['readOnlyHint','openWorldHint','destructiveHint'])
    assert.equal(typeof entry.annotations[key], 'boolean');
});

test('concurrent brief writers use revision checks and preserve history', async () => {
  const env = environment(), store = new Store(env, 'a');
  const project = await store.createProject({ name: 'Fixture', brief: BRIEF });
  const results = await Promise.allSettled([
    store.updateBrief({ project_id: project.id, expected_revision: 1, brief: { ...BRIEF, description: 'First update' } }),
    store.updateBrief({ project_id: project.id, expected_revision: 1, brief: { ...BRIEF, description: 'Second update' } }),
  ]);
  assert.equal(results.filter(result => result.status === 'fulfilled').length, 1);
  assert.equal(results.find(result => result.status === 'rejected').reason.status, 409);
  assert.equal((await store.getProject(project.id)).brief_revision, 2);
  const rows = env.DB.sqlite.prepare('SELECT * FROM revisions ORDER BY revision').all();
  assert.equal(rows.length, 2); assert.equal(JSON.parse(rows[0].brief).description, BRIEF.description);
});

test('ambiguous R2 write failures leave no published artifact pointers and clean known failed writes', async () => {
  const env = environment(), store = new Store(env, 'a');
  const project = await store.createProject({ name: 'Fixture', brief: BRIEF });
  env.BUCKET.failPut = true;
  await assert.rejects(() => store.generate({ project_id: project.id, expected_revision: 1 }, 'website'), error => error.status === 503);
  assert.equal(env.DB.sqlite.prepare('SELECT status FROM jobs').get().status, 'failed');
  assert.equal(env.DB.sqlite.prepare('SELECT count(*) AS n FROM artifacts').get().n, 0);
  assert.equal(env.BUCKET.objects.size, 0);
});

test('unknown commit outcome preserves potentially committed artifact bytes', async () => {
  const env = environment(), store = new Store(env, 'a');
  const project = await store.createProject({ name: 'Fixture', brief: BRIEF });
  env.DB.afterBatch = async () => { env.DB.failRead = true; throw new Error('Response lost after commit'); };
  await assert.rejects(() => store.generate({ project_id: project.id, expected_revision: 1 }, 'website'), error => error.status === 503);
  env.DB.failRead = false;
  const job = env.DB.sqlite.prepare('SELECT id,status FROM jobs').get();
  assert.equal(job.status, 'ready_for_review');
  assert.ok(env.BUCKET.objects.size > 0);
  const stored = await store.getJob(job.id);
  for (const asset of stored.artifacts) assert.ok((await store.readArtifact(asset.id)).bytes.byteLength > 0);
});

test('stale brief and wrong hash reviews cannot approve other bytes', async () => {
  const env = environment(), store = new Store(env, 'a');
  const project = await store.createProject({ name: 'Fixture', brief: BRIEF });
  const job = await store.generate({ project_id: project.id, expected_revision: 1 }, 'website');
  const asset = job.artifacts[0];
  await assert.rejects(() => store.review({ artifact_id: asset.id, decision: 'approved', expected_sha256: '0'.repeat(64) }), error => error.status === 409);
  const accepted = await store.review({ artifact_id: asset.id, decision: 'approved', expected_sha256: asset.sha256 });
  assert.equal(accepted.publication, 'No website was published');
  const reopened = new Store(env, 'a');
  assert.equal((await reopened.getJob(job.id)).artifacts.find(entry => entry.id === asset.id).latest_review.decision, 'approved');
  assert.equal((await reopened.artifact(asset.id)).latest_review.id, accepted.id);
  const result = await (await call('get_artifact', { artifact_id: asset.id }, 'a', env)).json();
  assert.equal(result.result.structuredContent.latest_review.decision, 'approved');
  assert.equal(result.result.structuredContent.stale, false);
  const changes = await store.review({ artifact_id: asset.id, decision: 'changes_requested', expected_sha256: asset.sha256, note: 'Revise the contact section' });
  assert.equal((await reopened.getJob(job.id)).artifacts.find(entry => entry.id === asset.id).latest_review.id, changes.id);
  assert.equal((await reopened.artifact(asset.id)).latest_review.decision, 'changes_requested');
  await store.updateBrief({ project_id: project.id, expected_revision: 1, brief: { ...BRIEF, description: 'Updated facts' } });
  await assert.rejects(() => store.review({ artifact_id: asset.id, decision: 'approved', expected_sha256: asset.sha256 }), error => error.status === 409);
  assert.equal(env.DB.sqlite.prepare('SELECT count(*) AS n FROM reviews').get().n, 2);
  assert.equal((await reopened.getJob(job.id)).stale, true);
  assert.equal((await reopened.artifact(asset.id)).stale, true);
});

test('authenticated downloads verify exact bytes and reject corruption', async () => {
  const env = environment(), store = new Store(env, 'a');
  const project = await store.createProject({ name: 'Fixture', brief: BRIEF });
  const job = await store.generate({ project_id: project.id, expected_revision: 1 }, 'website');
  const asset = job.artifacts[0];
  const url = `https://service.example/api/artifacts/${asset.id}/download`;
  assert.equal((await worker.fetch(new Request(url), env)).status, 401);
  const response = await worker.fetch(new Request(url, { headers: { 'oai-authenticated-user-id': 'a' } }), env);
  assert.equal(response.headers.get('etag'), `"${asset.sha256}"`);
  assert.equal((await response.arrayBuffer()).byteLength, asset.size_bytes);
  env.BUCKET.corrupt = true;
  assert.equal((await worker.fetch(new Request(url, { headers: { 'oai-authenticated-user-id': 'a' } }), env)).status, 503);
});

test('SQL triggers prevent rewriting frozen job inputs and reviewed artifacts', async () => {
  const env = environment(), store = new Store(env, 'a');
  const project = await store.createProject({ name: 'Fixture', brief: BRIEF });
  await store.generate({ project_id: project.id, expected_revision: 1 }, 'website');
  for (const sql of ["UPDATE jobs SET brief='{}'", "UPDATE artifacts SET sha256='bad'", "UPDATE revisions SET brief='{}'"])
    assert.throws(() => env.DB.sqlite.exec(sql), /immutable/);
});

test('oversized requests fail before tool execution', async () => {
  const env = environment();
  const response = await worker.fetch(new Request('https://service.example/mcp', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: 'x'.repeat(3 * 1024 * 1024 + 1) }), env);
  assert.equal(response.status, 413);
  assert.equal(env.DB.sqlite.prepare('SELECT count(*) AS n FROM projects').get().n, 0);
});
