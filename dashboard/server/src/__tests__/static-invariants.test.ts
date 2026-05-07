/**
 * static-invariants.test.ts — the load-bearing grep gate.
 *
 * Catches the class-of-bugs that owner review and unit tests would miss:
 *   1. NO better-sqlite3 / new Database in dashboard/server (thin-proxy).
 *   2. NO vault/memory or personas/ filesystem reads.
 *   3. Every route handler imports BOTH translation helpers.
 *   4. framework-client.ts is the only fetch surface (no bare fetch in routes).
 *   5. NO cross-slice imports (../personas, ../runtime, etc).
 */

import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

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

function readSource(p: string): string {
  return readFileSync(p, 'utf-8');
}

describe('static invariants: thin-proxy boundary', () => {
  it('NO better-sqlite3 import in dashboard/server', () => {
    const allFiles = walkFiles(SERVER_DIR);
    const violations: string[] = [];
    for (const f of allFiles) {
      const src = readSource(f);
      if (
        /better-sqlite3/.test(src) ||
        /\bnew\s+Database\(/.test(src) ||
        /from\s+['"]sqlite3['"]/.test(src)
      ) {
        violations.push(f);
      }
    }
    expect(violations).toEqual([]);
  });

  it('NO direct fs read of vault/personas paths', () => {
    const allFiles = walkFiles(SERVER_DIR);
    const violations: string[] = [];
    for (const f of allFiles) {
      const src = readSource(f);
      if (/TheHomie\/Memory/.test(src) || /\bpersonas\/[^"'`\s]+\/(config\.yaml|memory|state)/.test(src)) {
        violations.push(f);
      }
    }
    expect(violations).toEqual([]);
  });

  it('every route handler imports BOTH inboundPersonaId and outboundPersonaId', () => {
    const routeFiles = walkFiles(ROUTES_DIR);
    const violations: string[] = [];
    for (const f of routeFiles) {
      const src = readSource(f);
      const hasInbound = /inboundPersonaId/.test(src);
      const hasOutbound = /outboundPersonaId/.test(src);
      if (!hasInbound || !hasOutbound) {
        violations.push(`${f} (inbound=${hasInbound}, outbound=${hasOutbound})`);
      }
    }
    expect(violations).toEqual([]);
  });

  it('framework-client.ts is the only direct fetch() caller in routes/', () => {
    const routeFiles = walkFiles(ROUTES_DIR);
    const violations: string[] = [];
    for (const f of routeFiles) {
      const src = readSource(f);
      // Strip strings/comments crudely; look for a bare fetch( call.
      // Allow `fetch` references inside import lines.
      const lines = src.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const ln = lines[i] ?? '';
        const trimmed = ln.trim();
        if (trimmed.startsWith('//') || trimmed.startsWith('*')) continue;
        // bare-word `fetch(` at call position (not authedFetch, not part of identifier)
        if (/\bfetch\(/.test(ln) && !/(authedFetch|c\.req\.formData|outboundFetch)/.test(ln)) {
          violations.push(`${f}:${i + 1}: ${trimmed}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it('NO cross-slice imports (../personas, ../runtime, ../chat)', () => {
    const allFiles = walkFiles(SERVER_DIR);
    const violations: string[] = [];
    for (const f of allFiles) {
      const src = readSource(f);
      if (/from\s+['"](\.\.\/)+(personas|runtime|chat|orchestration|cognition)/.test(src)) {
        violations.push(f);
      }
    }
    expect(violations).toEqual([]);
  });
});
