import { afterEach, describe, expect, it, vi } from 'vitest';
import { agentsRoute } from '../routes/agents.js';

describe('agents blueprint proxy', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('forwards the complete preview intent unchanged to Python', async () => {
    const intent = {
      persona_id: 'api-engineer',
      display_name: 'API Engineer',
      template: 'ai-engineer',
      role: 'Inspect APIs and propose work.',
      model: 'claude-opus-4-7',
      domain: 'api-engineering',
      channel_intent: {
        kind: 'discord',
        channel_id: '123456789012345678',
        name: 'api-engineer',
      },
      operator_exec: false,
    };
    const upstreamCalls: Array<{ url: string; body: unknown }> = [];
    globalThis.fetch = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
      upstreamCalls.push({
        url: String(url),
        body: init?.body ? JSON.parse(String(init.body)) : null,
      });
      return new Response(JSON.stringify({
        persona_id: 'api-engineer',
        preview_hash: 'a'.repeat(64),
        state_hash: 'b'.repeat(64),
        plan: { persona_id: 'api-engineer' },
      }), { status: 200, headers: { 'content-type': 'application/json' } });
    }) as typeof fetch;

    const response = await agentsRoute.request('/api/agents/preview', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(intent),
    });

    expect(response.status).toBe(200);
    expect(upstreamCalls).toEqual([{
      url: 'http://127.0.0.1:4322/api/agents/preview',
      body: intent,
    }]);
    expect(await response.json()).toMatchObject({
      persona_id: 'api-engineer',
      preview_hash: 'a'.repeat(64),
    });
  });
});
