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
    expect(src).toContain("authedFetchJson('/api/browser-viewer/status')");
    expect(src).toContain("authedFetchBinary('/api/browser-viewer/screenshot'");
    expect(src).toContain("authedFetch(path, { method: 'POST' })");
    expect(src).not.toMatch(/\bfetch\(/);
    expect(src).not.toMatch(/better-sqlite3|\bnew\s+Database\(|sqlite3/);
    expect(src).not.toMatch(/config\.yaml|TheHomie\/Memory/);
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
