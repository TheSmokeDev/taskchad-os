/**
 * q5-no-yaml-parser.test.ts — Q5 single-yaml-surface lock.
 *
 * Hard checks (4):
 *   1. No `js-yaml` import in dashboard/server source.
 *   2. No `yaml.parse` / `yaml.safeLoad` calls.
 *   3. No `yaml` / `js-yaml` listed in dashboard/server/package.json deps.
 *   4. No `fs.readFile` or `fs.readFileSync` of any `*.yaml` literal.
 *
 * Python is the ONLY YAML parser in The Homie framework — see
 * dashboard-owner charter "The Single-YAML-Surface Lock (Q5)".
 */

import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SERVER_DIR = join(__dirname, '..');
const PKG_PATH = join(__dirname, '..', '..', 'package.json');

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

describe('Q5 single-yaml-surface lock', () => {
  it('no js-yaml or yaml import in dashboard/server source', () => {
    const allFiles = walkFiles(SERVER_DIR);
    const violations: string[] = [];
    for (const f of allFiles) {
      const src = readFileSync(f, 'utf-8');
      if (
        /from\s+['"]js-yaml['"]/.test(src) ||
        /from\s+['"]yaml['"]/.test(src) ||
        /require\(['"](js-)?yaml['"]\)/.test(src)
      ) {
        violations.push(f);
      }
    }
    expect(violations).toEqual([]);
  });

  it('no yaml.parse or yaml.safeLoad calls', () => {
    const allFiles = walkFiles(SERVER_DIR);
    const violations: string[] = [];
    for (const f of allFiles) {
      const src = readFileSync(f, 'utf-8');
      if (/\byaml\.(parse|safeLoad|load)\(/.test(src) || /\bsafeLoad\(/.test(src)) {
        violations.push(f);
      }
    }
    expect(violations).toEqual([]);
  });

  it('no js-yaml or yaml listed in package.json deps', () => {
    const pkg = JSON.parse(readFileSync(PKG_PATH, 'utf-8')) as {
      dependencies?: Record<string, string>;
      devDependencies?: Record<string, string>;
    };
    const deps = { ...(pkg.dependencies ?? {}), ...(pkg.devDependencies ?? {}) };
    expect(deps['yaml']).toBeUndefined();
    expect(deps['js-yaml']).toBeUndefined();
  });

  it('no fs.readFile of *.yaml literal', () => {
    const allFiles = walkFiles(SERVER_DIR);
    const violations: string[] = [];
    for (const f of allFiles) {
      const src = readFileSync(f, 'utf-8');
      // Any literal mention of .yaml inside a fs.readFile* / fs.readFileSync call
      if (/fs\.read(File|FileSync)\([^)]*\.ya?ml/i.test(src)) {
        violations.push(f);
      }
    }
    expect(violations).toEqual([]);
  });
});
