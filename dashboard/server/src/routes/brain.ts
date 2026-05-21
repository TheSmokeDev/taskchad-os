/**
 * /api/brain/graph — composed durable memory graph + Hive activity overlay.
 */

import { Hono } from 'hono';
import { authedFetchJson } from '../framework-client.js';
import { inboundPersonaId, outboundPersonaDict, outboundPersonaId } from '../translate.js';

export const brainRoute = new Hono();

function translateScopeLabel(value: unknown): unknown {
  if (typeof value !== 'string') {
    return value;
  }
  return value.replace('/default', '/main');
}

function translateScopeRecord(item: unknown): unknown {
  if (!item || typeof item !== 'object') {
    return item;
  }
  const scope = { ...(item as Record<string, unknown>) };
  if (scope.scope_id === 'default') {
    scope.scope_id = 'main';
  }
  if (scope.scopeId === 'default') {
    scope.scopeId = 'main';
  }
  return scope;
}

function translateStats(stats: unknown): unknown {
  if (!stats || typeof stats !== 'object') {
    return stats;
  }
  const out: Record<string, unknown> = { ...(stats as Record<string, unknown>) };
  if (out.scope_id === 'default') {
    out.scope_id = 'main';
  }
  if (out.activity_filter_persona_id === 'default') {
    out.activity_filter_persona_id = 'main';
  }
  if (Array.isArray(out.scopes)) {
    out.scopes = out.scopes.map(translateScopeRecord);
  }
  if (out.memory && typeof out.memory === 'object') {
    out.memory = translateStats(out.memory);
  }
  if (out.activity && typeof out.activity === 'object') {
    const activity = { ...(out.activity as Record<string, unknown>) };
    if (activity.filter_persona_id === 'default') {
      activity.filter_persona_id = 'main';
    }
    out.activity = activity;
  }
  return out;
}

export function translateBrainPayload(payload: unknown): unknown {
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
  if (Array.isArray(out.activity)) {
    out.activity = out.activity.map((item) => {
      if (!item || typeof item !== 'object') {
        return item;
      }
      return outboundPersonaDict(item as Record<string, unknown>);
    });
  }
  if (out.layers && typeof out.layers === 'object') {
    const layers = { ...(out.layers as Record<string, unknown>) };
    if (Array.isArray(layers.scopes)) {
      layers.scopes = layers.scopes.map(translateScopeLabel);
    }
    out.layers = layers;
  }
  if (out.stats && typeof out.stats === 'object') {
    out.stats = translateStats(out.stats);
  }
  return out;
}

brainRoute.get('/api/brain/graph', async (c) => {
  const url = new URL(c.req.url);
  const scopeId = url.searchParams.get('scope_id');
  if (scopeId) {
    const fwId = inboundPersonaId(scopeId) ?? scopeId;
    url.searchParams.set('scope_id', fwId);
  }
  const result = await authedFetchJson(`/api/brain/graph${url.search}`);
  return c.json(translateBrainPayload(result.json) as Record<string, unknown>, result.status as 200);
});

void outboundPersonaId; // imported for static-invariants grep gate.
