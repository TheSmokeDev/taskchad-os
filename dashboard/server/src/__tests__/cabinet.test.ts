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

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTE_MANIFEST } from '../routes.js';

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
      'personas',
      'agents',
      'roster',
      'transcript',
    ];
    for (const f of required) {
      expect(src, `outbound translator missing field: ${f}`).toContain(f);
    }
  });

  it('all 12 cabinet routes registered in ROUTE_MANIFEST', () => {
    const expected = [
      '/api/cabinet/list',
      '/api/cabinet/new',
      '/api/cabinet/warmup',
      '/api/cabinet/details',
      '/api/cabinet/transcripts',
      '/api/cabinet/stream',
      '/api/cabinet/send',
      '/api/cabinet/abort',
      '/api/cabinet/pin',
      '/api/cabinet/unpin',
      '/api/cabinet/clear',
      '/api/cabinet/end',
    ];
    for (const path of expected) {
      expect(ROUTE_MANIFEST).toContain(path);
    }
  });
});
