/**
 * Homie orchestration passthrough — /api/convoy*, /api/mailbox*, /api/team*,
 * /api/capabilities*.
 *
 * These endpoints belong to the orchestration slice (orchestration-owner).
 * Convoy/mailbox/team remain verbatim. Capability catalog queries carry the
 * framework persona id, so that one route honors the canonical main↔default
 * boundary mapping.
 *
 * The catch-all matches GET/POST/PATCH/DELETE on the three prefixes.
 */

import { Hono } from 'hono';
import { authedFetch } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaId } from '../translate.js';

export const missionRoute = new Hono();

const PASSTHROUGH_PREFIXES = ['/api/convoy', '/api/mailbox', '/api/team', '/api/capabilities'];

function isPassthrough(pathname: string): boolean {
  return PASSTHROUGH_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

export function translateCapabilityQuery(url: URL): URL {
  const translated = new URL(url);
  if (translated.pathname.startsWith('/api/capabilities')) {
    const personaId = translated.searchParams.get('persona_id');
    if (personaId !== null) {
      translated.searchParams.set('persona_id', inboundPersonaId(personaId) ?? personaId);
    }
  }
  return translated;
}

export function translateCapabilityResponse(pathname: string, body: string): string {
  if (!pathname.startsWith('/api/capabilities')) return body;
  try {
    const payload = JSON.parse(body) as {
      capabilities?: { catalog?: { persona_id?: string | null } };
    };
    const catalog = payload.capabilities?.catalog;
    if (catalog && typeof catalog.persona_id === 'string') {
      catalog.persona_id = outboundPersonaId(catalog.persona_id) ?? catalog.persona_id;
    }
    return JSON.stringify(payload);
  } catch {
    return body;
  }
}

async function forward(c: import('hono').Context): Promise<Response> {
  const url = translateCapabilityQuery(new URL(c.req.url));
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
  return c.body(translateCapabilityResponse(url.pathname, result.body), result.status as 200, {
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
missionRoute.all('/api/capabilities', async (c) => forward(c));
missionRoute.all('/api/capabilities/*', async (c) => forward(c));
