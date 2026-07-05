/**
 * /api/pair/* — QR device pairing proxy (Homie Mobile M2).
 *
 * Thin proxy to the Python pairing surface (pairing_api.py) — zero business
 * logic here per the thin-proxy charter. Two of these paths (`/claim`,
 * `/poll`) are PUBLIC at the auth middleware (the phone has no bearer yet);
 * they self-authenticate with bootstrap/poll secrets validated by Python.
 * The upstream call still rides framework-client's boot-snapshot Bearer.
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

// No persona ids cross this surface — imported for the static-invariants
// grep gate (Q4 translation lock), same as jarvis.ts.
void inboundPersonaId;
void outboundPersonaId;

export const pairRoute = new Hono();

async function proxyPost(c: any, upstreamPath: string) {
  const body = await c.req.json().catch(() => ({}));
  const upstream = await authedFetchJson(upstreamPath, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return c.json(upstream.json, upstream.status as 200);
}

pairRoute.post('/api/pair/start', (c) => proxyPost(c, '/api/pair/start'));
pairRoute.post('/api/pair/claim', (c) => proxyPost(c, '/api/pair/claim'));
pairRoute.post('/api/pair/poll', (c) => proxyPost(c, '/api/pair/poll'));

pairRoute.get('/api/pair/pending', async (c) => {
  const upstream = await authedFetchJson('/api/pair/pending', { method: 'GET' });
  return c.json(upstream.json, upstream.status as 200);
});

pairRoute.post('/api/pair/approve/:pairId', (c) =>
  proxyPost(c, `/api/pair/approve/${encodeURIComponent(c.req.param('pairId'))}`),
);
pairRoute.post('/api/pair/deny/:pairId', (c) =>
  proxyPost(c, `/api/pair/deny/${encodeURIComponent(c.req.param('pairId'))}`),
);
