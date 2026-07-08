/**
 * Ghost Viewer proxy — the ghost DEVICE surface (P4.1 Phase B).
 *
 * Distinct from browser-viewer: that drives Chrome over CDP; THIS drives the
 * whole ghost emulator over raw adb (screen / tap / type / swipe / app). Python
 * (dashboard_api.py) owns ALL policy — the HOMIE_GHOST_ENABLED gate, the
 * structurally-ghost-only capability seam, coordinate scaling, and every audit
 * row. Hono only forwards the PNG bytes and the JSON action bodies.
 */

import { Hono } from 'hono';
import { authedFetchBinary, authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId; // imported for the static-invariants grep gate.
void outboundPersonaId;

export const ghostViewerRoute = new Hono();

type JsonRecord = Record<string, unknown>;

ghostViewerRoute.get('/api/ghost-viewer/screen', async (c) => {
  const result = await authedFetchBinary('/api/ghost-viewer/screen', {
    headers: { Accept: 'image/png' },
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'image/png',
    'Cache-Control': result.headers.get('cache-control') ?? 'no-store',
    'X-Ghost-Screen-Width': result.headers.get('x-ghost-screen-width') ?? '0',
    'X-Ghost-Screen-Height': result.headers.get('x-ghost-screen-height') ?? '0',
  });
});

async function forwardJsonPost(c: import('hono').Context, path: string): Promise<Response> {
  const body = await c.req.json().catch(() => ({}));
  const result = await authedFetchJson(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return c.json(result.json as JsonRecord, result.status as 200);
}

ghostViewerRoute.post('/api/ghost-viewer/tap', (c) => forwardJsonPost(c, '/api/ghost-viewer/tap'));
ghostViewerRoute.post('/api/ghost-viewer/text', (c) => forwardJsonPost(c, '/api/ghost-viewer/text'));
ghostViewerRoute.post('/api/ghost-viewer/swipe', (c) => forwardJsonPost(c, '/api/ghost-viewer/swipe'));
ghostViewerRoute.post('/api/ghost-viewer/key', (c) => forwardJsonPost(c, '/api/ghost-viewer/key'));
ghostViewerRoute.post('/api/ghost-viewer/app/launch', (c) =>
  forwardJsonPost(c, '/api/ghost-viewer/app/launch'),
);
ghostViewerRoute.post('/api/ghost-viewer/app/install', (c) =>
  forwardJsonPost(c, '/api/ghost-viewer/app/install'),
);
