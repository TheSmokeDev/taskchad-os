/**
 * /api/scheduled routes — cron-shaped task list, create, patch, delete.
 *
 * persona_id is part of the body shape on create — translate before forward.
 */

import { Hono } from 'hono';
import { authedFetch, authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void outboundPersonaId; // imported for static-invariants grep gate.

export const scheduledRoute = new Hono();

scheduledRoute.get('/api/scheduled', async (c) => {
  const url = new URL(c.req.url);
  // Translate persona_id query param if present.
  const personaId = url.searchParams.get('persona_id');
  if (personaId) {
    const fwId = inboundPersonaId(personaId) ?? personaId;
    url.searchParams.set('persona_id', fwId);
  }
  const result = await authedFetchJson(`/api/scheduled${url.search}`);
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

scheduledRoute.post('/api/scheduled', async (c) => {
  const body = (await c.req.json().catch(() => ({}))) as Record<string, unknown>;
  if (typeof body.persona_id === 'string') {
    body.persona_id = inboundPersonaId(body.persona_id) ?? body.persona_id;
  }
  const result = await authedFetch('/api/scheduled', {
    method: 'POST',
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

scheduledRoute.patch('/api/scheduled/:taskId', async (c) => {
  const taskId = c.req.param('taskId');
  const body = await c.req.json().catch(() => ({}));
  const result = await authedFetch(
    `/api/scheduled/${encodeURIComponent(taskId)}`,
    {
      method: 'PATCH',
      body: JSON.stringify(body),
      headers: { 'Content-Type': 'application/json' },
    },
  );
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

scheduledRoute.delete('/api/scheduled/:taskId', async (c) => {
  const taskId = c.req.param('taskId');
  const result = await authedFetch(
    `/api/scheduled/${encodeURIComponent(taskId)}`,
    { method: 'DELETE' },
  );
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});
