export type NavSection = 'workspace' | 'intelligence' | 'collaborate' | 'configure';

export type LegacyBaselineId = 'origin-master' | 'local-phase0';

export type MigrationOwner = 589 | 590 | 592 | 593 | 594 | 595 | 596 | 597 | 713 | 716;

/** Observed source state, keyed by the legacy baseline it was read from. */
export type Observed<State> = Readonly<Record<LegacyBaselineId, State>>;

export interface Retain<Preserve extends string> {
  disposition: 'retain';
  preserve: Preserve;
  migrationOwner: MigrationOwner;
}

/** No keep/delete instruction exists; the decision owner must record one before cutover. */
export interface Undecided {
  disposition: 'undecided';
  decisionOwner: 721;
}

/** Dated source evidence, not a live health result or proof of a configured deployment. */
export interface DependencyReport {
  service: string;
  /** Baselines to which the cited report applies; no report means unrecorded, not healthy. */
  baselines: readonly LegacyBaselineId[];
  lastReported: {
    on: string;
    state: 'unreachable' | 'degraded' | 'required' | 'missing';
    detail: string;
    source: string;
  };
  /** Where the page reads live status; the inventory itself performs no health check. */
  liveStatusEndpoint?: string;
}

export interface NavPageEntry {
  kind: 'nav-page';
  path: string;
  sourceModule: string;
  label: string;
  section: NavSection;
  observed: Observed<'page' | 'placeholder' | 'absent'>;
  requirement: Retain<'shipped-page' | 'placeholder'> | Undecided;
  enrichmentOwner?: 591;
  /** Complete recorded dependency/degradation evidence. Absence is not a health verdict. */
  dependencies?: readonly DependencyReport[];
  /** Compatibility alias for the first report; use dependencies for the complete list. */
  dependency?: DependencyReport;
}

export interface SubPageEntry {
  kind: 'sub-page';
  path: string;
  sourceModule: string;
  parent: string;
  observed: Observed<'page' | 'absent'>;
  requirement: Retain<'shipped-page'>;
}

export interface AliasEntry {
  kind: 'alias';
  path: string;
  target: string;
  observed: Observed<'redirect' | 'absent'>;
  requirement: Retain<'redirect'>;
}

export interface HomeEntry {
  kind: 'home';
  path: '/';
  target: string;
  futureTarget: string;
  observed: Observed<'redirect'>;
  requirement: Retain<'home-redirect'>;
}

export interface FallbackEntry {
  kind: 'fallback';
  path: '*';
  title: string;
  observed: Observed<'placeholder'>;
  requirement: Retain<'not-found'>;
}

export type RouteInventoryEntry = NavPageEntry | SubPageEntry | AliasEntry | HomeEntry | FallbackEntry;

export const NAV_SECTIONS: readonly { id: NavSection; label: string }[] = [
  { id: 'workspace', label: 'Workspace' },
  { id: 'intelligence', label: 'Intelligence' },
  { id: 'collaborate', label: 'Collaborate' },
  { id: 'configure', label: 'Configure' },
];

const both = <State extends string>(state: State): Observed<State> => ({ 'origin-master': state, 'local-phase0': state });

const retain = <Preserve extends string>(preserve: Preserve, migrationOwner: MigrationOwner): Retain<Preserve> => ({
  disposition: 'retain',
  preserve,
  migrationOwner,
});

const page = (
  path: string,
  label: string,
  section: NavSection,
  migrationOwner: MigrationOwner,
  sourceModule: string,
): NavPageEntry => ({
  kind: 'nav-page',
  path,
  sourceModule,
  label,
  section,
  observed: both('page'),
  requirement: retain('shipped-page', migrationOwner),
});

const alias = (path: string, target: string, migrationOwner: MigrationOwner): AliasEntry => ({
  kind: 'alias',
  path,
  target,
  observed: both('redirect'),
  requirement: retain('redirect', migrationOwner),
});

const withDependencies = (entry: NavPageEntry, dependencies: readonly DependencyReport[]): NavPageEntry => ({
  ...entry,
  dependencies,
  dependency: dependencies[0],
});

const PRD_SOURCE = 'PRDs/active/PRD-dashboard-v2-operator-shell-2026-09-13.md';
const BOTH_BASELINES: readonly LegacyBaselineId[] = ['origin-master', 'local-phase0'];

/** Destination and section membership contract; within-section display order is owned by #713. */
export const ROUTE_INVENTORY: readonly RouteInventoryEntry[] = [
  page('/mission', 'Homie Dashboard', 'workspace', 589, 'MissionControl'),
  { ...page('/work', 'Work Queue', 'workspace', 590, 'WorkQueue'), enrichmentOwner: 591 },
  page('/convoy', 'Convoy', 'workspace', 590, 'Convoy'),
  page('/runs', 'Runs', 'workspace', 592, 'Runs'),
  page('/scheduled', 'Scheduled', 'workspace', 589, 'Scheduled'),
  page('/agents', 'Agents', 'workspace', 590, 'Agents'),
  page('/chat', 'Chat', 'workspace', 713, 'Chat'),
  page('/browser', 'Browser Viewer', 'workspace', 595, 'BrowserViewer'),
  page('/ghost', 'Ghost Phone', 'workspace', 595, 'GhostViewer'),
  withDependencies(page('/social', 'Social', 'workspace', 590, 'Social'), [
    {
      service: 'Postiz',
      baselines: BOTH_BASELINES,
      lastReported: {
        on: '2026-09-13',
        state: 'unreachable',
        detail: 'reachable:false, auth_ok:false (operational, not code)',
        source: `${PRD_SOURCE}, Problem Statement item 3`,
      },
      liveStatusEndpoint: '/api/social/status',
    },
  ]),
  page('/mobile', 'Mobile Access', 'workspace', 589, 'MobileAccess'),

  page('/memories', 'Memories', 'intelligence', 596, 'Memories'),
  page('/hive', 'Knowledge Graph', 'intelligence', 596, 'HiveMind'),
  page('/usage', 'Usage', 'intelligence', 589, 'Usage'),
  {
    kind: 'nav-page',
    path: '/jarvis',
    sourceModule: 'Jarvis',
    label: 'Jarvis',
    section: 'intelligence',
    observed: { 'origin-master': 'absent', 'local-phase0': 'page' },
    requirement: { disposition: 'undecided', decisionOwner: 721 },
  },
  page('/capabilities', 'Capabilities', 'intelligence', 716, 'CapabilityGateway'),
  withDependencies({
    ...page('/audit', 'Audit', 'intelligence', 589, 'Audit'),
    observed: { 'origin-master': 'placeholder', 'local-phase0': 'page' },
  }, [
    {
      service: 'Audit admin-token contract',
      baselines: BOTH_BASELINES,
      lastReported: {
        on: '2026-09-13',
        state: 'required',
        detail: 'The required real Audit capability needs DASHBOARD_ADMIN_TOKEN; without it the API returns typed 503. Configuration was not checked.',
        source: `${PRD_SOURCE}, section 7 P0-5`,
      },
    },
    {
      service: 'Hono /api/audit-log proxy',
      baselines: ['origin-master'],
      lastReported: {
        on: '2026-09-14',
        state: 'missing',
        detail: 'The published baseline has no /api/audit-log proxy in dashboard/server/src; Phase 0 records its addition. This is a source gap, not a live probe.',
        source: `${PRD_SOURCE}, Problem Statement structural rot and section 7 P0-5; conductor source check at 918b60dce3c697fddcdb9488128e70b919226143, dashboard/server/src`,
      },
    },
  ]),

  withDependencies(page('/cabinet', 'Cabinet', 'collaborate', 593, 'Cabinet'), [
    {
      service: 'Cabinet voice GET authentication',
      baselines: ['origin-master'],
      lastReported: {
        on: '2026-09-14',
        state: 'degraded',
        detail: 'The published baseline lacks P0-1: Hono rejects voice GET query-token paths and client.js forwarding drops the query string. The PRD reports the GET fix in local Phase 0; no live health check was performed.',
        source: `${PRD_SOURCE}, Problem Statement item 1 and section 7 P0-1/Phase 0 closeout; conductor source check at 918b60dce3c697fddcdb9488128e70b919226143, dashboard/server/src/middleware/auth.ts and dashboard/server/src/routes/cabinet.ts`,
      },
    },
  ]),
  page('/teams', 'Operating Room', 'collaborate', 597, 'Teams'),
  withDependencies(page('/voices', 'Voices', 'collaborate', 594, 'Voices'), [
    {
      service: 'Voice start/stop/restart POST authentication',
      baselines: BOTH_BASELINES,
      lastReported: {
        on: '2026-09-13',
        state: 'degraded',
        detail: 'Start/stop/restart POST actions send Bearer, which the Python voice prefix rejects on tokened deployments; this remains broken after Phase 0. The compatible auth contract is undecided.',
        source: `${PRD_SOURCE}, section 7 Phase 0 closeout follow-up (a); PRDs/active/PRD-dashboard-v2-operator-shell-2026-09-13.architecture.md, S2 and Voice POST authentication open question`,
      },
    },
  ]),
  page('/talk', 'Talk', 'collaborate', 594, 'Talk'),
  {
    ...page('/standup', 'Standup', 'collaborate', 589, 'StandupConfig'),
    observed: both('placeholder'),
    requirement: retain('placeholder', 589),
  },

  page('/settings', 'Settings', 'configure', 589, 'Settings'),

  { kind: 'sub-page', path: '/agents/:id', sourceModule: 'AgentDetail', parent: '/agents', observed: both('page'), requirement: retain('shipped-page', 590) },
  { kind: 'sub-page', path: '/agents/:id/files', sourceModule: 'AgentFiles', parent: '/agents', observed: both('page'), requirement: retain('shipped-page', 590) },

  alias('/hive-mind', '/hive', 596),
  alias('/hivemind', '/hive', 596),
  alias('/memory', '/memories', 596),
  alias('/tasks', '/work', 590),
  alias('/team', '/teams', 597),
  alias('/gateway', '/capabilities', 716),
  alias('/warroom', '/cabinet', 593),

  {
    kind: 'home',
    path: '/',
    target: '/mission',
    futureTarget: '/chat',
    observed: both('redirect'),
    requirement: retain('home-redirect', 713),
  },
  {
    kind: 'fallback',
    path: '*',
    title: 'Not found',
    observed: both('placeholder'),
    requirement: retain('not-found', 589),
  },
];

/** Retained shipped pages whose observed source in this baseline is not a real page. */
export function requirementGaps(baseline: LegacyBaselineId): string[] {
  return ROUTE_INVENTORY.filter(
    (entry) =>
      (entry.kind === 'nav-page' || entry.kind === 'sub-page') &&
      entry.requirement.disposition === 'retain' &&
      entry.requirement.preserve === 'shipped-page' &&
      entry.observed[baseline] !== 'page',
  ).map((entry) => entry.path);
}
