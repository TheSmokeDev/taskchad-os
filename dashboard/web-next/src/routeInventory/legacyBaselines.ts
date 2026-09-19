import type { LegacyBaselineId } from './inventory';

export interface SourceFingerprint {
  path: string;
  gitBlob: string;
  /** SHA-256 of the file text with CRLF normalized to LF; the two baselines differ in committed line endings. */
  sha256Lf: string;
}

export interface SourceExcerpt {
  path: string;
  line: number;
  text: string;
}

export interface LegacyBaseline {
  id: LegacyBaselineId;
  commit: string;
  summary: string;
  sources: readonly SourceFingerprint[];
  /** Verbatim lines for observations that a clean clone of the published branch cannot inspect. */
  localOnlyEvidence: readonly SourceExcerpt[];
}

const ROUTES_TS = 'dashboard/web/src/lib/routes.ts';
const APP_TSX = 'dashboard/web/src/App.tsx';
const AUDIT_TSX = 'dashboard/web/src/pages/Audit.tsx';
const STANDUP_TSX = 'dashboard/web/src/pages/StandupConfig.tsx';
const JARVIS_TSX = 'dashboard/web/src/pages/Jarvis.tsx';

const SHARED_PAGE_SOURCES: readonly SourceFingerprint[] = [
  {
    path: STANDUP_TSX,
    gitBlob: '2bc65a7c165fc517d060abce0a3814fcffde9fa1',
    sha256Lf: '4ce13ad06c3be3a634b35d5b752d195b40eb5e5183832c23a0534334c7445fbe',
  },
  {
    path: JARVIS_TSX,
    gitBlob: '6ffaec89e0c042ac2eaf8ec4cefd7eee5afa88ba',
    sha256Lf: 'e791e891955262aef304bb9bf51ac60a6f0175b826a9eb5ef79c9e772dd58719',
  },
];

export const LEGACY_BASELINES: Readonly<Record<LegacyBaselineId, LegacyBaseline>> = {
  'origin-master': {
    id: 'origin-master',
    commit: '918b60dce3c697fddcdb9488128e70b919226143',
    summary: '22 nav entries, 24 mounted destinations; Jarvis page file present but unmounted; Audit is a placeholder.',
    sources: [
      {
        path: ROUTES_TS,
        gitBlob: 'da1421163d29357808588150f2a65cdb9be5327d',
        sha256Lf: 'a7efba8821f62bad341c581512ea28a13a4903e23b45166d0e9223ffea182e06',
      },
      {
        path: APP_TSX,
        gitBlob: '436c212772838a24905657c40766d72761abd2f9',
        sha256Lf: '7d418c3af17781f3ceb71aef63c58da0de60a0e5ef3792fbb60c55aef3cb96a9',
      },
      {
        path: AUDIT_TSX,
        gitBlob: '5780dd3eb07b64e18ca7f0ff57d3852f13debfdf',
        sha256Lf: 'cd967a1afea96b2c2b3967a4878777e763d7da20ba7d5bb33113a58a88a8e14d',
      },
      ...SHARED_PAGE_SOURCES,
    ],
    localOnlyEvidence: [],
  },
  'local-phase0': {
    id: 'local-phase0',
    commit: '352cbb4ca2557f3988c30d2793f4c570d391fc4a',
    summary:
      '23 nav entries, 25 mounted destinations; Jarvis mounted with a nav entry; Audit is a real audit-log page. ' +
      'Unpublished Phase 0 source, observed at checkout 728e3caa203dfe4e92b192dbc8c45d83b2476135.',
    sources: [
      {
        path: ROUTES_TS,
        gitBlob: 'b4c11ad6a1eda7f9d558587e254bd3c102a1cd72',
        sha256Lf: 'f42ead0ad8252dd3fa82af148d4e1848d0659d58356d011b6c0f2fa788dc573a',
      },
      {
        path: APP_TSX,
        gitBlob: '509ad6435e12ee56a6fb5d38c1fffab3d359eddb',
        sha256Lf: '0167ab46b282a94945a6593b5be1bfdf19bc6c5e8ec0be27f412e64d9b766395',
      },
      {
        path: AUDIT_TSX,
        gitBlob: '160236a7bb9b775021fc7fe1423afa11da7b4a5a',
        sha256Lf: '6396ba9ad4a04a968934b6fa429a0660426faebdce4273aa0015032776e53f63',
      },
      ...SHARED_PAGE_SOURCES,
    ],
    localOnlyEvidence: [
      {
        path: ROUTES_TS,
        line: 39,
        text: "  { path: '/jarvis',        label: 'Jarvis',          section: 'intelligence', icon: Radar,         shortcut: 'g j' },",
      },
      { path: APP_TSX, line: 21, text: "import { Jarvis } from '@/pages/Jarvis';" },
      { path: APP_TSX, line: 76, text: '          <Route path="/jarvis"><Jarvis /></Route>' },
      { path: AUDIT_TSX, line: 45, text: "  const { data, loading, error } = useFetch<AuditLogResponse>('/api/audit-log');" },
    ],
  },
};
