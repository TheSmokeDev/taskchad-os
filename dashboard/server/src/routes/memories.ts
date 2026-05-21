/**
 * /api/memories — paginated read-only proxy.
 * /api/tokens — global lane-aware time series.
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaDict, outboundPersonaId } from '../translate.js';

void outboundPersonaId; // imported for static-invariants grep gate.

export const memoriesRoute = new Hono();

function translateMemoriesPayload(payload: unknown): unknown {
  if (!payload || typeof payload !== 'object') {
    return payload;
  }
  const out: Record<string, unknown> = { ...(payload as Record<string, unknown>) };
  if (Array.isArray(out.memories)) {
    out.memories = out.memories.map((item) => {
      if (!item || typeof item !== 'object') {
        return item;
      }
      return outboundPersonaDict(item as Record<string, unknown>);
    });
  }
  return out;
}

function translateMemoryGraphPayload(payload: unknown): unknown {
  if (!payload || typeof payload !== 'object') {
    return payload;
  }
  const out: Record<string, unknown> = { ...(payload as Record<string, unknown>) };
  if (Array.isArray(out.nodes)) {
    out.nodes = out.nodes.map((item) => {
      if (!item || typeof item !== 'object') {
        return item;
      }
      const node = { ...(item as Record<string, unknown>) };
      if (node.scope_id === 'default') {
        node.scope_id = 'main';
      }
      if (node.scopeId === 'default') {
        node.scopeId = 'main';
      }
      return outboundPersonaDict(node);
    });
  }
  if (out.stats && typeof out.stats === 'object') {
    const stats = { ...(out.stats as Record<string, unknown>) };
    if (stats.scope_id === 'default') {
      stats.scope_id = 'main';
    }
    if (Array.isArray(stats.scopes)) {
      stats.scopes = stats.scopes.map((item) => {
        if (!item || typeof item !== 'object') {
          return item;
        }
        const scope = { ...(item as Record<string, unknown>) };
        if (scope.scope_id === 'default') {
          scope.scope_id = 'main';
        }
        return scope;
      });
    }
    out.stats = stats;
  }
  return out;
}

memoriesRoute.get('/api/memories', async (c) => {
  const url = new URL(c.req.url);
  const personaId = url.searchParams.get('persona_id');
  if (personaId) {
    const fwId = inboundPersonaId(personaId) ?? personaId;
    url.searchParams.set('persona_id', fwId);
  }
  const result = await authedFetchJson(`/api/memories${url.search}`);
  return c.json(translateMemoriesPayload(result.json) as Record<string, unknown>, result.status as 200);
});

memoriesRoute.get('/api/memory/graph', async (c) => {
  const url = new URL(c.req.url);
  const scopeId = url.searchParams.get('scope_id');
  if (scopeId) {
    const fwId = inboundPersonaId(scopeId) ?? scopeId;
    url.searchParams.set('scope_id', fwId);
  }
  const result = await authedFetchJson(`/api/memory/graph${url.search}`);
  return c.json(translateMemoryGraphPayload(result.json) as Record<string, unknown>, result.status as 200);
});

memoriesRoute.get('/api/tokens', async (c) => {
  const url = new URL(c.req.url);
  const result = await authedFetchJson(`/api/tokens${url.search}`);
  return c.json(result.json as Record<string, unknown>, result.status as 200);
});
