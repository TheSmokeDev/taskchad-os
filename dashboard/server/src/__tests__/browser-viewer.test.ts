/**
 * browser-viewer.test.ts — read-only browser viewer proxy contract.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTE_MANIFEST } from '../routes.js';

const BROWSER_VIEWER_ROUTE = join(__dirname, '..', 'routes', 'browser-viewer.ts');

describe('browser viewer route', () => {
  it('registers the Browser Viewer API entries in the manifest', () => {
    expect(ROUTE_MANIFEST).toContain('/api/browser-viewer/status');
    expect(ROUTE_MANIFEST).toContain('/api/browser-viewer/screenshot');
    expect(ROUTE_MANIFEST).toContain('/api/browser-viewer/stream/enable');
    expect(ROUTE_MANIFEST).toContain('/api/browser-viewer/stream/disable');
  });

  it('keeps Hono as a thin proxy to Python browser policy', () => {
    const src = readFileSync(BROWSER_VIEWER_ROUTE, 'utf-8');
    expect(src).toContain("withTarget('/api/browser-viewer/status', resolveTarget(c))");
    expect(src).toContain("withTarget('/api/browser-viewer/screenshot', resolveTarget(c))");
    expect(src).not.toMatch(/\bfetch\(/);
    expect(src).not.toMatch(/better-sqlite3|\bnew\s+Database\(|sqlite3/);
    expect(src).not.toMatch(/config\.yaml|TheHomie\/Memory/);
  });

  it('threads the validated target through every framework call (P3.0)', () => {
    const src = readFileSync(BROWSER_VIEWER_ROUTE, 'utf-8');
    // A hardcoded framework path on a GET silently drives the DESKTOP for a
    // phone request — the original P3.0 landmine. Every GET (including the
    // SSE relay's internal status fetch) must resolve via withTarget().
    expect(src).not.toMatch(/authedFetchJson\('\/api\/browser-viewer/);
    expect(src).not.toMatch(/authedFetchBinary\('\/api\/browser-viewer/);
    expect(src).toContain("withTarget('/api/browser-viewer/elements', resolveTarget(c))");
    // The SSE relay checks the stream state for the SAME target it relays.
    const sseSection = src.slice(src.indexOf("stream/sse"));
    expect(sseSection).toContain("withTarget('/api/browser-viewer/status', resolveTarget(c))");
    // Stream mutations translate the query target into the Python body field
    // (raw passthrough; absent -> empty body = M12 shape).
    expect(src).toContain("JSON.stringify(target === null ? {} : { target })");
    // Raw value forwarded, encoded — Python's validator is the single
    // authority. NO coercion of unknown values to desktop (wrong-target
    // hazard); a port is never read from the client.
    expect(src).toContain('encodeURIComponent(target)');
    expect(src).not.toMatch(/=== 'phone' \? 'phone' : 'desktop'/);
    expect(src).not.toMatch(/query\('port'\)|cdp_port.*query/);
    // The SSE relay surfaces Python's 400/403 rather than mislabeling 409.
    expect(src).toContain('statusRes.status === 400 || statusRes.status === 403');
  });

  it('passes any validated target through raw incl. ghost — Python is the enum authority (P4.0)', () => {
    const src = readFileSync(BROWSER_VIEWER_ROUTE, 'utf-8');
    // resolveTarget returns the RAW query value; it must NOT allow-list target
    // names, or a new target (ghost) would be silently dropped to desktop —
    // exactly the wrong-target hazard this proxy exists to avoid.
    expect(src).toContain("const raw = c.req.query('target')");
    expect(src).toContain("return raw === undefined || raw === '' ? null : raw");
    // No two-value allow-list and no target-specific branch: ghost rides the
    // same passthrough as phone with zero Hono change.
    expect(src).not.toMatch(/\['desktop',\s*'phone'\]/);
    expect(src).not.toMatch(/target === 'ghost'/);
    expect(src).not.toMatch(/target === 'phone'/);
  });

  it('only exposes a direct stream URL on loopback hosts', () => {
    const src = readFileSync(BROWSER_VIEWER_ROUTE, 'utf-8');
    expect(src).toContain('function isLoopbackHost');
    expect(src).toContain("hostname === 'localhost'");
    expect(src).toContain("hostname === '127.0.0.1'");
    expect(src).toContain('direct_ws_url');
    expect(src).not.toContain('input_mouse');
    expect(src).not.toContain('input_keyboard');
  });
});
