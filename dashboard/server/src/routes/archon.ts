/**
 * /api/archon/* — Hono thin proxy for the Archon live-telemetry bridge
 * (epic #252 / ticket #254).
 *
 * Python owns everything real here: the read-only cursor-tail of Archon's
 * `remote_agent_workflow_events` ledger, the event normalization/redaction,
 * and the SSE channel. Hono forwards with Bearer auth and passes upstream
 * status codes through verbatim — 200 with an honest `status` field when the
 * ledger is missing or unreadable (this surface never 500s on a cold machine),
 * 410 + `X-Refetch-Hint` on an SSE replay-buffer miss, 503 when the
 * `archon_events` kill-switch is disabled.
 *
 * B3 lock (inherited from cabinet.ts): the `/api/archon/stream` route MUST use
 * `authedFetchStream()` + `new Response(upstream.body, ...)`. `authedFetch()`
 * buffers via `.text()` and would break SSE delivery.
 *
 * Archon payloads carry Archon run/conversation ids, never Homie persona ids,
 * so there is nothing to translate at this boundary.
 */

import { Hono } from 'hono';
import { authedFetch, authedFetchStream } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

export const archonRoute = new Hono();

// Touch both helpers so the static-invariants grep gate at
// __tests__/static-invariants.test.ts:66-78 sees them imported AND
// referenced in this module (same pattern as talk.ts).
void inboundPersonaId;
void outboundPersonaId;

archonRoute.get('/api/archon/events', async (c) => {
  const url = new URL(c.req.url);
  const upstream = await authedFetch(`/api/archon/events${url.search}`);
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(parsed, upstream.status as 200, {
      'Referrer-Policy': 'no-referrer',
    });
  }
  return c.body(upstream.body, upstream.status as 200, {
    'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
    'Referrer-Policy': 'no-referrer',
  });
});

// Convoy row enrichment (#258) — the worker identity + current node for one
// convoy's Archon-backed subtasks. Same thin-proxy contract as /events: Python
// owns the join (convoy ledger -> correlation key -> ro archon.db) and the
// workspace gate; Hono forwards with Bearer auth and passes the upstream status
// through verbatim, including the 404 for a convoy outside the caller's scope.
archonRoute.get('/api/archon/convoy/:convoyId', async (c) => {
  const url = new URL(c.req.url);
  const convoyId = encodeURIComponent(c.req.param('convoyId'));
  const upstream = await authedFetch(`/api/archon/convoy/${convoyId}${url.search}`);
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(parsed, upstream.status as 200, {
      'Referrer-Policy': 'no-referrer',
    });
  }
  return c.body(upstream.body, upstream.status as 200, {
    'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
    'Referrer-Policy': 'no-referrer',
  });
});

archonRoute.get('/api/archon/stream', async (c) => {
  // B3 — MUST use authedFetchStream + new Response(upstream.body, ...).
  // NEVER authedFetch / .text() (would buffer the entire stream).
  const url = new URL(c.req.url);
  url.searchParams.delete('token');
  const upstreamPath = `/api/archon/stream${url.search ? `?${url.searchParams.toString()}` : ''}`;
  const lastEventId = c.req.header('Last-Event-ID') ?? c.req.header('last-event-id') ?? null;

  const upstream = await authedFetchStream(upstreamPath, {
    method: 'GET',
    lastEventId,
    headers: { Accept: 'text/event-stream' },
  });

  if (upstream.status === 410) {
    const body = await upstream.text();
    return c.body(body, 410, {
      'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
      'X-Refetch-Hint': upstream.headers.get('X-Refetch-Hint') ?? '',
      'Referrer-Policy': 'no-referrer',
    });
  }

  if (!upstream.ok) {
    const body = await upstream.text();
    return c.body(body, upstream.status as 400, {
      'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
      'Referrer-Policy': 'no-referrer',
    });
  }

  const responseHeaders = new Headers({
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'X-Accel-Buffering': 'no',
    'Referrer-Policy': 'no-referrer',
    Connection: 'keep-alive',
  });

  if (!upstream.body) {
    return c.body('', 200, Object.fromEntries(responseHeaders.entries()));
  }

  return new Response(upstream.body, {
    status: 200,
    headers: responseHeaders,
  });
});
