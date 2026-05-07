/**
 * /api/health route — public, no auth (auth middleware exempts this path).
 *
 * Forwards to Python /api/health and returns the response body.
 * No persona ids touched, no translation needed.
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

// Translate imports are intentional — every route module imports both
// translation helpers to keep the static-invariants grep gate green
// (even modules that don't touch persona ids; the import surface is the
// invariant, not the runtime usage).
void inboundPersonaId;
void outboundPersonaId;

export const healthRoute = new Hono();

healthRoute.get('/api/health', async (c) => {
  const result = await authedFetchJson('/api/health');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});

healthRoute.get('/api/info', async (c) => {
  const result = await authedFetchJson('/api/info');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});
