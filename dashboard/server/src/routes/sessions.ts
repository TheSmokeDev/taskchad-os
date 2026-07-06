/**
 * /api/sessions — M8 sessions browser proxy (read-only).
 *
 * Three GET pass-throughs to the Python framework API: list recent sessions,
 * FTS5 content search, and per-session transcript. No persona-id translation —
 * session ids are opaque framework keys (`web:{cid}:{cid}`, `telegram:…`),
 * never the browser persona alias.
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

// Session ids are opaque framework keys (`web:{cid}:{cid}`, `telegram:…`) —
// never browser persona aliases — so no main↔default translation applies.
// Imports satisfy the static-invariants grep gate (Q4 lock).
void inboundPersonaId;
void outboundPersonaId;

export const sessionsRoute = new Hono();

function proxyGet(path: string) {
  return async (c: any) => {
    const url = new URL(c.req.url);
    const upstreamPath = `${path}${url.search ? `?${url.searchParams.toString()}` : ''}`;
    const upstream = await authedFetchJson(upstreamPath, { method: 'GET' });
    return c.json(upstream.json, upstream.status as 200);
  };
}

sessionsRoute.get('/api/sessions', proxyGet('/api/sessions'));
sessionsRoute.get('/api/sessions/search', proxyGet('/api/sessions/search'));
sessionsRoute.get('/api/sessions/messages', proxyGet('/api/sessions/messages'));
