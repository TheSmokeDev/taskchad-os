/** Build + Grow is a thin authenticated proxy; Python owns all project work. */
import { Hono, type Context } from 'hono';
import { authedFetchBinary, authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

// This slice has no persona identifiers to translate. Keep the shared route
// boundary explicit, as required by the dashboard's existing invariants.
void inboundPersonaId;
void outboundPersonaId;

export const buildGrowRoute = new Hono();

function callerToken(c: Context): string {
  // An empty override is intentional in loopback development: never silently
  // replace the caller's identity with the dashboard operator's boot token.
  const authorization = c.req.header('authorization') ?? '';
  return /^bearer /i.test(authorization) ? authorization.slice(7).trim() : '';
}

async function proxyJson(c: Context, path: string, method = 'GET') {
  const result = await authedFetchJson(path, {
    method,
    token: callerToken(c),
    ...(method !== 'GET' ? {
      headers: { 'content-type': 'application/json' },
      body: await c.req.text() || '{}',
    } : {}),
  });
  return c.json(result.json as Record<string, unknown>, result.status as 200);
}

buildGrowRoute.get('/api/build-grow/projects', (c) => proxyJson(c, '/api/build-grow/projects'));
buildGrowRoute.post('/api/build-grow/projects', (c) => proxyJson(c, '/api/build-grow/projects', 'POST'));
buildGrowRoute.get('/api/build-grow/projects/:id', (c) =>
  proxyJson(c, `/api/build-grow/projects/${encodeURIComponent(c.req.param('id'))}`));
buildGrowRoute.put('/api/build-grow/projects/:id/brief', (c) =>
  proxyJson(c, `/api/build-grow/projects/${encodeURIComponent(c.req.param('id'))}/brief`, 'PUT'));
buildGrowRoute.get('/api/build-grow/projects/:id/jobs', (c) =>
  proxyJson(c, `/api/build-grow/projects/${encodeURIComponent(c.req.param('id'))}/jobs`));
buildGrowRoute.post('/api/build-grow/projects/:id/jobs', (c) =>
  proxyJson(c, `/api/build-grow/projects/${encodeURIComponent(c.req.param('id'))}/jobs`, 'POST'));
buildGrowRoute.get('/api/build-grow/jobs/:id', (c) =>
  proxyJson(c, `/api/build-grow/jobs/${encodeURIComponent(c.req.param('id'))}`));
buildGrowRoute.post('/api/build-grow/jobs/:id/run', (c) =>
  proxyJson(c, `/api/build-grow/jobs/${encodeURIComponent(c.req.param('id'))}/run`, 'POST'));
buildGrowRoute.post('/api/build-grow/deliverables/:id/review', (c) =>
  proxyJson(c, `/api/build-grow/deliverables/${encodeURIComponent(c.req.param('id'))}/review`, 'POST'));
buildGrowRoute.get('/api/build-grow/deliverables/:id/download', async (c) => {
  const result = await authedFetchBinary(
    `/api/build-grow/deliverables/${encodeURIComponent(c.req.param('id'))}/download`,
    { token: callerToken(c) },
  );
  for (const header of ['content-type', 'content-disposition', 'etag', 'x-content-type-options']) {
    const value = result.headers.get(header);
    if (value) c.header(header, value);
  }
  c.header('Cache-Control', 'no-store');
  return c.body(result.body, result.status as 200);
});
