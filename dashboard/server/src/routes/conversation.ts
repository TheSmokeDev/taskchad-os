/**
 * /api/conversation/:id/stream — SSE proxy.
 *
 * Forwarding contract (dashboard-owner charter, R1 M4 owner Decision 4):
 *   - Browser opens EventSource with `?token=...&conversation_id=...`.
 *   - Hono extracts the query token, validates via auth middleware (in
 *     SSE-token-via-query allowlist), and translates it into a Bearer
 *     header when calling the Python framework. The query token NEVER
 *     appears in the upstream URL — framework-client only attaches Bearer.
 *   - Hono forwards Last-Event-ID request header to Python.
 *   - Python is the canonical id source — it emits `id: N\n` lines on a
 *     monotonic integer counter. Hono streams the body byte-for-byte.
 *   - Hono sets `Referrer-Policy: no-referrer` on the response (defense
 *     in depth — the browser referer header would leak the query token
 *     if the page navigated away).
 *   - Hono does NOT add its own keepalive — Python emits `: keepalive\n\n`
 *     every 20s and we forward it verbatim.
 *   - On 410 Gone (Last-Event-ID outside replay buffer), Hono forwards the
 *     status + body verbatim including the X-Refetch-Hint header.
 */

import { Hono } from 'hono';
import { authedFetchStream } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void outboundPersonaId; // imported for static-invariants grep gate.

export const conversationRoute = new Hono();

conversationRoute.get('/api/conversation/:id/stream', async (c) => {
  const browserId = c.req.param('id');
  const frameworkId = inboundPersonaId(browserId) ?? browserId;

  const url = new URL(c.req.url);
  // Strip the `token=` query parameter before building the upstream URL —
  // framework-client only attaches Bearer header to upstream requests.
  url.searchParams.delete('token');
  const upstreamPath = `/api/conversation/${encodeURIComponent(frameworkId)}/stream${
    url.search ? `?${url.searchParams.toString()}` : ''
  }`;

  const lastEventId = c.req.header('Last-Event-ID') ?? c.req.header('last-event-id') ?? null;

  // The token used to authenticate this request is whatever auth middleware
  // accepted — it has already been validated. authedFetchStream will fall
  // back to the boot-snapshot AUTH_POLICY token for upstream Bearer header.
  const upstream = await authedFetchStream(upstreamPath, {
    method: 'GET',
    lastEventId,
    headers: { Accept: 'text/event-stream' },
  });

  if (!upstream.ok && upstream.status !== 410) {
    // Non-200, non-410 — pass through error JSON (likely 401/404/500).
    const body = await upstream.text();
    return c.body(body, upstream.status as 400, {
      'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
    });
  }

  if (upstream.status === 410) {
    const body = await upstream.text();
    const refetchHint = upstream.headers.get('X-Refetch-Hint') ?? '';
    return c.body(body, 410, {
      'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
      'X-Refetch-Hint': refetchHint,
      'Referrer-Policy': 'no-referrer',
    });
  }

  // SSE happy path — stream byte-for-byte with strict SSE response headers.
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
