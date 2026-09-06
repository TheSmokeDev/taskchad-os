import { afterEach, describe, expect, it, vi } from 'vitest';
import { agentsRoute } from '../routes/agents.js';

describe('learning proxy', () => {
  afterEach(() => vi.restoreAllMocks());

  it('translates the default persona and nested ledger ownership without changing record ids', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      persona_id: 'default', records: [{ id: 'default', persona_id: 'default', payload: { content: 'default', persona_id: 'sales' } }], next_cursor: '5',
    }), { headers: { 'content-type': 'application/json' } }));
    globalThis.fetch = fetchMock;
    const response = await agentsRoute.request('/api/agents/main/learning/records?kind=candidate&cursor=8&limit=20&token=never-forward');
    const url = new URL(String(fetchMock.mock.calls[0][0]));
    expect(url.pathname).toBe('/api/agents/default/learning/records');
    expect(Object.fromEntries(url.searchParams)).toEqual({ kind: 'candidate', cursor: '8', limit: '20' });
    expect(await response.json()).toMatchObject({
      persona_id: 'main', records: [{ id: 'default', persona_id: 'main', payload: { content: 'default', persona_id: 'sales' } }],
    });
  });

  it.each(['pause', 'resume', 'activations/act_1/rollback'])('forwards %s once to Python', async (path) => {
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => new Response(JSON.stringify({ persona_id: 'default', paused: true })));
    globalThis.fetch = fetchMock as typeof fetch;
    const response = await agentsRoute.request(`/api/agents/main/learning/${path}`, { method: 'POST' });
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(`http://127.0.0.1:4322/api/agents/default/learning/${path}`);
    expect(fetchMock.mock.calls[0][1]?.method).toBe('POST');
    expect(response.status).toBe(200);
  });

  it('preserves missing evidence and rollback conflicts', async () => {
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ detail: 'Newer procedure conflicts with rollback' }), { status: 409 }));
    const response = await agentsRoute.request('/api/agents/sales/learning/activations/act_1/rollback', { method: 'POST' });
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ detail: 'Newer procedure conflicts with rollback' });
  });
});
