/**
 * /api/runtime/status — read-only runtime lane/provider/model projection.
 *
 * Straight GET pass-through to the Python framework's structured view of
 * the current runtime selection (Chat model/provider pill). ZERO business
 * logic here — the Python side owns the shape and the fail-open behavior
 * ({available: false, ...} on any resolver error).
 *
 * Deliberately NO mutation route: switching lanes/models rides the
 * existing gated `/model <target>` chat command through the conversation
 * send path — never a new HTTP mutation surface.
 *
 * No persona translation applies: lane/provider/model identifiers are
 * framework-level, never persona aliases. Imports satisfy the
 * static-invariants grep gate (Q4 lock).
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId;
void outboundPersonaId;

export const runtimeRoute = new Hono();

runtimeRoute.get('/api/runtime/status', async (c) => {
  const result = await authedFetchJson('/api/runtime/status');
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});
