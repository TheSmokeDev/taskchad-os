/**
 * social.test.ts - Social (Postiz lane) proxy contract.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTE_MANIFEST } from '../routes.js';

const SOCIAL_ROUTE = join(__dirname, '..', 'routes', 'social.ts');

const SOCIAL_PATHS = [
  '/api/social/status',
  '/api/social/channels',
  '/api/social/queue',
  '/api/social/posts',
  '/api/social/connect-url',
  '/api/social/compose',
  '/api/social/approve',
  '/api/social/reject',
  '/api/social/reconcile',
];

describe('social route', () => {
  it('registers every social path in the manifest', () => {
    for (const path of SOCIAL_PATHS) {
      expect(ROUTE_MANIFEST).toContain(path);
    }
  });

  it('keeps Hono as a thin proxy to the Python social routes', () => {
    const src = readFileSync(SOCIAL_ROUTE, 'utf-8');
    expect(src).toContain("authedFetchJson('/api/social/status')");
    expect(src).not.toMatch(/\bfetch\(/);
    // No SQLite, no yaml, no business logic in the proxy layer.
    expect(src).not.toMatch(/sqlite|yaml/i);
  });

  it('never logs response bodies (connect URLs are sensitive)', () => {
    const src = readFileSync(SOCIAL_ROUTE, 'utf-8');
    expect(src).not.toMatch(/console\.(log|info|warn|error)/);
  });
});
