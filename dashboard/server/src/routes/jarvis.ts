/**
 * Jarvis status route - thin proxy to Python /api/jarvis/status.
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId;
void outboundPersonaId;

export const jarvisRoute = new Hono();

jarvisRoute.get('/api/jarvis/status', async (c) => {
  const result = await authedFetchJson('/api/jarvis/status');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});
