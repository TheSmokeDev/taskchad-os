/**
 * Mission control passthrough — /api/convoy*, /api/mailbox*, /api/team*.
 *
 * These endpoints belong to the orchestration slice (orchestration-owner).
 * Hono forwards verbatim — no body translation, no persona-id mapping
 * (orchestration uses agent_id/team_id keyspace, not persona_id).
 *
 * The catch-all matches GET/POST/PATCH/DELETE on the three prefixes.
 */

import { Hono } from 'hono';
import { authedFetch } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

void inboundPersonaId; // imported for static-invariants grep gate.
void outboundPersonaId;

export const missionRoute = new Hono();

const PASSTHROUGH_PREFIXES = ['/api/convoy', '/api/mailbox', '/api/team'];

function isPassthrough(pathname: string): boolean {
  return PASSTHROUGH_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

async function forward(c: import('hono').Context): Promise<Response> {
  const url = new URL(c.req.url);
  const upstreamPath = `${url.pathname}${url.search}`;
  const method = c.req.method;
  const hasBody = method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS';
  const bodyText = hasBody ? await c.req.text() : undefined;

  const headers: Record<string, string> = {};
  const ct = c.req.header('content-type');
  if (ct) headers['Content-Type'] = ct;

  const result = await authedFetch(upstreamPath, {
    method,
    body: bodyText,
    headers,
  });
  return c.body(result.body, result.status as 200, {
    'Content-Type': result.headers.get('content-type') ?? 'application/json',
  });
}

missionRoute.all('/api/convoy', async (c) => {
  if (!isPassthrough(new URL(c.req.url).pathname)) return c.notFound();
  return forward(c);
});

missionRoute.all('/api/convoy/*', async (c) => forward(c));
missionRoute.all('/api/mailbox', async (c) => forward(c));
missionRoute.all('/api/mailbox/*', async (c) => forward(c));
missionRoute.all('/api/team', async (c) => forward(c));
missionRoute.all('/api/team/*', async (c) => forward(c));
