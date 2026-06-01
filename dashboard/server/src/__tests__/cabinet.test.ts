/**
 * cabinet.test.ts — Hono cabinet route static + behavior invariants.
 *
 * PRD-8 Phase 5a / WS3.
 *
 * Asserts:
 *   - B3: /api/cabinet/stream uses authedFetchStream + Response(upstream.body)
 *     never authedFetch / .text() (would buffer SSE).
 *   - B6 + NM1: every Q4 persona-id-bearing field is translated.
 *   - Route mount: ROUTE_MANIFEST contains every /api/cabinet/* path.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { Hono } from 'hono';
import { ROUTE_MANIFEST } from '../routes.js';
import { cabinetRoute } from '../routes/cabinet.js';

const CABINET_ROUTE = join(__dirname, '..', 'routes', 'cabinet.ts');

describe('cabinet route — static invariants', () => {
  it('exists at routes/cabinet.ts', () => {
    expect(() => readFileSync(CABINET_ROUTE, 'utf-8')).not.toThrow();
  });

  it('uses authedFetchStream for /api/cabinet/stream (B3)', () => {
    const src = readFileSync(CABINET_ROUTE, 'utf-8');
    // Find the stream route handler block.
    const streamBlock = src.match(/cabinetRoute\.get\(\s*['"]\/api\/cabinet\/stream['"][\s\S]*?\}\);/);
    expect(streamBlock, 'no /api/cabinet/stream route found').toBeTruthy();
    const block = streamBlock![0];
    expect(block).toContain('authedFetchStream(');
    expect(block).toContain('new Response(upstream.body');
  });

  it('does NOT call authedFetch( or .text() inside the /stream handler (B3)', () => {
    const src = readFileSync(CABINET_ROUTE, 'utf-8');
    const streamBlock = src.match(/cabinetRoute\.get\(\s*['"]\/api\/cabinet\/stream['"][\s\S]*?\}\);/);
    expect(streamBlock).toBeTruthy();
    const block = streamBlock![0];
    // authedFetch( is forbidden inside /stream — `authedFetchStream` is allowed
    // and should not match a bare `authedFetch(` (the `Stream` suffix means
    // the regex is anchored to start-of-identifier).
    const bareAuthedFetch = /\bauthedFetch\(/g;
    const matches = block.match(bareAuthedFetch);
    expect(matches, '/stream handler must not call authedFetch() — buffering breaks SSE').toBeFalsy();
  });

  it('imports both inboundPersonaId and outboundPersonaId (Q4 translation)', () => {
    const src = readFileSync(CABINET_ROUTE, 'utf-8');
    expect(src).toMatch(/inboundPersonaId/);
    expect(src).toMatch(/outboundPersonaId/);
  });

  it('translates persona-id-bearing fields on outbound responses (B6/NM1)', () => {
    const src = readFileSync(CABINET_ROUTE, 'utf-8');
    // The Q4 enumeration: every persona-id-bearing field gets translated.
    // We assert the field names appear in the translation helper.
    const required = [
      'pinnedAgent',
      'agentId',
      'pinnedPersona',
      'primaryPersona',
      'primary',
      'interveners',
      'clearedAgents',
      'broadcastOrder',
      'targetAgentId',
      'targetAgentIds',
      'personas',
      'agents',
      'roster',
      'transcript',
    ];
    for (const f of required) {
      expect(src, `outbound translator missing field: ${f}`).toContain(f);
    }
  });

  it('all cabinet routes registered in ROUTE_MANIFEST', () => {
    const expected = [
      '/api/cabinet/list',
      '/api/cabinet/new',
      '/api/cabinet/open',
      '/api/cabinet/warmup',
      '/api/cabinet/details',
      '/api/cabinet/participants/available',
      '/api/cabinet/participants/add',
      '/api/cabinet/participants/remove',
      '/api/cabinet/transcripts',
      '/api/cabinet/stream',
      '/api/cabinet/send',
      '/api/cabinet/abort',
      '/api/cabinet/pin',
      '/api/cabinet/unpin',
      '/api/cabinet/clear',
      '/api/cabinet/end',
      '/api/cabinet/voice/ui',
      '/api/cabinet/voice/client.bundle.js',
      '/api/cabinet/voice/client.js',
      '/api/cabinet/voice/avatars/:persona_id.png',
    ];
    for (const path of expected) {
      expect(ROUTE_MANIFEST).toContain(path);
    }
  });

  it('proxies Cabinet voice document/static routes through Python', () => {
    const src = readFileSync(CABINET_ROUTE, 'utf-8');
    expect(src).toContain("/api/cabinet/voice/ui");
    expect(src).toContain("/api/cabinet/voice/client.bundle.js");
    expect(src).toContain("/api/cabinet/voice/client.js");
    expect(src).toContain("/api/cabinet/voice/avatars/:persona_file");
    expect(src).toContain("authedFetchBinary(");
    expect(src).toContain("Referrer-Policy");
  });
});

describe('cabinet route — voice proxy behavior', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.resetAllMocks();
  });

  it('preserves the voice UI query string when forwarding to Python', async () => {
    const fetchMock = vi.fn(async () =>
      new Response('<html>voice</html>', {
        status: 200,
        headers: { 'content-type': 'text/html; charset=utf-8' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const app = new Hono();
    app.route('/', cabinetRoute);

    const res = await app.request('/api/cabinet/voice/ui?meetingId=7&chatId=cabinet-browser&token=secret');
    expect(res.status).toBe(200);
    expect(await res.text()).toContain('voice');
    const upstreamUrl = String(fetchMock.mock.calls[0]?.[0] ?? '');
    expect(upstreamUrl).toContain('/api/cabinet/voice/ui?meetingId=7&chatId=cabinet-browser&token=secret');
    expect(res.headers.get('Referrer-Policy')).toBe('no-referrer');
  });

  it('proxies Cabinet voice avatar PNGs with the persona id intact', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(new Uint8Array([137, 80, 78, 71]), {
        status: 200,
        headers: { 'content-type': 'image/png' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const app = new Hono();
    app.route('/', cabinetRoute);

    const res = await app.request('/api/cabinet/voice/avatars/main.png?token=secret');
    expect(res.status).toBe(200);
    const upstreamUrl = String(fetchMock.mock.calls[0]?.[0] ?? '');
    expect(upstreamUrl).toContain('/api/cabinet/voice/avatars/main.png?token=secret');
    expect(res.headers.get('Content-Type')).toContain('image/png');
  });
});
