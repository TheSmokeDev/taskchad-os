import { NAV_SECTIONS, ROUTE_INVENTORY, type LegacyBaselineId } from './inventory';

export interface LegacyNavEntry {
  path: string;
  label: string;
  section: string;
}

export interface LegacyRouteSnapshot {
  sectionLabels: Readonly<Record<string, string>>;
  nav: readonly LegacyNavEntry[];
  pages: readonly string[];
  pageModules: Readonly<Record<string, string>>;
  placeholders: readonly string[];
  aliases: Readonly<Record<string, string>>;
  defaultRoute: string;
  home: string;
  fallback: string | null;
}

export type LegacyRouteLine =
  | { kind: 'page'; path: string; component: string }
  | { kind: 'redirect'; path: string; to: string }
  | { kind: 'default-redirect'; path: string };

export type BaselineMatch =
  | { matched: LegacyBaselineId; drift: null }
  | { matched: null; drift: Readonly<Record<LegacyBaselineId, readonly string[]>> };

const lines = (text: string) => text.split(/\r?\n/);

/** Bounded lexical masking for the App/Audit shapes pinned by this inventory. */
function sourceEvidence(source: string, file: string): { withoutComments: string; code: string } {
  const blank = (text: string) => text.replace(/[^\r\n]/g, ' ');
  let withoutComments = '';
  let code = '';
  for (let at = 0; at < source.length;) {
    const from = at;
    if (source.startsWith('//', at)) {
      const newline = source.indexOf('\n', at + 2);
      at = newline < 0 ? source.length : newline;
      const masked = blank(source.slice(from, at));
      withoutComments += masked;
      code += masked;
    } else if (source.startsWith('/*', at)) {
      const end = source.indexOf('*/', at + 2);
      if (end < 0) throw new Error(`${file}: unterminated block comment`);
      at = end + 2;
      const masked = blank(source.slice(from, at));
      withoutComments += masked;
      code += masked;
    } else if (source[at] === '"' || source[at] === "'") {
      const quote = source[at++];
      while (at < source.length && source[at] !== quote) {
        if (source[at] === '\\') at += 2;
        else if (source[at] === '\n' || source[at] === '\r') {
          throw new Error(`${file}: unsupported multiline quoted text`);
        } else at++;
      }
      if (at >= source.length) throw new Error(`${file}: unterminated quoted text`);
      at++;
      const literal = source.slice(from, at);
      withoutComments += literal;
      code += blank(literal);
    } else if (source[at] === '`') {
      // Interpolation can hide declarations/comments. Extend the reader explicitly
      // if either pinned source adopts it instead of pretending to parse TSX.
      throw new Error(`${file}: template literals are outside the supported source shape`);
    } else if (file === 'App.tsx' && source[at] === '/' && source[at - 1] !== '<' && source[at + 1] !== '>') {
      // App has JSX closing tags but no division/regular-expression literals.
      // Do not mistake quotes in a newly introduced regexp for string bounds.
      throw new Error('App.tsx: non-JSX slash syntax is outside the supported source shape');
    } else {
      withoutComments += source[at];
      code += source[at++];
    }
  }
  return { withoutComments, code };
}

function block(source: string, start: string, end: string, file: string): string {
  const from = source.indexOf(start);
  if (from < 0 || source.indexOf(start, from + 1) >= 0) {
    throw new Error(`${file}: expected exactly one "${start}"`);
  }
  const to = source.indexOf(end, from + start.length);
  if (to < 0) throw new Error(`${file}: "${start}" is not closed by "${end}"`);
  return source.slice(from + start.length, to);
}

export function parseNavLine(line: string): LegacyNavEntry | null {
  const match = /^\s*\{\s*path:\s*'([^']+)',\s*label:\s*'([^']+)',\s*section:\s*'([^']+)',\s*icon:\s*\w+\s*(?:,\s*shortcut:\s*'[^']*'\s*)?\},?\s*$/.exec(line);
  return match ? { path: match[1], label: match[2], section: match[3] } : null;
}

export function parseRouteLine(line: string): LegacyRouteLine | null {
  const trimmed = line.trim();
  let match = /^<Route path="([^"]+)"><(\w+) \/><\/Route>$/.exec(trimmed);
  if (match) return { kind: 'page', path: match[1], component: match[2] };
  match = /^<Route path="([^"]+)" component=\{(\w+)\} \/>$/.exec(trimmed);
  if (match) return { kind: 'page', path: match[1], component: match[2] };
  match = /^<Route path="([^"]+)"><Redirect to="([^"]+)" \/><\/Route>$/.exec(trimmed);
  if (match) return { kind: 'redirect', path: match[1], to: match[2] };
  match = /^<Route path="([^"]+)"><Redirect to=\{DEFAULT_ROUTE\} \/><\/Route>$/.exec(trimmed);
  if (match) return { kind: 'default-redirect', path: match[1] };
  return null;
}

export function parsePageImportLine(line: string): { component: string; module: string } | null {
  const match = /^import \{ (\w+) \} from '@\/pages\/(\w+)';$/.exec(line.trim());
  return match ? { component: match[1], module: match[2] } : null;
}

function isPlaceholderPage(source: string): boolean {
  return /import \{ Placeholder \} from '(?:\.\/|@\/pages\/)Placeholder';|<Placeholder(?:\s|\/?>)/.test(source);
}

function parseRoutesTs(routesTs: string) {
  const navBlock = block(routesTs, 'export const ROUTES: RouteDef[] = [', '];', 'routes.ts');
  const nav = lines(navBlock)
    .filter((line) => line.trim() !== '' && !line.trim().startsWith('//'))
    .map((line) => {
      const entry = parseNavLine(line);
      if (!entry) throw new Error(`routes.ts: unrecognized ROUTES line: ${line.trim()}`);
      return entry;
    });
  const navPaths = new Set<string>();
  for (const entry of nav) {
    if (navPaths.has(entry.path)) throw new Error(`routes.ts: duplicate nav path ${entry.path}`);
    navPaths.add(entry.path);
  }
  if ((routesTs.match(/\{[ \t]*path:/g) ?? []).length !== nav.length) {
    throw new Error('routes.ts: a route definition exists outside the ROUTES array');
  }

  const labelBlock = block(routesTs, 'export const SECTION_LABEL: Record<RouteSection, string> = {', '};', 'routes.ts');
  const sectionLabels: Record<string, string> = {};
  for (const line of lines(labelBlock).filter((line) => line.trim() !== '')) {
    const match = /^\s*(\w+):\s*'([^']+)',?\s*$/.exec(line);
    if (!match) throw new Error(`routes.ts: unrecognized SECTION_LABEL line: ${line.trim()}`);
    if (Object.hasOwn(sectionLabels, match[1])) throw new Error(`routes.ts: duplicate section label ${match[1]}`);
    sectionLabels[match[1]] = match[2];
  }

  const defaults = [...routesTs.matchAll(/^export const DEFAULT_ROUTE = '([^']+)';\s*$/gm)];
  if (defaults.length !== 1) throw new Error('routes.ts: expected exactly one DEFAULT_ROUTE literal');

  return { nav, sectionLabels, defaultRoute: defaults[0][1] };
}

function parseAppTsx(appTsx: string) {
  const evidence = sourceEvidence(appTsx, 'App.tsx');
  appTsx = evidence.withoutComments;
  // A JSX comment becomes an empty expression after lexical masking.
  const switchBlock = block(appTsx, '<Switch>', '</Switch>', 'App.tsx').replace(/\{\s*\}/g, '');
  const fallbackPattern = /<Route>\s*<Placeholder\s+title="([^"]+)"[^>]*\/>\s*<\/Route>/g;
  const fallbacks = [...switchBlock.matchAll(fallbackPattern)];
  if (fallbacks.length > 1) throw new Error('App.tsx: more than one catch-all route');
  if (fallbacks.length === 1) {
    const fallback = fallbacks[0];
    const afterFallback = switchBlock.slice(fallback.index! + fallback[0].length).trim();
    if (afterFallback) throw new Error('App.tsx: catch-all route must be last in Switch');
  }

  const routeLines = lines(switchBlock.replace(fallbackPattern, ''))
    .filter((line) => line.trim() !== '')
    .map((line) => {
      const route = parseRouteLine(line);
      if (!route) throw new Error(`App.tsx: unrecognized route line: ${line.trim()}`);
      return route;
    });
  if ((appTsx.match(/<Route\b/g) ?? []).length !== routeLines.length + fallbacks.length) {
    throw new Error('App.tsx: a <Route> exists outside the recognized <Switch> lines');
  }

  const seen = new Set<string>();
  for (const route of routeLines) {
    if (seen.has(route.path)) throw new Error(`App.tsx: duplicate route ${route.path}`);
    seen.add(route.path);
  }

  const pageModules = new Map<string, string>();
  const importLines = new Set<number>();
  for (const [index, line] of lines(appTsx).entries()) {
    const pageImport = parsePageImportLine(line);
    if (pageImport) {
      if (pageModules.has(pageImport.component)) throw new Error(`App.tsx: duplicate page import ${pageImport.component}`);
      pageModules.set(pageImport.component, pageImport.module);
      importLines.add(index);
    }
  }

  const outsideBindings = lines(evidence.code)
    .filter((_, index) => !importLines.has(index)).join('\n')
    .replace(/<Switch>[\s\S]*?<\/Switch>/, '');
  for (const route of routeLines) {
    if (route.kind === 'page' && new RegExp(`\\b${route.component}\\b`).test(outsideBindings)) {
      throw new Error(`App.tsx: mounted component ${route.component} has a reference outside its page import and recognized routes`);
    }
  }

  return { routeLines, pageModules, fallback: fallbacks[0]?.[1] ?? null };
}

/** Reads the legacy Preact router source as text; `readPage` receives a module name under `src/pages`. */
export function parseLegacyRouteSource(
  routesTs: string,
  appTsx: string,
  readPage: (module: string) => string,
): LegacyRouteSnapshot {
  const { nav, sectionLabels, defaultRoute } = parseRoutesTs(routesTs);
  const { routeLines, pageModules, fallback } = parseAppTsx(appTsx);

  const pages: string[] = [];
  const mountedModules: Record<string, string> = {};
  const placeholders: string[] = [];
  const aliases: Record<string, string> = {};
  let home: string | null = null;

  for (const route of routeLines) {
    if (route.kind === 'page') {
      const module = pageModules.get(route.component);
      if (!module) throw new Error(`App.tsx: ${route.path} renders ${route.component}, which is not imported from @/pages`);
      pages.push(route.path);
      mountedModules[route.path] = module;
      const rawPageSource = readPage(module);
      const pageSource = module === 'Audit'
        ? sourceEvidence(rawPageSource, 'Audit.tsx').withoutComments : rawPageSource;
      if (isPlaceholderPage(pageSource)) {
        placeholders.push(route.path);
      } else if (module === 'Audit' && !/['"]\/api\/audit-log['"]/.test(pageSource)) {
        throw new Error('Audit.tsx: non-placeholder page must retain /api/audit-log source evidence');
      }
    } else if (route.path === '/') {
      home = route.kind === 'default-redirect' ? defaultRoute : route.to;
    } else if (route.kind === 'redirect') {
      aliases[route.path] = route.to;
    } else {
      throw new Error(`App.tsx: ${route.path} redirects to DEFAULT_ROUTE; only "/" may`);
    }
  }
  if (home === null) throw new Error('App.tsx: no "/" redirect');

  return {
    sectionLabels,
    nav: [...nav].sort((a, b) => a.path.localeCompare(b.path)),
    pages: pages.sort(),
    pageModules: mountedModules,
    placeholders: placeholders.sort(),
    aliases,
    defaultRoute,
    home,
    fallback,
  };
}

/** The legacy router shape that the inventory's per-baseline observations describe. */
export function expectedLegacySnapshot(baseline: LegacyBaselineId): LegacyRouteSnapshot {
  const nav: LegacyNavEntry[] = [];
  const pages: string[] = [];
  const pageModules: Record<string, string> = {};
  const placeholders: string[] = [];
  const aliases: Record<string, string> = {};
  let home = '';
  let fallback: string | null = null;

  for (const entry of ROUTE_INVENTORY) {
    const observed = entry.observed[baseline];
    if (observed === 'absent') continue;
    switch (entry.kind) {
      case 'nav-page':
        nav.push({ path: entry.path, label: entry.label, section: entry.section });
        pages.push(entry.path);
        pageModules[entry.path] = entry.sourceModule;
        if (observed === 'placeholder') placeholders.push(entry.path);
        break;
      case 'sub-page':
        pages.push(entry.path);
        pageModules[entry.path] = entry.sourceModule;
        break;
      case 'alias':
        aliases[entry.path] = entry.target;
        break;
      case 'home':
        home = entry.target;
        break;
      case 'fallback':
        fallback = entry.title;
        break;
    }
  }

  return {
    sectionLabels: Object.fromEntries(NAV_SECTIONS.map((section) => [section.id, section.label])),
    nav: nav.sort((a, b) => a.path.localeCompare(b.path)),
    pages: pages.sort(),
    pageModules,
    placeholders: placeholders.sort(),
    aliases,
    defaultRoute: home,
    home,
    fallback,
  };
}

function diffSets(category: string, expected: readonly string[], actual: readonly string[]): string[] {
  return [
    ...expected.filter((item) => !actual.includes(item)).map((item) => `${category}: missing ${item}`),
    ...actual.filter((item) => !expected.includes(item)).map((item) => `${category}: unexpected ${item}`),
  ];
}

function diffRecords(category: string, expected: Readonly<Record<string, string>>, actual: Readonly<Record<string, string>>): string[] {
  const keys = [...new Set([...Object.keys(expected), ...Object.keys(actual)])].sort();
  return keys.flatMap((key) => {
    if (!(key in actual)) return [`${category}: missing ${key} -> ${expected[key]}`];
    if (!(key in expected)) return [`${category}: unexpected ${key} -> ${actual[key]}`];
    return expected[key] === actual[key] ? [] : [`${category}: ${key} -> ${expected[key]} changed to ${actual[key]}`];
  });
}

function diffLegacySnapshots(expected: LegacyRouteSnapshot, actual: LegacyRouteSnapshot): string[] {
  const navRecord = (nav: readonly LegacyNavEntry[]) =>
    Object.fromEntries(nav.map((entry) => [entry.path, `${entry.section} "${entry.label}"`]));
  const scalar = (category: string, want: string | null, got: string | null) =>
    want === got ? [] : [`${category}: ${want ?? 'none'} changed to ${got ?? 'none'}`];

  return [
    ...diffRecords('nav section label', expected.sectionLabels, actual.sectionLabels),
    ...diffRecords('nav', navRecord(expected.nav), navRecord(actual.nav)),
    ...diffSets('page', expected.pages, actual.pages),
    ...diffRecords('page module', expected.pageModules, actual.pageModules),
    ...diffSets('placeholder', expected.placeholders, actual.placeholders),
    ...diffRecords('alias', expected.aliases, actual.aliases),
    ...scalar('default route', expected.defaultRoute, actual.defaultRoute),
    ...scalar('home redirect', expected.home, actual.home),
    ...scalar('fallback', expected.fallback, actual.fallback),
  ];
}

export function matchLegacyBaseline(actual: LegacyRouteSnapshot): BaselineMatch {
  const drift = {
    'origin-master': diffLegacySnapshots(expectedLegacySnapshot('origin-master'), actual),
    'local-phase0': diffLegacySnapshots(expectedLegacySnapshot('local-phase0'), actual),
  };
  const matched = (Object.keys(drift) as LegacyBaselineId[]).find((id) => drift[id].length === 0);
  return matched ? { matched, drift: null } : { matched: null, drift };
}
