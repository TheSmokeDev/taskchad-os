/**
 * /api/talk/* — Hono thin proxy for the Talk mode surface: OpenAI Realtime
 * (WebRTC) voice sessions against the Python orchestration API.
 *
 * Python owns auth resolution (configured key → env → Codex OAuth) and
 * ephemeral client-secret minting. Hono only forwards with Bearer auth
 * (never `?token=`) and passes upstream status codes + FastAPI error bodies
 * (`{detail: {error, switch?}}`) through verbatim — 400 invalid voice,
 * 502 upstream OpenAI failure, 503 no auth / `voice` kill-switch disabled.
 *
 * The minted `clientSecret` is a short-lived ephemeral key forwarded to the
 * browser for the direct OpenAI SDP offer POST. It is never logged here.
 */

import { Hono } from 'hono';
import type { Context } from 'hono';
import { authedFetch, authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

export const talkRoute = new Hono();

// Talk payloads carry no persona ids, so there is nothing to translate.
// Touch both helpers so the static-invariants grep gate at
// __tests__/static-invariants.test.ts:66-78 sees them imported AND
// referenced in this module (same pattern as cabinet.ts).
void inboundPersonaId;
void outboundPersonaId;

talkRoute.get('/api/talk/status', async (c) => {
  const upstream = await authedFetchJson('/api/talk/status');
  if (upstream.json && typeof upstream.json === 'object') {
    return c.json(upstream.json, upstream.status as 200, {
      'Referrer-Policy': 'no-referrer',
    });
  }
  // Upstream returned a non-JSON body — surface as a bad gateway.
  return c.json({ detail: { error: 'talk_status_unavailable' } }, 502, {
    'Referrer-Policy': 'no-referrer',
  });
});

talkRoute.post('/api/talk/session', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const upstream = await authedFetch('/api/talk/session', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
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

// Function-tool relay: the browser forwards each model function call; Python
// owns the entire tool surface (memory vault, calendar, router commands,
// work-queue delegation, gated code exec). Hono stays a pure passthrough —
// 200 with `{ok, output}` (even for tool failures, so the model can speak
// them), 400 unknown tool, 503 voice kill-switch.
talkRoute.post('/api/talk/tool', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const upstream = await authedFetch('/api/talk/tool', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
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

// Session-end vault debrief: the page posts its finalized transcript on
// stop/close; Python gates trivial sessions and spawns the detached
// memory_flush. Pure passthrough — always 200 with a receipt, 503 only for
// the voice kill switch.
talkRoute.post('/api/talk/flush', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const upstream = await authedFetch('/api/talk/flush', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
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

/**
 * Async run polling. The page polls until a run terminates, then injects the
 * result into the Realtime conversation so the model speaks it.
 *
 * `/api/talk/runs/*` is the unified surface (skill | agent | archon | look);
 * `/api/talk/skill-runs/:runId` is the original alias, kept for back-compat.
 * Both MUST be mounted here: static-web.ts 404s any unhandled `/api/` path, and
 * both dev (Vite proxies /api → :3141) and prod go through this proxy. A
 * missing route here means the page silently polls a 404 until its cap and the
 * operator never hears the result.
 */
const proxyRun = async (c: Context, path: string) => {
  const upstream = await authedFetch(path);
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
};

talkRoute.get('/api/talk/runs', async (c) => {
  // Forward the query string — the Runs panel passes ?limit=&history=; the
  // Talk page's bare poll is unchanged.
  const url = new URL(c.req.url);
  return proxyRun(c, `/api/talk/runs${url.search}`);
});

talkRoute.get('/api/talk/runs/:runId', async (c) =>
  proxyRun(c, `/api/talk/runs/${c.req.param('runId')}`),
);

talkRoute.get('/api/talk/skill-runs/:runId', async (c) =>
  proxyRun(c, `/api/talk/skill-runs/${c.req.param('runId')}`),
);
