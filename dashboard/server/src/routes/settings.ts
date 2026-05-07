/**
 * /api/dashboard/settings — KV settings store proxy.
 */

import { Hono } from 'hono';
import { authedFetch, authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId;
void outboundPersonaId;

export const settingsRoute = new Hono();

settingsRoute.get('/api/dashboard/settings', async (c) => {
  const result = await authedFetchJson('/api/dashboard/settings');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

settingsRoute.patch('/api/dashboard/settings', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const result = await authedFetch('/api/dashboard/settings', {
    method: 'PATCH',
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});
