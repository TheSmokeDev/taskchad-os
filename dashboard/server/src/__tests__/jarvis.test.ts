/**
 * jarvis.test.ts - Jarvis proof proxy contract.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTE_MANIFEST } from '../routes.js';

const JARVIS_ROUTE = join(__dirname, '..', 'routes', 'jarvis.ts');

describe('jarvis route', () => {
  it('registers /api/jarvis/status in the manifest', () => {
    expect(ROUTE_MANIFEST).toContain('/api/jarvis/status');
  });

  it('keeps Hono as a thin proxy to Python /api/jarvis/status', () => {
    const src = readFileSync(JARVIS_ROUTE, 'utf-8');
    expect(src).toContain("authedFetchJson('/api/jarvis/status')");
    expect(src).not.toMatch(/\bfetch\(/);
  });
});
