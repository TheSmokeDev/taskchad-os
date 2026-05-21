/**
 * brain.test.ts — composed brain proxy contract.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTE_MANIFEST } from '../routes.js';
import { translateBrainPayload } from '../routes/brain.js';

const BRAIN_ROUTE = join(__dirname, '..', 'routes', 'brain.ts');

describe('brain route', () => {
  it('registers /api/brain/graph in the manifest', () => {
    expect(ROUTE_MANIFEST).toContain('/api/brain/graph');
  });

  it('keeps Hono as a thin proxy to Python /api/brain/graph', () => {
    const src = readFileSync(BRAIN_ROUTE, 'utf-8');
    expect(src).toContain('authedFetchJson(`/api/brain/graph${url.search}`)');
    expect(src).not.toMatch(/\bfetch\(/);
  });

  it('translates default scope and activity persona ids outbound', () => {
    const translated = translateBrainPayload({
      nodes: [
        {
          id: 'chunk:1',
          persona_id: 'default',
          personaId: 'default',
          scope_type: 'global',
          scope_id: 'default',
        },
      ],
      activity: [
        {
          id: 'chat-1',
          persona_id: 'default',
          personaId: 'default',
          details: 'recent event',
        },
      ],
      layers: {
        memory: true,
        activity: true,
        scopes: ['global/default', 'room/cabinet-1'],
      },
      stats: {
        scope: 'persona',
        scope_id: 'default',
        activity_filter_persona_id: 'default',
        memory: {
          scopes: [{ scope_type: 'global', scope_id: 'default', count: 2 }],
        },
        activity: {
          filter_persona_id: 'default',
        },
      },
    }) as Record<string, any>;

    expect(translated.nodes[0].persona_id).toBe('main');
    expect(translated.nodes[0].personaId).toBe('main');
    expect(translated.nodes[0].scope_id).toBe('main');
    expect(translated.activity[0].persona_id).toBe('main');
    expect(translated.activity[0].personaId).toBe('main');
    expect(translated.layers.scopes).toEqual(['global/main', 'room/cabinet-1']);
    expect(translated.stats.scope_id).toBe('main');
    expect(translated.stats.activity_filter_persona_id).toBe('main');
    expect(translated.stats.memory.scopes[0].scope_id).toBe('main');
    expect(translated.stats.activity.filter_persona_id).toBe('main');
  });
});
