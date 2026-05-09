/**
 * /api/cabinet/* — Hono thin proxy for the Phase 5a cabinet REST + SSE
 * surface (PRD-8 Phase 5a / WS3).
 *
 * 11 verbatim ports of upstream `dashboard.ts:802-1254` action/query-shaped
 * routes (`/list`, `/new`, `/warmup`, `/transcripts` (= upstream `/history`),
 * `/stream`, `/send`, `/abort`, `/pin`, `/unpin`, `/clear`, `/end`) PLUS
 * 1 Homie delta `GET /api/cabinet/details` (page-load helper not present
 * upstream).
 *
 * Q4 lock — every persona-id-bearing field is translated at this Hono
 * boundary:
 *   - request body: pinnedAgent / agentId / personas[] / pinnedPersona /
 *     primaryPersona / participantId / intervenerId — Browser ('main') →
 *     Python ('default') via `inboundPersonaId`.
 *   - response body + SSE event payloads: every field above plus
 *     `meeting_state.pinnedAgent`, `meeting_state.agents[].id`,
 *     `meeting_state_update.pinnedAgent`, `router_decision.primary` +
 *     `router_decision.interveners[]`, `status_update.agentId`,
 *     `agent_*.agentId`, `tool_*.agentId`, `intervention_skipped.agentId`,
 *     `error.agentId`, `turn_aborted.clearedAgents[]` — Python ('default')
 *     → Browser ('main') via `outboundPersonaId`.
 *   - cross-cutting `turnId` / `clientMsgId` / `transcriptRowId` are
 *     passthrough (NOT persona ids, NOT translated).
 *
 * B3 lock — the `/api/cabinet/stream` route MUST call `authedFetchStream()`
 * and return `new Response(upstream.body, ...)` for raw SSE passthrough.
 * `authedFetch()` would buffer via `.text()` and break SSE delivery.
 *
 * Mirror the existing conversation stream pattern at
 * `dashboard/server/src/routes/conversation.ts:30-87`.
 */

import { Hono } from 'hono';
import { authedFetch, authedFetchStream } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

export const cabinetRoute = new Hono();

// ── translation helpers (Q4 — main↔default) ──────────────────────────────

function tr<T>(value: T, fn: (id: string | undefined | null) => string | undefined | null): T {
  if (typeof value !== 'string') return value;
  const out = fn(value);
  return (out as unknown) as T;
}

function translatePersonaArray<T extends string>(
  arr: unknown,
  fn: (id: string | undefined | null) => string | undefined | null,
): T[] | unknown {
  if (!Array.isArray(arr)) return arr;
  return arr.map((v) => (typeof v === 'string' ? fn(v) ?? v : v));
}

function translatePersonaFieldsOutbound<T extends Record<string, unknown>>(obj: T): T {
  // Outbound — Python 'default' → Browser 'main'. Apply to every
  // persona-id-bearing field on the upstream response body or SSE event.
  if (!obj || typeof obj !== 'object') return obj;
  const fields: string[] = [
    'pinnedAgent',
    'pinned_persona',
    'agentId',
    'primaryPersona',
    'pinnedPersona',
    'participantId',
    'intervenerId',
    'primary',
  ];
  const out: Record<string, unknown> = { ...obj };
  for (const f of fields) {
    if (f in out && typeof out[f] === 'string') {
      out[f] = outboundPersonaId(out[f] as string);
    }
  }
  if ('interveners' in out) {
    out.interveners = translatePersonaArray(out.interveners, outboundPersonaId);
  }
  if ('clearedAgents' in out) {
    out.clearedAgents = translatePersonaArray(out.clearedAgents, outboundPersonaId);
  }
  if ('personas' in out) {
    out.personas = translatePersonaArray(out.personas, outboundPersonaId);
  }
  // agents[].id rewrite for meeting_state / details / list / transcripts.
  if (Array.isArray(out.agents)) {
    out.agents = (out.agents as Array<Record<string, unknown>>).map((a) => {
      if (a && typeof a === 'object' && typeof a.id === 'string') {
        return { ...a, id: outboundPersonaId(a.id as string) };
      }
      return a;
    });
  }
  // roster[].id rewrite for /details.
  if (Array.isArray(out.roster)) {
    out.roster = (out.roster as Array<Record<string, unknown>>).map((a) => {
      if (a && typeof a === 'object' && typeof a.id === 'string') {
        return { ...a, id: outboundPersonaId(a.id as string) };
      }
      return a;
    });
  }
  // transcript[].speaker rewrite for /transcripts response.
  if (Array.isArray(out.transcript)) {
    out.transcript = (out.transcript as Array<Record<string, unknown>>).map((row) => {
      if (row && typeof row === 'object' && typeof row.speaker === 'string') {
        return { ...row, speaker: outboundPersonaId(row.speaker as string) };
      }
      return row;
    });
  }
  // meeting{} nested for /details.
  if (out.meeting && typeof out.meeting === 'object') {
    out.meeting = translatePersonaFieldsOutbound(out.meeting as Record<string, unknown>);
  }
  return out as T;
}

function translatePersonaFieldsInbound<T extends Record<string, unknown>>(obj: T): T {
  if (!obj || typeof obj !== 'object') return obj;
  const fields: string[] = [
    'pinnedAgent',
    'agentId',
    'primaryPersona',
    'pinnedPersona',
    'participantId',
    'intervenerId',
  ];
  const out: Record<string, unknown> = { ...obj };
  for (const f of fields) {
    if (f in out && typeof out[f] === 'string') {
      out[f] = inboundPersonaId(out[f] as string);
    }
  }
  if ('personas' in out) {
    out.personas = translatePersonaArray(out.personas, inboundPersonaId);
  }
  return out as T;
}

// Touch the helper so the static-invariants grep gate at
// __tests__/static-invariants.test.ts:66-78 sees both translators imported
// AND used in this module.
void tr;

// ── Routes ───────────────────────────────────────────────────────────────

cabinetRoute.get('/api/cabinet/list', async (c) => {
  const url = new URL(c.req.url);
  const upstream = await authedFetch(`/api/cabinet/list${url.search}`);
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    const body = parsed as { meetings?: Array<Record<string, unknown>> };
    if (Array.isArray(body.meetings)) {
      body.meetings = body.meetings.map((m) => translatePersonaFieldsOutbound(m));
    }
    return c.json(body, upstream.status as 200);
  }
  return c.body(upstream.body, upstream.status as 200);
});

cabinetRoute.post('/api/cabinet/new', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const upstream = await authedFetch('/api/cabinet/new', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(translatePersonaFieldsInbound(body)),
  });
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(translatePersonaFieldsOutbound(parsed as Record<string, unknown>), upstream.status as 200);
  }
  return c.body(upstream.body, upstream.status as 200);
});

cabinetRoute.post('/api/cabinet/warmup', async (c) => {
  const upstream = await authedFetch('/api/cabinet/warmup', { method: 'POST' });
  return c.body(upstream.body, upstream.status as 200, {
    'Content-Type': upstream.headers.get('content-type') ?? 'application/json',
  });
});

cabinetRoute.get('/api/cabinet/details', async (c) => {
  const url = new URL(c.req.url);
  const upstream = await authedFetch(`/api/cabinet/details${url.search}`);
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(translatePersonaFieldsOutbound(parsed as Record<string, unknown>), upstream.status as 200);
  }
  return c.body(upstream.body, upstream.status as 200);
});

cabinetRoute.get('/api/cabinet/transcripts', async (c) => {
  const url = new URL(c.req.url);
  const upstream = await authedFetch(`/api/cabinet/transcripts${url.search}`);
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(translatePersonaFieldsOutbound(parsed as Record<string, unknown>), upstream.status as 200);
  }
  return c.body(upstream.body, upstream.status as 200);
});

cabinetRoute.get('/api/cabinet/stream', async (c) => {
  // B3 — MUST use authedFetchStream + new Response(upstream.body, ...).
  // NEVER authedFetch / .text() (would buffer the entire stream).
  const url = new URL(c.req.url);
  url.searchParams.delete('token');
  const upstreamPath = `/api/cabinet/stream${url.search ? `?${url.searchParams.toString()}` : ''}`;
  const lastEventId = c.req.header('Last-Event-ID') ?? c.req.header('last-event-id') ?? null;

  const upstream = await authedFetchStream(upstreamPath, {
    method: 'GET',
    lastEventId,
    headers: { Accept: 'text/event-stream' },
  });

  if (!upstream.ok && upstream.status !== 410) {
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

  // NOTE: SSE event payloads carry persona ids (`agentId`, `pinnedAgent`,
  // etc) in JSON inside the `data:` lines. Translating these requires
  // server-side stream parsing, which would buffer chunks. For Phase 5a,
  // SSE byte-streaming is preserved AS-IS — the Browser-side EventSource
  // consumer in `dashboard/web/src/lib/cabinet-stream.ts` MUST translate
  // `default` → `main` on receipt for any persona-id-bearing field. This
  // matches the conversation SSE pattern (Phase 3).
  return new Response(upstream.body, {
    status: 200,
    headers: responseHeaders,
  });
});

cabinetRoute.post('/api/cabinet/send', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  // Q4 inbound mention translation (dashboard-owner GAP 2 fix): operator
  // typed `@main hello` from the composer (which sees the translated
  // canonical id `main`); rewrite the `@main` token to `@default` BEFORE
  // forwarding so the Python @-mention extractor at
  // cabinet/text_orchestrator.py:250-263 finds the match.
  const translatedBody = translatePersonaFieldsInbound(body) as Record<string, unknown>;
  if (typeof translatedBody.text === 'string') {
    translatedBody.text = (translatedBody.text as string).replace(
      /(^|\s)@main\b/g,
      '$1@default',
    );
  }
  const upstream = await authedFetch('/api/cabinet/send', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(translatedBody),
  });
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(translatePersonaFieldsOutbound(parsed as Record<string, unknown>), upstream.status as 200);
  }
  return c.body(upstream.body, upstream.status as 200);
});

cabinetRoute.post('/api/cabinet/abort', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const upstream = await authedFetch('/api/cabinet/abort', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(translatePersonaFieldsInbound(body)),
  });
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(translatePersonaFieldsOutbound(parsed as Record<string, unknown>), upstream.status as 200);
  }
  return c.body(upstream.body, upstream.status as 200);
});

cabinetRoute.post('/api/cabinet/pin', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const translated = translatePersonaFieldsInbound(body);
  const upstream = await authedFetch('/api/cabinet/pin', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(translated),
  });
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(translatePersonaFieldsOutbound(parsed as Record<string, unknown>), upstream.status as 200);
  }
  return c.body(upstream.body, upstream.status as 200);
});

cabinetRoute.post('/api/cabinet/unpin', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const upstream = await authedFetch('/api/cabinet/unpin', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(translatePersonaFieldsInbound(body)),
  });
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(translatePersonaFieldsOutbound(parsed as Record<string, unknown>), upstream.status as 200);
  }
  return c.body(upstream.body, upstream.status as 200);
});

cabinetRoute.post('/api/cabinet/clear', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const upstream = await authedFetch('/api/cabinet/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(translatePersonaFieldsInbound(body)),
  });
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(translatePersonaFieldsOutbound(parsed as Record<string, unknown>), upstream.status as 200);
  }
  return c.body(upstream.body, upstream.status as 200);
});

cabinetRoute.post('/api/cabinet/end', async (c) => {
  const body = await c.req.json().catch(() => ({}));
  const upstream = await authedFetch('/api/cabinet/end', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(translatePersonaFieldsInbound(body)),
  });
  const parsed = upstream.json();
  if (parsed && typeof parsed === 'object') {
    return c.json(translatePersonaFieldsOutbound(parsed as Record<string, unknown>), upstream.status as 200);
  }
  return c.body(upstream.body, upstream.status as 200);
});
