import { describe, test, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

const WEB_SRC = path.resolve(__dirname, '..');
const PROJECT_ROOT = path.resolve(__dirname, '..', '..');

// Recursively walk a directory and return absolute file paths matching
// the .ts/.tsx filter. Skips node_modules and dist.
function walk(dir: string, filter: (name: string) => boolean): string[] {
  const out: string[] = [];
  function visit(d: string) {
    let entries: string[];
    try { entries = readdirSync(d); } catch { return; }
    for (const name of entries) {
      if (name === 'node_modules' || name === 'dist' || name === '.vite') continue;
      const full = path.join(d, name);
      let stat;
      try { stat = statSync(full); } catch { continue; }
      if (stat.isDirectory()) visit(full);
      else if (filter(name)) out.push(full);
    }
  }
  visit(dir);
  return out;
}

// Exclude test files — they contain literals describing the patterns
// they're testing FOR. The scans target production source under src/.
const TS_FILES = walk(WEB_SRC, (n) => n.endsWith('.ts') || n.endsWith('.tsx'))
  .filter((p) => !p.includes(path.sep + '__tests__' + path.sep));

/** Strip line comments and block comments from TS/TSX source so the
 *  anti-pattern greps don't match prose in JSDoc / `//` comments. */
function stripComments(src: string): string {
  // Remove block comments (lazy match across newlines).
  let out = src.replace(/\/\*[\s\S]*?\*\//g, '');
  // Remove line comments — preserve URLs by requiring no preceding ":" right before "//".
  // Simple line-comment strip: split per line, drop everything from `//` onward,
  // unless the `//` appears inside a string literal. For our anti-pattern
  // greps the simple "first // wins" heuristic is good enough — we never
  // grep inside string literals for these patterns.
  out = out.split('\n').map((line) => {
    // Find the first `//` that isn't inside a string. Cheap heuristic:
    // count quotes before the `//`. If even number of quotes, `//` is
    // outside a string.
    let i = 0;
    while (i < line.length - 1) {
      if (line[i] === '/' && line[i + 1] === '/') {
        // Count quotes before i.
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
  }).join('\n');
  return out;
}

function readSourceWithoutComments(file: string): string {
  return stripComments(readFileSync(file, 'utf8'));
}

describe('Q5 single-yaml-surface lock — no YAML library imports anywhere in dashboard/web', () => {
  test('no `import.*from "yaml"` or `import.*from "js-yaml"`', () => {
    const offenders: string[] = [];
    const yamlImportRe = /import[\s\S]+?from\s+['"](yaml|js-yaml|yaml-ast-parser|@iarna\/toml)['"]/;
    for (const file of TS_FILES) {
      const text = readSourceWithoutComments(file);
      if (yamlImportRe.test(text)) {
        offenders.push(file);
      }
    }
    expect(offenders, `YAML library imported here: ${offenders.join(', ')}`).toEqual([]);
  });

  test('no require("yaml") / require("js-yaml")', () => {
    const offenders: string[] = [];
    const yamlRequireRe = /require\(['"](yaml|js-yaml)['"]\)/;
    for (const file of TS_FILES) {
      const text = readSourceWithoutComments(file);
      if (yamlRequireRe.test(text)) offenders.push(file);
    }
    expect(offenders, `yaml required here: ${offenders.join(', ')}`).toEqual([]);
  });

  test('no yaml.parse() or yaml.safeLoad() call sites', () => {
    const offenders: string[] = [];
    const callRe = /\byaml\.(parse|safeLoad|load)\s*\(/;
    for (const file of TS_FILES) {
      const text = readSourceWithoutComments(file);
      if (callRe.test(text)) offenders.push(file);
    }
    expect(offenders, `yaml.* called here: ${offenders.join(', ')}`).toEqual([]);
  });

  test('package.json does NOT depend on a YAML library', () => {
    const pkg = JSON.parse(readFileSync(path.join(PROJECT_ROOT, 'package.json'), 'utf8'));
    const allDeps = { ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) };
    expect(allDeps['yaml']).toBeUndefined();
    expect(allDeps['js-yaml']).toBeUndefined();
    expect(allDeps['yaml-ast-parser']).toBeUndefined();
  });
});

describe('Rule 2 — no module-scope mutable cache (Map/Set/Object) under src/lib/', () => {
  // The donor's useFetch.ts had `const _cache = new Map<string, unknown>()`
  // at module scope — classic stale-state class-of-bug. We grep for that
  // shape across src/lib/. Components are allowed to declare per-instance
  // refs; module-level mutable maps are NOT.
  test('no `^const|^let|^var ... = new Map(...)` at module scope under src/lib/', () => {
    const libFiles = TS_FILES.filter((f) => f.includes(path.sep + 'lib' + path.sep));
    const offenders: { file: string; line: string }[] = [];
    // Match a top-of-line declaration: `const|let|var name = new Map(...)`.
    // We only grep top-of-line so per-function declarations don't trip.
    const re = /^(?:const|let|var)\s+\w+(?:\s*:\s*[^=]+)?\s*=\s*new (?:Map|Set|WeakMap|WeakSet)\s*\(/m;
    for (const file of libFiles) {
      const text = readSourceWithoutComments(file);
      const lines = text.split('\n');
      for (const line of lines) {
        // Module-scope means line starts with `const|let|var` (no leading whitespace).
        if (/^(const|let|var)\s/.test(line) && /=\s*new (Map|Set|WeakMap|WeakSet)\s*\(/.test(line)) {
          offenders.push({ file, line: line.trim() });
        }
      }
    }
    const msg = offenders.map((o) => `${path.relative(PROJECT_ROOT, o.file)}: ${o.line}`).join('\n');
    expect(offenders, `Rule 2 violation — module-scope mutable cache:\n${msg}`).toEqual([]);
  });
});

describe('Rule 1 — no tunable config bound in default args under src/lib/api.ts', () => {
  test('no `function name(arg = process.env.X)` defaults in api.ts', () => {
    const apiPath = path.join(WEB_SRC, 'lib', 'api.ts');
    const text = readSourceWithoutComments(apiPath);
    // Grep for `=process.env.X` or `=  process.env.X` as a default arg.
    // The Rule 1 violation looks like:
    //   function fetch(token = process.env.SOME_TOKEN) { ... }
    const re = /\(\s*\w+\s*(?::\s*[^=,)]+)?\s*=\s*process\.env\./;
    const match = text.match(re);
    expect(match, `Rule 1 violation — default arg bound to process.env in api.ts: ${match?.[0] ?? ''}`).toBeNull();
  });

  test('no `function name(arg = config.X)` defaults under src/', () => {
    const offenders: { file: string; match: string }[] = [];
    const re = /\(\s*\w+\s*(?::\s*[^=,)]+)?\s*=\s*[A-Z_][A-Z_0-9]+[A-Z_0-9]\s*[,)]/;
    // The above matches `arg = ALL_CAPS_CONST` in a function signature.
    for (const file of TS_FILES) {
      const text = readSourceWithoutComments(file);
      const m = text.match(re);
      if (m) offenders.push({ file, match: m[0] });
    }
    // Allow some specific known-safe patterns? — none for now.
    const msg = offenders.map((o) => `${path.relative(PROJECT_ROOT, o.file)}: ${o.match}`).join('\n');
    expect(offenders, `Rule 1 violation — default arg bound to ALL_CAPS const:\n${msg}`).toEqual([]);
  });
});

describe('thin-proxy boundary — web/ never imports server-only modules', () => {
  test('no `better-sqlite3`, `fs`, `node:fs`, or sqlite-related imports in src/', () => {
    const offenders: { file: string; match: string }[] = [];
    // Allow `node:fs` ONLY in __tests__/ (anti-patterns test reads files).
    const re = /from\s+['"](?:better-sqlite3|sqlite3|sqlite-async|node:fs|fs|node:fs\/promises|fs\/promises|node:child_process|child_process)['"]/;
    for (const file of TS_FILES) {
      if (file.includes(path.sep + '__tests__' + path.sep)) continue;
      const text = readSourceWithoutComments(file);
      const m = text.match(re);
      if (m) offenders.push({ file, match: m[0] });
    }
    const msg = offenders.map((o) => `${path.relative(PROJECT_ROOT, o.file)}: ${o.match}`).join('\n');
    expect(offenders, `Thin-proxy violation — server-only import in web/:\n${msg}`).toEqual([]);
  });

  test('no direct personas/<id>/ path references in src/', () => {
    const offenders: { file: string; match: string }[] = [];
    const re = /personas\/\$\{|personas\/\w+\/(?:config\.yaml|memory|state)/;
    for (const file of TS_FILES) {
      if (file.includes(path.sep + '__tests__' + path.sep)) continue;
      const text = readSourceWithoutComments(file);
      const m = text.match(re);
      if (m) offenders.push({ file, match: m[0] });
    }
    const msg = offenders.map((o) => `${path.relative(PROJECT_ROOT, o.file)}: ${o.match}`).join('\n');
    expect(offenders, `Thin-proxy violation — direct personas/ access:\n${msg}`).toEqual([]);
  });
});

describe('donor URL alias /api/agents/create is intentionally dropped', () => {
  test('no `/api/agents/create` literal anywhere in src/', () => {
    const offenders: string[] = [];
    for (const file of TS_FILES) {
      if (file.includes(path.sep + '__tests__' + path.sep)) continue;
      const text = readSourceWithoutComments(file);
      // Strip string literals too — donor's textual reference in
      // INTENTIONAL_DEVIATIONS.md doesn't apply, but a doc-string could
      // legitimately contain the URL.
      if (text.includes('/api/agents/create')) {
        offenders.push(file);
      }
    }
    expect(offenders, `Donor alias /api/agents/create still in: ${offenders.join(', ')}`).toEqual([]);
  });
});

describe('main↔default translation — the browser side uses "main", not "default"', () => {
  test('DEFAULT_PERSONA_ID_UI exists and equals "main"', async () => {
    const mod = await import('@/lib/routes');
    expect(mod.DEFAULT_PERSONA_ID_UI).toBe('main');
  });
});
