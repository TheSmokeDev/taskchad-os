import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import * as ts from 'typescript';
import { describe, expect, it } from 'vitest';
import {
  ROUTE_INVENTORY,
  requirementGaps,
  type LegacyBaselineId,
  type MigrationOwner,
  type RouteInventoryEntry,
} from '@/routeInventory/inventory';
import { LEGACY_BASELINES } from '@/routeInventory/legacyBaselines';
import {
  expectedLegacySnapshot,
  matchLegacyBaseline,
  parseLegacyRouteSource,
  parseNavLine,
  parsePageImportLine,
  parseRouteLine,
  type BaselineMatch,
  type LegacyRouteSnapshot,
} from '@/routeInventory/legacySource';

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../../..');
const readRepoFile = (path: string) => readFileSync(resolve(REPO_ROOT, path), 'utf8');
const sha256Lf = (text: string) => createHash('sha256').update(text.replace(/\r\n/g, '\n')).digest('hex');

const physicalRoutesTs = readRepoFile('dashboard/web/src/lib/routes.ts');
const physicalAppTsx = readRepoFile('dashboard/web/src/App.tsx');
const physicalPage = (module: string) => readRepoFile(`dashboard/web/src/pages/${module}.tsx`);

const exactBaselines = Object.values(LEGACY_BASELINES)
  .filter((baseline) => baseline.sources.every((source) => sha256Lf(readRepoFile(source.path)) === source.sha256Lf))
  .map((baseline) => baseline.id);

function parse(
  { routesTs = physicalRoutesTs, appTsx = physicalAppTsx, readPage = physicalPage }: {
    routesTs?: string;
    appTsx?: string;
    readPage?: (module: string) => string;
  } = {},
): LegacyRouteSnapshot {
  return parseLegacyRouteSource(routesTs, appTsx, readPage);
}

function formatDrift(match: BaselineMatch): string {
  if (match.matched !== null) return `matched ${match.matched}`;
  return Object.entries(match.drift)
    .map(([id, drift]) => `${id}:\n  ${drift.join('\n  ')}`)
    .join('\n');
}

function physicalBaseline(): LegacyBaselineId {
  const match = matchLegacyBaseline(parse());
  if (match.matched === null) throw new Error(`physical legacy router matches no baseline:\n${formatDrift(match)}`);
  return match.matched;
}

function replaceOnce(source: string, search: string | RegExp, replacement: string): string {
  const count = typeof search === 'string' ? source.split(search).length - 1 : (source.match(new RegExp(search, 'g')) ?? []).length;
  if (count !== 1) throw new Error(`mutation target must occur exactly once, found ${count}: ${search}`);
  return source.replace(search, replacement);
}

/** Use the existing test-time compiler to ignore source examples inside comments and literals. */
function moduleSpecifiers(source: string): string[] {
  const file = ts.createSourceFile('inventory.ts', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const specifiers: string[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
      if (node.moduleSpecifier && ts.isStringLiteralLike(node.moduleSpecifier)) specifiers.push(node.moduleSpecifier.text);
    } else if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      const specifier = node.arguments[0];
      if (!specifier || !ts.isStringLiteralLike(specifier)) throw new Error('inventory dynamic imports must use a literal module specifier');
      specifiers.push(specifier.text);
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return [...new Set(specifiers)].sort();
}

describe('legacy dashboard/web router source', () => {
  it('matches exactly one documented baseline by destinations, nav grouping, aliases, default and fallback', () => {
    const match = matchLegacyBaseline(parse());
    expect(match.matched, formatDrift(match)).not.toBeNull();
  });

  it('has byte-identical (LF-normalized) sources pinned to the same baseline it structurally matches', () => {
    expect(exactBaselines).toEqual([physicalBaseline()]);
  });
});

describe('local-only evidence snapshot', () => {
  const evidence = LEGACY_BASELINES['local-phase0'].localOnlyEvidence;

  it('parses to exactly the Jarvis nav and route delta, plus the Audit placeholder removal', () => {
    const origin = expectedLegacySnapshot('origin-master');
    const local = expectedLegacySnapshot('local-phase0');

    const evidenceNav = evidence.flatMap((line) => parseNavLine(line.text) ?? []);
    const evidenceImports = evidence.flatMap((line) => parsePageImportLine(line.text) ?? []);
    const evidenceRoutes = evidence.flatMap((line) => {
      const route = parseRouteLine(line.text);
      return route?.kind === 'page' ? [route] : [];
    });

    expect(evidenceNav).toEqual([{ path: '/jarvis', label: 'Jarvis', section: 'intelligence' }]);
    expect(evidenceImports).toEqual([{ component: 'Jarvis', module: 'Jarvis' }]);
    expect(evidenceRoutes).toEqual([{ kind: 'page', path: '/jarvis', component: 'Jarvis' }]);
    expect(origin.placeholders).toContain('/audit');
    // Equality of the entire snapshot checks both directions: no additional local
    // additions, removals or changes to nav, modules, aliases, home or fallback.
    expect(local).toEqual({
      ...origin,
      nav: [...origin.nav, ...evidenceNav].sort((a, b) => a.path.localeCompare(b.path)),
      pages: [...origin.pages, '/jarvis'].sort(),
      pageModules: { ...origin.pageModules, '/jarvis': 'Jarvis' },
      placeholders: origin.placeholders.filter((path) => path !== '/audit'),
    });
    expect(evidence.filter((line) => line.path.endsWith('/pages/Audit.tsx')).map((line) => line.text)).toEqual([
      expect.stringContaining("'/api/audit-log'"),
    ]);
  });

  it.runIf(exactBaselines.includes('local-phase0'))('is verbatim at the recorded line numbers of the local source', () => {
    for (const line of evidence) {
      expect(readRepoFile(line.path).split(/\r?\n/)[line.line - 1]).toBe(line.text);
    }
  });

  it.runIf(exactBaselines.includes('origin-master'))('is absent from the published source', () => {
    for (const line of evidence) {
      expect(readRepoFile(line.path).split(/\r?\n/)).not.toContain(line.text);
    }
  });
});

describe('route drift sensitivity on the physical source', () => {
  function driftOf(snapshot: LegacyRouteSnapshot): readonly string[] {
    const match = matchLegacyBaseline(snapshot);
    if (match.matched !== null) throw new Error(`mutated source still matches ${match.matched}`);
    return match.drift[physicalBaseline()];
  }

  const cases: { name: string; snapshot: () => LegacyRouteSnapshot; drift: string[] }[] = [
    {
      name: 'a mounted destination is removed',
      snapshot: () => parse({ appTsx: replaceOnce(physicalAppTsx, '<Route path="/usage"><Usage /></Route>', '') }),
      drift: ['page: missing /usage', 'page module: missing /usage -> Usage'],
    },
    {
      name: 'an unlisted destination is mounted',
      snapshot: () =>
        parse({
          appTsx: replaceOnce(
            physicalAppTsx,
            '<Route path="/settings"><Settings /></Route>',
            '<Route path="/settings"><Settings /></Route>\n<Route path="/labs"><Settings /></Route>',
          ),
        }),
      drift: ['page: unexpected /labs', 'page module: unexpected /labs -> Settings'],
    },
    {
      name: 'an alias is retargeted',
      snapshot: () =>
        parse({
          appTsx: replaceOnce(
            physicalAppTsx,
            '<Route path="/tasks"><Redirect to="/work" /></Route>',
            '<Route path="/tasks"><Redirect to="/convoy" /></Route>',
          ),
        }),
      drift: ['alias: /tasks -> /work changed to /convoy'],
    },
    {
      name: 'an alias is removed',
      snapshot: () => parse({ appTsx: replaceOnce(physicalAppTsx, '<Route path="/warroom"><Redirect to="/cabinet" /></Route>', '') }),
      drift: ['alias: missing /warroom -> /cabinet'],
    },
    {
      name: 'an alias is added',
      snapshot: () =>
        parse({
          appTsx: replaceOnce(
            physicalAppTsx,
            '<Route path="/team"><Redirect to="/teams" /></Route>',
            '<Route path="/team"><Redirect to="/teams" /></Route>\n<Route path="/queue"><Redirect to="/work" /></Route>',
          ),
        }),
      drift: ['alias: unexpected /queue -> /work'],
    },
    {
      name: 'a redirect is replaced by a page',
      snapshot: () =>
        parse({
          appTsx: replaceOnce(
            physicalAppTsx,
            '<Route path="/gateway"><Redirect to="/capabilities" /></Route>',
            '<Route path="/gateway"><CapabilityGateway /></Route>',
          ),
        }),
      drift: ['page: unexpected /gateway', 'page module: unexpected /gateway -> CapabilityGateway', 'alias: missing /gateway -> /capabilities'],
    },
    {
      name: 'the default route is switched to the future /chat home',
      snapshot: () =>
        parse({ routesTs: replaceOnce(physicalRoutesTs, "export const DEFAULT_ROUTE = '/mission';", "export const DEFAULT_ROUTE = '/chat';") }),
      drift: ['default route: /mission changed to /chat', 'home redirect: /mission changed to /chat'],
    },
    {
      name: 'a nav entry moves to another group',
      snapshot: () =>
        parse({ routesTs: replaceOnce(physicalRoutesTs, "section: 'intelligence', icon: ShieldCheck", "section: 'configure', icon: ShieldCheck") }),
      drift: ['nav: /audit -> intelligence "Audit" changed to configure "Audit"'],
    },
    {
      name: 'a nav entry is added without a route',
      snapshot: () =>
        parse({
          routesTs: replaceOnce(physicalRoutesTs, /\n\];/, "\n  { path: '/labs', label: 'Labs', section: 'configure', icon: SettingsIcon },\n];"),
        }),
      drift: ['nav: unexpected /labs -> configure "Labs"'],
    },
    {
      name: 'the catch-all route is removed',
      snapshot: () => parse({ appTsx: replaceOnce(physicalAppTsx, /<Route>\s*<Placeholder[\s\S]*?<\/Route>/, '') }),
      drift: ['fallback: Not found changed to none'],
    },
    {
      name: 'the Standup placeholder becomes a page',
      snapshot: () =>
        parse({
          readPage: (module) => (module === 'StandupConfig' ? 'export function StandupConfig() { return null; }' : physicalPage(module)),
        }),
      drift: ['placeholder: missing /standup'],
    },
  ];

  it.each(cases)('reports drift when $name', ({ snapshot, drift }) => {
    expect(driftOf(snapshot())).toEqual(drift);
  });

  it('rejects a route registration shape it cannot read instead of skipping it', () => {
    const appTsx = replaceOnce(
      physicalAppTsx,
      '<Route path="/usage"><Usage /></Route>',
      "<Route path=\"/usage\" component={lazy(() => import('@/pages/Usage'))} />",
    );
    expect(() => parse({ appTsx })).toThrow(/unrecognized route line/);
  });

  it('rejects an unrelated page mounted at an existing destination', () => {
    const appTsx = replaceOnce(physicalAppTsx, '<Route path="/usage"><Usage /></Route>', '<Route path="/usage"><Settings /></Route>');
    expect(driftOf(parse({ appTsx }))).toEqual(['page module: /usage -> Usage changed to Settings']);
  });

  it('rejects a catch-all before the specific routes it would shadow', () => {
    const fallback = physicalAppTsx.match(/<Route>\s*<Placeholder[\s\S]*?<\/Route>/)![0];
    const withoutFallback = replaceOnce(physicalAppTsx, fallback, '');
    const appTsx = replaceOnce(withoutFallback, '<Switch>', `<Switch>\n${fallback}`);
    expect(() => parse({ appTsx })).toThrow(/catch-all route must be last/);
  });

  it.each(['configure', 'intelligence'])('rejects a duplicate nav path in %s before the original entry', (section) => {
    const routesTs = replaceOnce(physicalRoutesTs, 'export const ROUTES: RouteDef[] = [',
      `export const ROUTES: RouteDef[] = [\n  { path: '/usage', label: 'Usage', section: '${section}', icon: Activity },`);
    expect(() => parse({ routesTs })).toThrow(/duplicate nav path \/usage/);
  });

  it('rejects duplicate section labels rather than silently keeping the last value', () => {
    const routesTs = replaceOnce(physicalRoutesTs, 'export const SECTION_LABEL: Record<RouteSection, string> = {',
      "export const SECTION_LABEL: Record<RouteSection, string> = {\n  workspace: 'Changed',");
    expect(() => parse({ routesTs })).toThrow(/duplicate section label workspace/);
  });

  it('ignores a commented import and reports the actual wrong page module', () => {
    const appTsx = replaceOnce(physicalAppTsx, "import { Usage } from '@/pages/Usage';",
      "import { Usage } from '@/pages/Settings';\n/*\nimport { Usage } from '@/pages/Usage';\n*/");
    expect(driftOf(parse({ appTsx }))).toEqual(['page module: /usage -> Usage changed to Settings']);
  });

  it('rejects actual duplicate page imports', () => {
    const appTsx = replaceOnce(physicalAppTsx, "import { Usage } from '@/pages/Usage';",
      "import { Usage } from '@/pages/Settings';\nimport { Usage } from '@/pages/Usage';");
    expect(() => parse({ appTsx })).toThrow(/duplicate page import Usage/);
  });

  it.each(['/*\nimport { Usage } from \'@/pages/Usage\';\n*/', "// import { Usage } from '@/pages/Usage';"])(
    'rejects a commented Usage import replaced by an inline placeholder: %s', (commentedImport) => {
      const appTsx = replaceOnce(physicalAppTsx, "import { Usage } from '@/pages/Usage';",
        `${commentedImport}\nconst Usage = () => <Placeholder title="Usage" />;`);
      expect(() => parse({ appTsx })).toThrow(/mounted component Usage has a reference outside/);
    },
  );

  it.each([
    'const Usage = Settings;',
    'const { Usage } = pages;',
    'function render(Usage: unknown) { return Usage; }',
    'Usage = Settings;',
  ])('rejects mounted page shadowing or reassignment: %s', (binding) => {
    const appTsx = replaceOnce(physicalAppTsx, 'export function App() {', `export function App() {\n  ${binding}`);
    expect(() => parse({ appTsx })).toThrow(/mounted component Usage has a reference outside/);
  });

  it('preserves quoted comment delimiters and ignores page names inside comments and strings', () => {
    const appTsx = `${physicalAppTsx}\nconst example = "https://example.test/Usage/*not a comment*/";\n// const Usage = Settings;\n/* function Usage() {} */`;
    expect(parse({ appTsx })).toEqual(parse());
  });

  it('fails closed on template interpolation in App source', () => {
    expect(() => parse({ appTsx: `${physicalAppTsx}\nconst example = \`\${Usage}\`;` }))
      .toThrow(/template literals are outside the supported source shape/);
  });

  it('fails closed on regexp quotes that could mask a local page binding', () => {
    const appTsx = replaceOnce(physicalAppTsx, 'export function App() {',
      "export function App() {\n  const before = /'/; const Usage = Settings; const after = /'/;");
    expect(() => parse({ appTsx })).toThrow(/non-JSX slash syntax is outside the supported source shape/);
  });

  it('rejects an empty Audit page even when source fingerprints no longer match', () => {
    expect(() => parse({ readPage: (module) => module === 'Audit'
      ? 'export function Audit() { return null; }' : physicalPage(module) }))
      .toThrow(/must retain \/api\/audit-log source evidence/);
  });

  it.each([
    "// useFetch('/api/audit-log');",
    "/* useFetch('/api/audit-log'); */",
  ])('rejects Audit endpoint evidence present only in a comment: %s', (comment) => {
    expect(() => parse({ readPage: (module) => module === 'Audit'
      ? `${comment}\nexport function Audit() { return null; }` : physicalPage(module) }))
      .toThrow(/must retain \/api\/audit-log source evidence/);
  });

  it('retains real Audit evidence after a string containing comment delimiters', () => {
    const snapshot = parse({ readPage: (module) => module === 'Audit'
      ? "const example = 'https://example.test/*audit*/';\nexport function Audit() { return useFetch('/api/audit-log'); }"
      : physicalPage(module) });
    expect(snapshot.placeholders).not.toContain('/audit');
  });

  it('rejects a route mounted outside the recognized Switch', () => {
    const appTsx = replaceOnce(physicalAppTsx, '</Switch>', '</Switch>\n<Route path="/outside"><Usage /></Route>');
    expect(() => parse({ appTsx })).toThrow(/outside the recognized/);
  });

  it('fails loudly when the ROUTES array cannot be found', () => {
    const routesTs = replaceOnce(physicalRoutesTs, 'export const ROUTES: RouteDef[]', 'export const NAV_ROUTES: RouteDef[]');
    expect(() => parse({ routesTs })).toThrow(/expected exactly one/);
  });
});

describe('route inventory contract', () => {
  const byPath = new Map(ROUTE_INVENTORY.map((entry) => [entry.path, entry]));
  const ownerOf = (entry: RouteInventoryEntry | undefined) =>
    entry?.requirement.disposition === 'retain' ? entry.requirement.migrationOwner : null;

  it('lists each path once', () => {
    expect(byPath.size).toBe(ROUTE_INVENTORY.length);
  });

  it('assigns every destination, alias, home redirect and 404 a migration owner, leaving only Jarvis undecided', () => {
    const owners: Partial<Record<MigrationOwner, string[]>> = {};
    for (const entry of ROUTE_INVENTORY) {
      const owner = ownerOf(entry);
      if (owner !== null) (owners[owner] ??= []).push(entry.path);
    }
    expect(owners).toEqual({
      589: ['/mission', '/scheduled', '/mobile', '/usage', '/audit', '/standup', '/settings', '*'],
      590: ['/work', '/convoy', '/agents', '/social', '/agents/:id', '/agents/:id/files', '/tasks'],
      592: ['/runs'],
      593: ['/cabinet', '/warroom'],
      594: ['/voices', '/talk'],
      595: ['/browser', '/ghost'],
      596: ['/memories', '/hive', '/hive-mind', '/hivemind', '/memory'],
      597: ['/teams', '/team'],
      713: ['/chat', '/'],
      716: ['/capabilities', '/gateway'],
    });
    expect(ROUTE_INVENTORY.filter((entry) => entry.requirement.disposition === 'undecided')).toEqual([
      expect.objectContaining({ path: '/jarvis', requirement: { disposition: 'undecided', decisionOwner: 721 } }),
    ]);
  });

  it('gives each alias the owner of the page it redirects to', () => {
    for (const entry of ROUTE_INVENTORY) {
      if (entry.kind !== 'alias') continue;
      const target = byPath.get(entry.target);
      expect(target?.kind, entry.path).toBe('nav-page');
      expect(ownerOf(entry), entry.path).toBe(ownerOf(target));
    }
  });

  it('keeps /mission as the observed home and records /chat as the future home owned by #713', () => {
    const home = ROUTE_INVENTORY.find((entry) => entry.kind === 'home');
    expect(home).toMatchObject({ target: '/mission', futureTarget: '/chat', requirement: { migrationOwner: 713 } });
    expect(ownerOf(byPath.get('/chat'))).toBe(713);
    expect(byPath.get('/mission')?.requirement).toEqual({ disposition: 'retain', preserve: 'shipped-page', migrationOwner: 589 });
    for (const baseline of Object.keys(LEGACY_BASELINES) as LegacyBaselineId[]) {
      expect(expectedLegacySnapshot(baseline).home).toBe('/mission');
    }
  });

  it('retains /work as a ledger destination migrated by #590 and enriched by #591', () => {
    expect(byPath.get('/work')).toMatchObject({
      kind: 'nav-page',
      requirement: { disposition: 'retain', preserve: 'shipped-page', migrationOwner: 590 },
      enrichmentOwner: 591,
    });
  });

  it('requires the real Audit page even where the published source is still a placeholder', () => {
    expect(byPath.get('/audit')?.requirement).toEqual({ disposition: 'retain', preserve: 'shipped-page', migrationOwner: 589 });
    expect(requirementGaps('local-phase0')).toEqual([]);
    expect(requirementGaps('origin-master')).toEqual(['/audit']);
  });

  it('keeps Social as a shipped page whose Postiz outage is a dated report, not a live health result', () => {
    expect(byPath.get('/social')).toMatchObject({
      observed: { 'origin-master': 'page', 'local-phase0': 'page' },
      requirement: { disposition: 'retain', preserve: 'shipped-page' },
      dependency: {
        service: 'Postiz',
        baselines: ['origin-master', 'local-phase0'],
        lastReported: { on: '2026-09-13', state: 'unreachable' },
        liveStatusEndpoint: '/api/social/status',
      },
    });
    expect(physicalPage('Social')).toContain("'/api/social/status'");
  });

  it('records the unresolved Voices POST auth degradation on both source baselines', () => {
    expect(byPath.get('/voices')).toMatchObject({
      dependencies: [{
        service: 'Voice start/stop/restart POST authentication',
        baselines: ['origin-master', 'local-phase0'],
        lastReported: {
          on: '2026-09-13', state: 'degraded',
          detail: expect.stringContaining('remains broken after Phase 0'),
          source: expect.stringContaining('Phase 0 closeout follow-up (a)'),
        },
      }],
    });
  });

  it('separates Audits required admin-token configuration from the missing published proxy', () => {
    expect(byPath.get('/audit')).toMatchObject({
      dependencies: [
        {
          service: 'Audit admin-token contract', baselines: ['origin-master', 'local-phase0'],
          lastReported: {
            on: '2026-09-13', state: 'required',
            detail: expect.stringContaining('DASHBOARD_ADMIN_TOKEN'),
            source: expect.stringContaining('section 7 P0-5'),
          },
        },
        {
          service: 'Hono /api/audit-log proxy', baselines: ['origin-master'],
          lastReported: {
            on: '2026-09-14', state: 'missing',
            source: expect.stringContaining('conductor source check at 918b60dc'),
          },
        },
      ],
    });
  });

  it('limits the reported Cabinet voice GET deadlock to the baseline without P0-1', () => {
    expect(byPath.get('/cabinet')).toMatchObject({
      dependencies: [{
        service: 'Cabinet voice GET authentication', baselines: ['origin-master'],
        lastReported: {
          on: '2026-09-14', state: 'degraded',
          detail: expect.stringContaining('GET fix in local Phase 0'),
          source: expect.stringContaining('conductor source check at 918b60dc'),
        },
      }],
    });
  });

  it('keeps complete dated reports with a compatible first-report alias and no implied health for other pages', () => {
    const reportedPaths: string[] = [];
    for (const entry of ROUTE_INVENTORY) {
      if (entry.kind !== 'nav-page') continue;
      if (!entry.dependencies) {
        expect(entry.dependency, entry.path).toBeUndefined();
        continue;
      }
      reportedPaths.push(entry.path);
      expect(entry.dependencies.length, entry.path).toBeGreaterThan(0);
      expect(entry.dependency, entry.path).toBe(entry.dependencies[0]);
      for (const report of entry.dependencies) {
        expect(report.service).not.toBe('');
        expect(report.lastReported.on).toMatch(/^\d{4}-\d{2}-\d{2}$/);
        expect(report.lastReported.source).toContain('PRDs/active/PRD-dashboard-v2-operator-shell-2026-09-13.md');
        expect(report.lastReported.detail).not.toBe('');
        expect(report.baselines.length).toBeGreaterThan(0);
        expect(new Set(report.baselines).size).toBe(report.baselines.length);
        for (const baseline of report.baselines) expect(LEGACY_BASELINES).toHaveProperty(baseline);
      }
    }
    expect(reportedPaths).toEqual(['/social', '/audit', '/cabinet', '/voices']);
  });

  it('carries the Standup page and 404 as truthful placeholders owned by #589', () => {
    expect(byPath.get('/standup')).toMatchObject({
      observed: { 'origin-master': 'placeholder', 'local-phase0': 'placeholder' },
      requirement: { disposition: 'retain', preserve: 'placeholder', migrationOwner: 589 },
    });
    expect(byPath.get('*')).toMatchObject({
      title: 'Not found',
      requirement: { disposition: 'retain', preserve: 'not-found', migrationOwner: 589 },
    });
  });

  it('stays framework-neutral: inventory modules import only each other', () => {
    const importsOf = (module: string) =>
      moduleSpecifiers(readRepoFile(`dashboard/web-next/src/routeInventory/${module}.ts`));
    expect({
      inventory: importsOf('inventory'),
      legacyBaselines: importsOf('legacyBaselines'),
      legacySource: importsOf('legacySource'),
    }).toEqual({ inventory: [], legacyBaselines: ['./inventory'], legacySource: ['./inventory'] });
  });

  it.each([
    "import 'react';",
    'import "react";',
    "import React from 'react';",
    'import { useState } from "react";',
    "export { useState } from 'react';",
    'export * from "react";',
    "const load = () => import('react');",
    'const load = () => import("react");',
  ])('detects a framework dependency regardless of import form: %s', (source) => {
    expect(moduleSpecifiers(`${source}\nimport type { LegacyBaselineId } from './inventory';`))
      .toEqual(['./inventory', 'react']);
  });

  it('ignores import examples in strings, comments and regular expressions', () => {
    expect(moduleSpecifiers(`const example = "import 'react';";
      // export * from 'react';
      const pattern = /import 'react'/;`)).toEqual([]);
  });

  it('rejects an uninspectable computed dynamic import', () => {
    expect(() => moduleSpecifiers('const load = () => import(moduleName);'))
      .toThrow(/must use a literal module specifier/);
  });
});
