/**
 * /api/dashboard/settings — dashboard settings and read-only operator status.
 */

import { Hono } from 'hono';
import { authedFetch, authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId;
void outboundPersonaId;

export const settingsRoute = new Hono();

settingsRoute.get('/api/dashboard/mobile-access', async (c) => {
  const result = await authedFetchJson('/api/dashboard/mobile-access', {
    headers: {
      'X-Dashboard-Request-Host': c.req.header('host') ?? '',
    },
  });
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

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

settingsRoute.get('/api/autostart', async (c) => {
  const result = await authedFetchJson('/api/autostart');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

settingsRoute.post('/api/autostart', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const result = await authedFetch('/api/autostart', {
    method: 'POST',
    body: JSON.stringify(body),
    headers: { 'Content-Type': 'application/json' },
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
});

// Python owns the admin gate: /api/audit-log is exempt from the
// orchestration-token middleware and DASHBOARD_ADMIN_TOKEN bearer is the
// SOLE auth path (fail-closed 503 when unset). Forward the incoming
// operator bearer verbatim — Hono never substitutes its own framework
// token on this path.
settingsRoute.get('/api/audit-log', async (c) => {
  const auth = c.req.header('Authorization') ?? '';
  const token = auth.startsWith('Bearer ') ? auth.slice('Bearer '.length) : undefined;
  const query = new URL(c.req.url).search;
  const result = await authedFetchJson(`/api/audit-log${query}`, { token });
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});
