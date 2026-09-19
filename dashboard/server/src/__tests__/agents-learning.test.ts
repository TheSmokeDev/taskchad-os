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

  it.each(['pause', 'resume', 'activations/act_1/rollback', 'tuning/run', 'tuning/rollback'])('forwards %s once to Python', async (path) => {
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => new Response(JSON.stringify({ persona_id: 'default', paused: true })));
    globalThis.fetch = fetchMock as typeof fetch;
    const response = await agentsRoute.request(`/api/agents/main/learning/${path}`, { method: 'POST' });
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(`http://127.0.0.1:4322/api/agents/default/learning/${path}`);
    expect(fetchMock.mock.calls[0][1]?.method).toBe('POST');
    expect(response.status).toBe(200);
  });

  it.each(['lifecycle', 'tuning'])('reads %s without forwarding caller controls', async (path) => {
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => new Response(JSON.stringify({ persona_id: 'default', status: 'not_ready' })));
    globalThis.fetch = fetchMock as typeof fetch;
    const response = await agentsRoute.request(`/api/agents/main/learning/${path}?force=true&token=secret`);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(`http://127.0.0.1:4322/api/agents/default/learning/${path}`);
    expect(fetchMock.mock.calls[0][1]?.method ?? 'GET').toBe('GET');
    expect(await response.json()).toMatchObject({ persona_id: 'main', status: 'not_ready' });
  });

  it('preserves missing evidence and rollback conflicts', async () => {
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ detail: 'Newer procedure conflicts with rollback' }), { status: 409 }));
    const response = await agentsRoute.request('/api/agents/sales/learning/activations/act_1/rollback', { method: 'POST' });
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ detail: 'Newer procedure conflicts with rollback' });
  });

  it.each(['GET', 'POST'])('keeps %s report periods and inference ownership in Python', async (method) => {
    const fetchMock = vi.fn(async (_url: unknown, _init?: RequestInit) => new Response(JSON.stringify({
      persona_id: 'default', counts: { distinct_conclusions: 2 }, records: [{ id: 'idea', persona_id: 'default' }],
    })));
    globalThis.fetch = fetchMock as typeof fetch;
    const response = await agentsRoute.request('/api/agents/main/learning/report?since=2026-09-01T00%3A00%3A00Z&until=2026-09-08T00%3A00%3A00Z&token=never-forward', { method });
    expect(fetchMock).toHaveBeenCalledOnce();
    const url = new URL(String(fetchMock.mock.calls[0][0]));
    expect(url.pathname).toBe('/api/agents/default/learning/report');
    expect(Object.fromEntries(url.searchParams)).toEqual({ since: '2026-09-01T00:00:00Z', until: '2026-09-08T00:00:00Z' });
    expect(fetchMock.mock.calls[0][1]?.method).toBe(method);
    expect(await response.json()).toMatchObject({ persona_id: 'main', counts: { distinct_conclusions: 2 }, records: [{ id: 'idea', persona_id: 'main' }] });
  });
});
