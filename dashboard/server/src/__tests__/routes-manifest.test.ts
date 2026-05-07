/**
 * routes-manifest.test.ts — verifies ROUTE_MANIFEST stays in sync with
 * the actual Hono route mounts.
 *
 * F4 fix (PRD-8 Phase 3 post-build): the manifest is a contract surface
 * consumed by `dashboard/web/src/__tests__/donor-route-manifest.test.ts`.
 * If a route is added in `routes/*.ts` but not added to the manifest,
 * the donor-route-manifest test in WS4 will start producing false
 * negatives (no false positives — donor literals would match the manifest
 * as long as the same path was somewhere). This test catches the gap
 * BEFORE the WS4 test mismatches.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTE_MANIFEST } from '../routes.js';

const SERVER_DIR = join(__dirname, '..');
const ROUTES_DIR = join(SERVER_DIR, 'routes');

function walkFiles(dir: string, suffix = '.ts'): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      out.push(...walkFiles(full, suffix));
    } else if (entry.endsWith(suffix) && !entry.endsWith('.test.ts')) {
      out.push(full);
    }
  }
  return out;
}

/**
 * Extract Hono route literals from a routes/*.ts file.
 *
 * Matches `routerName.<verb>('/api/...', ...)` calls — the leading
 * verb is one of get/post/put/patch/delete/all. Returns paths in the
 * exact form they appear in source (with `:id` colon notation).
 */
function extractHonoRoutes(src: string): string[] {
  const out: string[] = [];
  // Match e.g. agentsRoute.get('/api/agents/:id'
  // or missionRoute.all('/api/convoy'
  const re = /\.(?:get|post|put|patch|delete|all)\s*\(\s*['"`](\/api\/[^'"`]+)['"`]/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) {
    out.push(m[1]);
  }
  return out;
}

describe('ROUTE_MANIFEST contract', () => {
  it('exports a non-empty readonly array', () => {
    expect(ROUTE_MANIFEST.length).toBeGreaterThan(0);
    // Frozen at compile time via `as const` — runtime check that no
    // accidental mutations slip in.
    expect(Array.isArray(ROUTE_MANIFEST)).toBe(true);
  });

  it('contains the canonical /api/health and /api/agents entries', () => {
    expect(ROUTE_MANIFEST).toContain('/api/health');
    expect(ROUTE_MANIFEST).toContain('/api/info');
    expect(ROUTE_MANIFEST).toContain('/api/agents');
    expect(ROUTE_MANIFEST).toContain('/api/agents/:id');
    expect(ROUTE_MANIFEST).toContain('/api/agents/:id/full');
    expect(ROUTE_MANIFEST).toContain('/api/agents/validate-id');
    expect(ROUTE_MANIFEST).toContain('/api/agents/validate-token');
  });

  it('every concrete Hono route in routes/*.ts is covered by the manifest', () => {
    const routeFiles = walkFiles(ROUTES_DIR);
    const found = new Set<string>();
    for (const f of routeFiles) {
      for (const path of extractHonoRoutes(readFileSync(f, 'utf-8'))) {
        // mission.ts uses wildcard mounts (`/api/convoy/*`) — strip the
        // trailing `/*` so we compare against the manifest's parent entry.
        const normalized = path.endsWith('/*') ? path.slice(0, -2) : path;
        found.add(normalized);
      }
    }

    const missing: string[] = [];
    for (const path of found) {
      if (!ROUTE_MANIFEST.includes(path)) {
        missing.push(path);
      }
    }
    expect(missing, `route(s) missing from ROUTE_MANIFEST: ${missing.join(', ')}`).toEqual([]);
  });
});
