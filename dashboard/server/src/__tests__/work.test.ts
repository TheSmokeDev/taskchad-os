/**
 * work.test.ts — Work Queue proxy contract.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTE_MANIFEST } from '../routes.js';

const WORK_ROUTE = join(__dirname, '..', 'routes', 'work.ts');

describe('work route', () => {
  it('registers the Work Queue API entries in the manifest', () => {
    expect(ROUTE_MANIFEST).toContain('/api/work/tasks');
    expect(ROUTE_MANIFEST).toContain('/api/work/tasks/:taskId');
    expect(ROUTE_MANIFEST).toContain('/api/work/tasks/:taskId/dispatch');
  });

  it('keeps Hono as a thin proxy to Python /api/work/tasks', () => {
    const src = readFileSync(WORK_ROUTE, 'utf-8');
    expect(src).toContain('authedFetch(upstreamPath');
    expect(src).not.toMatch(/\bfetch\(/);
    expect(src).not.toMatch(/better-sqlite3|\bnew\s+Database\(|sqlite3/);
    expect(src).not.toMatch(/readFileSync|config\.yaml|TheHomie\/Memory/);
  });
});
