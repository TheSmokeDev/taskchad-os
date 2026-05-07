/**
 * anti-patterns.test.ts — TS Rule 1 + Rule 2 enforcement.
 *
 * Rule 1: No tunable config in default args — config resolution must
 *   happen INSIDE the function body, not in the parameter default.
 *   Reject: `function f(t = process.env.X) { ... }`
 *   Reject: `function f(t = config.X) { ... }`
 *
 * Rule 2: No request-time auth caches that drift from disk. Module-level
 *   mutable cache is forbidden EXCEPT the deliberately-snapshotted
 *   AUTH_POLICY in auth-policy.ts (documented exception per PRP §1241).
 *   Reject: top-level `const X = fs.readFileSync(...)`.
 *   Reject: top-level `const X = new Database(...)`.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SERVER_DIR = join(__dirname, '..');

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

describe('TS Rule 1: no tunable config in default args', () => {
  it('no `= process.env.X` in default args', () => {
    const allFiles = walkFiles(SERVER_DIR);
    const violations: string[] = [];
    for (const f of allFiles) {
      const src = readFileSync(f, 'utf-8');
      // Match a parameter default that pulls from process.env
      // `(name = process.env.FOO)` or `(name: T = process.env.FOO)`.
      // We use a multiline scan; ignore lines inside comments.
      const lines = src.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const ln = lines[i] ?? '';
        const trimmed = ln.trim();
        if (trimmed.startsWith('//') || trimmed.startsWith('*')) continue;
        // Heuristic: parenthesized parameter list line (function/arrow) with `= process.env`.
        if (/[(,]\s*\w+(\s*:\s*[^=,)]+)?\s*=\s*process\.env\./.test(ln)) {
          violations.push(`${f}:${i + 1}: ${trimmed}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it('no `= config.X` constant binding in default args', () => {
    const allFiles = walkFiles(SERVER_DIR);
    const violations: string[] = [];
    for (const f of allFiles) {
      const src = readFileSync(f, 'utf-8');
      const lines = src.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const ln = lines[i] ?? '';
        const trimmed = ln.trim();
        if (trimmed.startsWith('//') || trimmed.startsWith('*')) continue;
        // Pattern: parameter default = config.UPPERCASE_NAME.
        if (/[(,]\s*\w+(\s*:\s*[^=,)]+)?\s*=\s*config\.[A-Z_][A-Z_]+/.test(ln)) {
          violations.push(`${f}:${i + 1}: ${trimmed}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });
});

describe('TS Rule 2: no module-level mutable cache (except documented exceptions)', () => {
  it('no top-level fs read into a const', () => {
    const allFiles = walkFiles(SERVER_DIR);
    const violations: string[] = [];
    for (const f of allFiles) {
      const src = readFileSync(f, 'utf-8');
      const lines = src.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const ln = lines[i] ?? '';
        const trimmed = ln.trim();
        // Only flag at column-0 const/let assignments (top-level scope).
        if (/^(const|let)\s+\w+\s*=\s*fs\.read(File|FileSync|dirSync)\(/.test(ln)) {
          violations.push(`${f}:${i + 1}: ${trimmed}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it('no top-level new Database(...) at module scope', () => {
    const allFiles = walkFiles(SERVER_DIR);
    const violations: string[] = [];
    for (const f of allFiles) {
      const src = readFileSync(f, 'utf-8');
      const lines = src.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const ln = lines[i] ?? '';
        if (/^(const|let)\s+\w+\s*=\s*new\s+Database\(/.test(ln)) {
          violations.push(`${f}:${i + 1}: ${ln.trim()}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it('AUTH_POLICY in auth-policy.ts is the ONLY documented module-scope mutable cache', () => {
    // Scan every .ts file for module-level `let _<NAME>` that is mutated
    // — only auth-policy.ts is allowed (it documents the exception).
    const allFiles = walkFiles(SERVER_DIR);
    const violators: string[] = [];
    for (const f of allFiles) {
      if (f.endsWith('auth-policy.ts')) continue;
      const src = readFileSync(f, 'utf-8');
      const lines = src.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const ln = lines[i] ?? '';
        // Top-level mutable let with a UPPER_CASE name (the typical "config cache" smell).
        if (/^let\s+_?[A-Z_][A-Z_0-9]+\s*[:=]/.test(ln)) {
          violators.push(`${f}:${i + 1}: ${ln.trim()}`);
        }
      }
    }
    expect(violators).toEqual([]);
  });
});
