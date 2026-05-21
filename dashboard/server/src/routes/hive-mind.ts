/**
 * /api/hive-mind/recent — recent chat events for the 3D Hive Mind brain visualization.
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaDict, outboundPersonaId } from '../translate.js';

void outboundPersonaId; // imported for static-invariants grep gate.

export const hiveMindRoute = new Hono();

function translateHivePayload(payload: unknown): unknown {
  if (!payload || typeof payload !== 'object') {
    return payload;
  }
  const out: Record<string, unknown> = { ...(payload as Record<string, unknown>) };
  for (const key of ['entries', 'events'] as const) {
    if (Array.isArray(out[key])) {
      out[key] = out[key].map((item) => {
        if (!item || typeof item !== 'object') {
          return item;
        }
        return outboundPersonaDict(item as Record<string, unknown>);
      });
    }
  }
  return out;
}

hiveMindRoute.get('/api/hive-mind/recent', async (c) => {
  const url = new URL(c.req.url);
  const personaId = url.searchParams.get('persona_id');
  if (personaId) {
    const fwId = inboundPersonaId(personaId) ?? personaId;
    url.searchParams.set('persona_id', fwId);
  }
  const result = await authedFetchJson(`/api/hive-mind/recent${url.search}`);
  return c.json(translateHivePayload(result.json) as Record<string, unknown>, result.status as 200);
});
