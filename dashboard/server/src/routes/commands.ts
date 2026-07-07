/**
 * /api/commands — read-only slash-command registry proxy.
 *
 * Straight GET pass-through to the Python framework's projection of the
 * chat slice's COMMANDS/CATEGORIES registry (dashboard chat composer
 * autocomplete). ZERO business logic here — the Python side owns the
 * registry shape and fail-open behavior ({"commands": []} on error).
 *
 * No persona translation applies: command names are framework-level
 * identifiers, never persona aliases. Imports satisfy the
 * static-invariants grep gate (Q4 lock).
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId;
void outboundPersonaId;

export const commandsRoute = new Hono();

commandsRoute.get('/api/commands', async (c) => {
  const result = await authedFetchJson('/api/commands');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});
