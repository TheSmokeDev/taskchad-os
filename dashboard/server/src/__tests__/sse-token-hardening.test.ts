/**
 * sse-token-hardening.test.ts — owner Decision 4 / R1 M4.
 *
 * Three checks:
 *   1. Hono access log scrubs `?token=...` query string before write.
 *   2. SSE response sets `Referrer-Policy: no-referrer`.
 *   3. framework-client never includes `?token=` in upstream URL when
 *      calling port 4322 (static check on the source).
 */

import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { scrubTokenFromUrl } from '../middleware/log-scrub.js';

const FRAMEWORK_CLIENT = join(__dirname, '..', 'framework-client.ts');
const CONVERSATION_ROUTE = join(__dirname, '..', 'routes', 'conversation.ts');

describe('SSE token hardening', () => {
  it('scrubTokenFromUrl strips token query value', () => {
    expect(scrubTokenFromUrl('http://x/api/info?token=abc&foo=bar')).toBe(
      'http://x/api/info?token=<redacted>&foo=bar',
    );
    expect(scrubTokenFromUrl('http://x/api/info?foo=bar&token=abc')).toBe(
      'http://x/api/info?foo=bar&token=<redacted>',
    );
    expect(scrubTokenFromUrl('http://x/api/info?Token=ABC')).toBe(
      'http://x/api/info?Token=<redacted>',
    );
    expect(scrubTokenFromUrl('http://x/api/info')).toBe('http://x/api/info');
  });

  it('conversation route forces Referrer-Policy: no-referrer on SSE response', () => {
    const src = readFileSync(CONVERSATION_ROUTE, 'utf-8');
    expect(src).toMatch(/Referrer-Policy['"]?\s*[:,]\s*['"]no-referrer/);
  });

  it('framework-client never appends ?token= to upstream URL', () => {
    const src = readFileSync(FRAMEWORK_CLIENT, 'utf-8');
    // Allow `?token=` to appear in comments only (never in code that builds a URL).
    // Strip block + line comments before scanning code.
    const codeOnly = src
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .split('\n')
      .map((ln) => (ln.trim().startsWith('//') ? '' : ln))
      .join('\n');
    // Now scan code for `token=` literal in URL construction.
    expect(codeOnly).not.toMatch(/['"`][^'"`]*\?token=/);
    expect(codeOnly).not.toMatch(/['"`][^'"`]*&token=/);
    // And ensure we attach Authorization Bearer.
    expect(src).toMatch(/Authorization.*Bearer/);
  });
});
