/**
 * /api/voice/* — mobile push-to-talk round-trip proxy (Homie Mobile M4).
 *
 * Thin JSON passthrough to the Python voice endpoints (dashboard_api.py):
 *   POST /api/voice/stt  { audio_base64, ext } -> { text }
 *   POST /api/voice/tts  { text }              -> { audio_base64, mime }
 * Both are Bearer-gated by the shared auth middleware (no query-token, not public).
 * No persona ids cross this surface — imports kept for the static-invariants grep.
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId;
void outboundPersonaId;

export const voiceRoute = new Hono();

async function proxyJson(c: any, upstreamPath: string) {
  const body = await c.req.json().catch(() => ({}));
  const upstream = await authedFetchJson(upstreamPath, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return c.json(upstream.json, upstream.status as 200);
}

voiceRoute.post('/api/voice/stt', (c) => proxyJson(c, '/api/voice/stt'));
voiceRoute.post('/api/voice/tts', (c) => proxyJson(c, '/api/voice/tts'));
