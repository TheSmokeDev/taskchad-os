/**
 * donor-route-manifest.test.ts — F4 fix (PRD-8 Phase 3 post-build).
 *
 * Mechanism that catches donor URLs drifting from Hono. Scans every
 * `dashboard/web/src/**\/*.{ts,tsx}` file (production sources only —
 * skips __tests__/ and INTENTIONAL_DEVIATIONS.md content) for `/api/`
 * URL literals, and asserts each one resolves to either:
 *   (a) an entry in `ROUTE_MANIFEST` (the canonical Hono route surface),
 *       OR
 *   (b) a literal documented in `dashboard/web/INTENTIONAL_DEVIATIONS.md`
 *       as an explicit drop / alias.
 *
 * Adding an `/api/...` literal that is neither in the manifest nor in
 * INTENTIONAL_DEVIATIONS.md fails this test. That is exactly the gap
 * that allowed F1 (the wizard contract drift) to ship to post-build
 * adversarial review undetected.
 *
 * Path-parameter normalization: the manifest uses Hono `:id` notation;
 * source code uses template-literal `${...}` notation. Both shapes are
 * normalized to a structural placeholder before comparison.
 */

import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { ROUTE_MANIFEST } from '../../../server/src/routes.js';

const WEB_SRC = path.resolve(__dirname, '..');
const PROJECT_ROOT = path.resolve(__dirname, '..', '..');
const DEVIATIONS_PATH = path.join(PROJECT_ROOT, 'INTENTIONAL_DEVIATIONS.md');

function walk(dir: string, filter: (name: string) => boolean): string[] {
  const out: string[] = [];
  function visit(d: string) {
    let entries: string[];
    try {
      entries = readdirSync(d);
    } catch {
      return;
    }
    for (const name of entries) {
      if (name === 'node_modules' || name === 'dist' || name === '.vite') continue;
      const full = path.join(d, name);
      let stat;
      try {
        stat = statSync(full);
      } catch {
        continue;
      }
      if (stat.isDirectory()) visit(full);
      else if (filter(name)) out.push(full);
    }
  }
  visit(dir);
  return out;
}

/**
 * Strip block + line comments so JSDoc references like `apiPost('/api/x')`
 * inside doc comments don't trip the test. Mirrors the heuristic used by
 * anti-patterns.test.tsx.
 */
function stripComments(src: string): string {
  let out = src.replace(/\/\*[\s\S]*?\*\//g, '');
  out = out
    .split('\n')
    .map((line) => {
      let i = 0;
      while (i < line.length - 1) {
        if (line[i] === '/' && line[i + 1] === '/') {
          const before = line.slice(0, i);
          const dq = (before.match(/"/g) || []).length;
          const sq = (before.match(/'/g) || []).length;
          const bq = (before.match(/`/g) || []).length;
          if (dq % 2 === 0 && sq % 2 === 0 && bq % 2 === 0) {
            return line.slice(0, i).trimEnd();
          }
        }
        i++;
      }
      return line;
    })
    .join('\n');
  return out;
}

/**
 * Normalize an extracted URL literal to a structural shape that matches
 * `ROUTE_MANIFEST`'s `:id` notation. The literal scanner captures
 * everything up to the next non-URL character; we still need to fold:
 *   - `${anything}`  → `:id`
 *   - trailing `/${encodeURIComponent` (cut by the regex) → `/:id`
 *   - `:something` segments stay as `:id`
 *   - trailing `/` (e.g. `/api/agents/`) → drop
 *   - query-string portion → drop
 */
function normalize(literal: string): string {
  let out = literal;
  // Step 1 — collapse all `${...}` interpolations BEFORE touching `?`.
  //
  // Two distinct `${...}` shapes:
  //   (a) `/foo/${id}` — the interpolation IS the path segment → `:id`.
  //   (b) `/foo${cond ? '?x' : ''}` — the interpolation is a SUFFIX
  //       modifier (typically a query-string toggle); not a path segment.
  //       We strip these so they don't leak `?` chars into the path.
  // Heuristic: if the character right BEFORE `${` is `/`, fold to `:id`.
  // Otherwise, strip the entire `${...}` block (suffix modifier).
  out = out.replace(/(\/)?\$\{[^}]*\}/g, (_match, prevSlash) =>
    prevSlash === '/' ? '/:id' : '',
  );
  // Handle the (rare) unbalanced trailing `${...` left when the source
  // extractor truncated on a quote boundary.
  out = out.replace(/(\/)?\$\{[a-zA-Z_]+$/, (_match, prevSlash) =>
    prevSlash === '/' ? '/:id' : '',
  );
  // Fold any trailing `/encodeURIComponent` token left behind by the
  // template literal capture into `:id`.
  out = out.replace(/\/encodeURIComponent$/, '/:id');

  // Step 2 — drop the literal query-string suffix, AFTER `${...}`
  // interpolations are removed (so a `?` inside a stripped interpolation
  // doesn't get treated as the path/query boundary).
  const q = out.indexOf('?');
  if (q !== -1) out = out.slice(0, q);

  // Step 3 — replace existing `:foo` segments with `:id` (uniform form).
  out = out.replace(/:[a-zA-Z_][a-zA-Z0-9_]*/g, ':id');
  // Drop trailing `/` (canonical: no trailing slashes per contract).
  if (out.endsWith('/') && out !== '/') out = out.slice(0, -1);
  return out;
}

/**
 * Returns true if a normalized literal is covered by ROUTE_MANIFEST.
 * Direct membership OR prefix match against a manifest base path
 * (handles cases like `/api/scheduled` matching when the literal is
 * `/api/scheduled/:id`).
 */
function isManifestCovered(normalized: string): boolean {
  if (ROUTE_MANIFEST.includes(normalized)) return true;
  // Allow a literal to match a manifest entry if normalized strips to the
  // same prefix (e.g. literal `/api/agents/:id/files/:id` matches manifest
  // `/api/agents/:id/files/:filename` after both are normalized to `:id`).
  for (const m of ROUTE_MANIFEST) {
    if (normalize(m) === normalized) return true;
  }
  // Also allow matches against parent paths for wildcard-mounted mission
  // proxies — `/api/convoy/:id/anything` covers any path under `/api/convoy/`
  // because mission.ts mounts `/api/convoy/*` as a passthrough.
  const wildcardParents = [
    '/api/convoy',
    '/api/mailbox',
    '/api/team',
  ];
  for (const parent of wildcardParents) {
    if (normalized === parent) return true;
    if (normalized.startsWith(parent + '/')) return true;
  }
  return false;
}

function readDeviations(): string {
  try {
    return readFileSync(DEVIATIONS_PATH, 'utf-8');
  } catch {
    return '';
  }
}

const TS_FILES = walk(WEB_SRC, (n) => n.endsWith('.ts') || n.endsWith('.tsx')).filter(
  (p) => !p.includes(path.sep + '__tests__' + path.sep),
);

/**
 * Extract /api/... literals from source. Handles three syntaxes:
 *   - Plain string literals: `'/api/agents'`, `"/api/agents/foo"`
 *   - Template literals: ``\`/api/agents/${id}/full\``` — captures the
 *     whole path up to the closing backtick, including any nested `(...)`
 *     inside `${...}` interpolations.
 *
 * Returns the source-form literal (template literals keep their `${...}`
 * placeholders verbatim — `normalize()` folds them to `:id` afterwards).
 */
function extractApiLiterals(src: string): string[] {
  const out: string[] = [];

  // (1) Plain string literals — single or double quotes.
  // Match `/api/...` until the next quote of the same type.
  const plainRe = /['"](\/api\/[^'"]*)['"]/g;
  let m: RegExpExecArray | null;
  while ((m = plainRe.exec(src)) !== null) {
    out.push(m[1]);
  }

  // (2) Template literals — capture `/api/...` content until closing backtick.
  // Inside backticks, `${...}` interpolations may contain nested parens; we
  // walk balanced braces to find the matching `}` and continue.
  const tmplStart = /`(\/api\/[^`$]*)/g;
  while ((m = tmplStart.exec(src)) !== null) {
    let i = m.index + 1; // position after the opening backtick
    let path = '';
    while (i < src.length && src[i] !== '`') {
      if (src[i] === '$' && src[i + 1] === '{') {
        // Walk to the matching closing brace.
        let depth = 1;
        path += '${';
        i += 2;
        while (i < src.length && depth > 0) {
          if (src[i] === '{') depth++;
          else if (src[i] === '}') depth--;
          if (depth === 0) {
            path += '}';
            i++;
            break;
          }
          path += src[i];
          i++;
        }
      } else {
        path += src[i];
        i++;
      }
    }
    if (path.startsWith('/api/')) out.push(path);
  }

  return out;
}

describe('donor-route-manifest: every /api/ literal in web src maps to Hono route or intentional deviation', () => {
  it('every dashboard api literal maps to hono route or intentional deviation', () => {
    const deviationsText = readDeviations();

    const offenders: { file: string; literal: string; normalized: string }[] = [];
    for (const file of TS_FILES) {
      const src = stripComments(readFileSync(file, 'utf-8'));
      const literals = extractApiLiterals(src);
      for (const literal of literals) {
        const normalized = normalize(literal);
        if (isManifestCovered(normalized)) continue;
        // Documented as an intentional drop?
        if (deviationsText.includes(literal)) continue;
        offenders.push({ file, literal, normalized });
      }
    }

    const msg = offenders
      .map((o) => `${path.relative(PROJECT_ROOT, o.file)}: ${o.literal} (normalized: ${o.normalized})`)
      .join('\n');
    expect(
      offenders,
      `Donor-route drift — these /api/ literals are neither in ROUTE_MANIFEST nor INTENTIONAL_DEVIATIONS.md:\n${msg}`,
    ).toEqual([]);
  });

  it('ROUTE_MANIFEST is non-empty and contains the dashboard core routes', () => {
    expect(ROUTE_MANIFEST.length).toBeGreaterThan(0);
    expect(ROUTE_MANIFEST).toContain('/api/agents');
    expect(ROUTE_MANIFEST).toContain('/api/agents/validate-id');
    expect(ROUTE_MANIFEST).toContain('/api/agents/validate-token');
  });

  it('INTENTIONAL_DEVIATIONS.md exists', () => {
    expect(readDeviations().length).toBeGreaterThan(0);
  });
});
