/**
 * conversation.test.ts — chat stream/send proxy contract.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { ROUTE_MANIFEST } from '../routes.js';

const CONVERSATION_ROUTE = join(__dirname, '..', 'routes', 'conversation.ts');

describe('conversation route', () => {
  it('registers chat history, send, and stream entries in the manifest', () => {
    expect(ROUTE_MANIFEST).toContain('/api/conversation/:id/history');
    expect(ROUTE_MANIFEST).toContain('/api/conversation/:id/send');
    expect(ROUTE_MANIFEST).toContain('/api/conversation/:id/stream');
  });

  it('keeps Hono as a thin proxy to Python conversation routes', () => {
    const src = readFileSync(CONVERSATION_ROUTE, 'utf-8');
    expect(src).toContain('authedFetchJson');
    expect(src).toContain('authedFetchStream');
    expect(src).toContain('/history');
    expect(src).toContain('/send');
    expect(src).not.toMatch(/better-sqlite3|\bnew\s+Database\(|sqlite3/);
    expect(src).not.toMatch(/readFileSync|TheHomie\/Memory/);
  });
});
