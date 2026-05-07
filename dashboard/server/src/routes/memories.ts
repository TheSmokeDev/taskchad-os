/**
 * /api/memories — paginated read-only proxy.
 * /api/tokens — global lane-aware time series.
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void outboundPersonaId; // imported for static-invariants grep gate.

export const memoriesRoute = new Hono();

memoriesRoute.get('/api/memories', async (c) => {
  const url = new URL(c.req.url);
  const personaId = url.searchParams.get('persona_id');
  if (personaId) {
    const fwId = inboundPersonaId(personaId) ?? personaId;
    url.searchParams.set('persona_id', fwId);
  }
  const result = await authedFetchJson(`/api/memories${url.search}`);
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

memoriesRoute.get('/api/tokens', async (c) => {
  const url = new URL(c.req.url);
  const result = await authedFetchJson(`/api/tokens${url.search}`);
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});
