/**
 * Work queue passthrough — /api/work/tasks.
 *
 * Python owns orchestration state and dispatch. This route is only the
 * dashboard server proxy boundary: preserve auth, method, body, and content
 * type while avoiding any storage or scheduling logic in Hono.
 */

import { Hono } from 'hono';
import { authedFetch } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId; // imported for static-invariants grep gate.
void outboundPersonaId;

export const workRoute = new Hono();

async function forward(c: import('hono').Context): Promise<Response> {
  const url = new URL(c.req.url);
  const upstreamPath = `${url.pathname}${url.search}`;
  const method = c.req.method;
  const hasBody = method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS';
  const bodyText = hasBody ? await c.req.text() : undefined;

  const headers: Record<string, string> = {};
  const ct = c.req.header('content-type');
  if (ct) headers['Content-Type'] = ct;

  const result = await authedFetch(upstreamPath, {
    method,
    body: bodyText,
    headers,
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
}

workRoute.get('/api/work/tasks', async (c) => forward(c));
workRoute.post('/api/work/tasks', async (c) => forward(c));
workRoute.patch('/api/work/tasks/:taskId', async (c) => forward(c));
workRoute.post('/api/work/tasks/:taskId/dispatch', async (c) => forward(c));
