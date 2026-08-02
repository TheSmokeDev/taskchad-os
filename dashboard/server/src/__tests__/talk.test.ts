/**
 * talk.test.ts — Hono talk route static + behavior invariants.
 *
 * Asserts:
 *   - /api/talk/status forwards via authedFetchJson and passes the Python
 *     readiness payload through verbatim.
 *   - /api/talk/session forwards the JSON body via authedFetch (Bearer,
 *     never `?token=`) and returns the minted session verbatim.
 *   - Upstream error statuses (503 kill-switch / no-auth, 502 upstream
 *     OpenAI failure) pass through with the FastAPI
 *     `{detail: {error, switch?}}` body verbatim.
 *   - Route mount: ROUTE_MANIFEST contains both /api/talk/* paths.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Hono } from 'hono';
import { ROUTE_MANIFEST } from '../routes.js';
import { talkRoute } from '../routes/talk.js';

const TALK_ROUTE = join(__dirname, '..', 'routes', 'talk.ts');

describe('talk route — static invariants', () => {
  it('exists at routes/talk.ts', () => {
    expect(() => readFileSync(TALK_ROUTE, 'utf-8')).not.toThrow();
  });

  it('forwards through framework-client (authedFetchJson + authedFetch)', () => {
    const src = readFileSync(TALK_ROUTE, 'utf-8');
    expect(src).toContain('authedFetchJson(');
    expect(src).toContain('authedFetch(');
  });

  it('references both persona translators (static-invariants gate)', () => {
    const src = readFileSync(TALK_ROUTE, 'utf-8');
    expect(src).toMatch(/inboundPersonaId/);
    expect(src).toMatch(/outboundPersonaId/);
  });

  it('both talk routes registered in ROUTE_MANIFEST', () => {
    expect(ROUTE_MANIFEST).toContain('/api/talk/status');
    expect(ROUTE_MANIFEST).toContain('/api/talk/session');
  });

  it('mounts the session-end flush route the page fires on stop/close', () => {
    const src = readFileSync(TALK_ROUTE, 'utf-8');
    expect(src).toMatch(/'\/api\/talk\/flush'/);
    expect(ROUTE_MANIFEST).toContain('/api/talk/flush');
  });

  /**
   * Regression: `/api/talk/runs/:runId` shipped on the Python side without a
   * Hono handler. static-web.ts 404s any unhandled `/api/` path, and the Talk
   * page's poller swallows fetch errors and retries — so delegated agent
   * results silently never reached the browser. Every run route the page polls
   * MUST have a handler here, not just a manifest entry (app.ts mounts modules
   * directly; the manifest is a test-time artifact).
   */
  it('mounts every async-run poll route the Talk page calls', () => {
    const src = readFileSync(TALK_ROUTE, 'utf-8');
    expect(src).toMatch(/'\/api\/talk\/runs'/);
    expect(src).toMatch(/'\/api\/talk\/runs\/:runId'/);
    expect(src).toMatch(/'\/api\/talk\/skill-runs\/:runId'/);
    expect(ROUTE_MANIFEST).toContain('/api/talk/runs');
    expect(ROUTE_MANIFEST).toContain('/api/talk/runs/:runId');
  });

  it('the page polls the same run path the proxy mounts', () => {
    const page = readFileSync(
      join(__dirname, '..', '..', '..', 'web', 'src', 'pages', 'Talk.tsx'),
      'utf-8',
    );
    // If the page's poll URL and the proxy mount drift, results stop arriving.
    expect(page).toMatch(/\/api\/talk\/runs\/\$\{runId\}/);
  });
});

describe('talk route — proxy behavior', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetAllMocks();
  });

  it('forwards the runs query string (the Runs panel history poll)', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ ok: true, runs: [] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const app = new Hono();
    app.route('/', talkRoute);

    const res = await app.request('/api/talk/runs?limit=50&history=true');
    expect(res.status).toBe(200);
    const upstreamUrl = String(fetchMock.mock.calls[0]?.[0] ?? '');
    expect(upstreamUrl).toContain('/api/talk/runs?limit=50&history=true');
  });

  it('passes the status payload through verbatim', async () => {
    const payload = {
      ok: true,
      configured: true,
      source: 'codex-oauth',
      detail: 'Signed in via Codex OAuth.',
      model: 'gpt-4o-realtime-preview',
      voice: 'marin',
      voices: ['marin', 'cedar'],
      killSwitchVoiceDisabled: false,
    };
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const app = new Hono();
    app.route('/', talkRoute);

    const res = await app.request('/api/talk/status');
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(payload);
    const upstreamUrl = String(fetchMock.mock.calls[0]?.[0] ?? '');
    expect(upstreamUrl).toContain('/api/talk/status');
    expect(res.headers.get('Referrer-Policy')).toBe('no-referrer');
  });

  it('forwards the session POST body and returns the minted session verbatim', async () => {
    const minted = {
      ok: true,
      clientSecret: 'ek_test_ephemeral',
      expiresAt: 1750000000000,
      offerUrl: 'https://api.openai.com/v1/realtime/calls',
      model: 'gpt-4o-realtime-preview',
      voice: 'cedar',
      authSource: 'env',
    };
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify(minted), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const app = new Hono();
    app.route('/', talkRoute);

    const res = await app.request('/api/talk/session', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ voice: 'cedar' }),
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual(minted);
    const upstreamUrl = String(fetchMock.mock.calls[0]?.[0] ?? '');
    expect(upstreamUrl).toContain('/api/talk/session');
    expect(upstreamUrl).not.toContain('token=');
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body as string)).toEqual({ voice: 'cedar' });
  });

  it('passes a 503 kill-switch error body through verbatim', async () => {
    const errBody = { detail: { error: 'voice disabled by kill switch', switch: 'voice' } };
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify(errBody), {
        status: 503,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const app = new Hono();
    app.route('/', talkRoute);

    const res = await app.request('/api/talk/session', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({}),
    });
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual(errBody);
  });

  it('passes a 502 upstream OpenAI failure through verbatim', async () => {
    const errBody = { detail: { error: 'OpenAI realtime session mint failed' } };
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify(errBody), {
        status: 502,
        headers: { 'content-type': 'application/json' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const app = new Hono();
    app.route('/', talkRoute);

    const res = await app.request('/api/talk/session', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({}),
    });
    expect(res.status).toBe(502);
    expect(await res.json()).toEqual(errBody);
  });
});
