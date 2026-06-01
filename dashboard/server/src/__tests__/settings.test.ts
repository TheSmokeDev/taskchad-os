/**
 * settings.test.ts — dashboard settings/mobile-access proxy contract.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTE_MANIFEST } from '../routes.js';

const SETTINGS_ROUTE = join(__dirname, '..', 'routes', 'settings.ts');

describe('settings route', () => {
  it('registers settings and mobile access in the manifest', () => {
    expect(ROUTE_MANIFEST).toContain('/api/dashboard/settings');
    expect(ROUTE_MANIFEST).toContain('/api/dashboard/mobile-access');
  });

  it('keeps mobile access as a thin read-only proxy to Python', () => {
    const src = readFileSync(SETTINGS_ROUTE, 'utf-8');
    expect(src).toContain("authedFetchJson('/api/dashboard/mobile-access'");
    expect(src).toContain("'X-Dashboard-Request-Host'");
    expect(src).not.toMatch(/\bfetch\(/);
    expect(src).not.toMatch(/better-sqlite3|\bnew\s+Database\(|sqlite3/);
    expect(src).not.toMatch(/tailscale\s+serve|tailscale\s+up|tailscale\s+set/);
  });
});
