# Legacy Route Inventory (#714)

A typed contract listing every destination the legacy Preact Dashboard
(`dashboard/web`) exposes, with three facts about each one. First, what the
source actually shows in each known baseline. Second, what the React
migration must preserve. Third, which ticket owns that migration. #581 uses
it for its route-manifest test. The #589–#597, #713 and #716 route tickets use
it for their ownership.

This is an inventory only. It does not migrate routes, change
`dashboard/web`, mount Jarvis, or restore Audit.

## Files

| File | Contents |
|------|----------|
| `src/routeInventory/inventory.ts` | `ROUTE_INVENTORY`, `NAV_SECTIONS`, entry types, `requirementGaps()` |
| `src/routeInventory/legacyBaselines.ts` | The two known source baselines: commit, git blob IDs, LF-normalized SHA-256, local-only evidence lines |
| `src/routeInventory/legacySource.ts` | Text parser for `dashboard/web/src/lib/routes.ts` + `App.tsx`, and baseline matching |
| `src/__tests__/routeInventory.test.ts` | Physical drift check, evidence checks, drift-sensitivity cases, contract checks |

These modules import only each other: no React, Preact, router or icon
package. A test enforces this. The Preact `ROUTES` registry is read as text,
never imported.

## Observation vs requirement

Every entry keeps two things apart:

- `observed[baseline]` records what the physical source shows: `page`,
  `placeholder`, `redirect` or `absent`.
- `requirement` records what the migration owes:
  - `{ disposition: 'retain', preserve, migrationOwner }`, where `preserve` is
    `shipped-page`, `placeholder`, `redirect`, `home-redirect` or `not-found`;
  - or `{ disposition: 'undecided', decisionOwner: 721 }`.

Entry kinds are `nav-page` (with its legacy `section` and `label`),
`sub-page`, `alias`, `home` (`/`) and `fallback` (`*`, the 404).
Both page kinds carry `sourceModule`, the observed legacy module binding.
It does not override `requirement`: on `origin-master`, the `Audit` module
is a placeholder, while the migration still owes the real Audit capability.

Degradation and operational dependencies are recorded in `dependencies`,
separately from source observations and migration requirements. Each report
has a service/contract, applicable baselines, date, state, detail and source
citation; a live-status endpoint is optional. `dependency` remains a
compatibility alias for the first report. Consumers must use `dependencies`
to read all reports. An absent report means **no degradation recorded**, not
verified healthy. A mounted page and a retained capability are not health
verdicts.

| Destination | Date | Evidence | Applicability |
|-------------|------|----------|---------------|
| `/social` | 2026-09-13 | Postiz unreachable (`reachable:false, auth_ok:false`); PRD Problem Statement 3 | Both source baselines; operational report, not a current probe |
| `/voices` | 2026-09-13 | Start/stop/restart POSTs still send Bearer rejected by the Python voice prefix; PRD Phase 0 follow-up (a) | Both baselines; requires an explicit auth-contract decision under #594 |
| `/audit` | 2026-09-13 | Real capability requires `DASHBOARD_ADMIN_TOKEN`, with typed 503 when unset; PRD P0-5 | Both baselines; configuration requirement, current configuration unknown |
| `/audit` | 2026-09-14 | Missing Hono `/api/audit-log` proxy, checked against the published source; PRD structural-rot finding and P0-5 | Published `origin-master` only; #589 must deliver the real path |
| `/cabinet` | 2026-09-14 | Published source confirms the PRD's voice GET auth deadlock, Problem Statement 1 and P0-1 | Published `origin-master` only; local Phase 0 reports the GET fix |

The conductor confirmed the published Cabinet gap at `918b60dc`: the Hono
query-token allowlist omits voice status/session GETs and the `client.js`
proxy drops the query string. This is static baseline evidence, not a new
live health check. No auth or runtime changes are implemented by this ticket.

## Baselines

Both baselines have the same 7 aliases, `/` redirecting to
`DEFAULT_ROUTE = '/mission'`, and a `Not found` placeholder as the catch-all.

| Baseline | Source | Nav entries | Mounted destinations | Jarvis | Audit | Placeholders |
|----------|--------|-------------|----------------------|--------|-------|--------------|
| `origin-master` | `918b60dc` | 22 | 24 | page file present, not mounted | placeholder | `/audit`, `/standup` |
| `local-phase0` | `352cbb4c` (Phase 0, unpublished; observed at `728e3caa`) | 23 | 25 | mounted, Intelligence nav | real `/api/audit-log` page | `/standup` |

A clean clone of the published branch cannot inspect `local-phase0`. Its
evidence is therefore carried in `legacyBaselines.ts`:

- blob IDs (`routes.ts` `b4c11ad6`, `App.tsx` `509ad643`, `Audit.tsx` `160236a7`);
- LF-normalized hashes;
- the verbatim lines `routes.ts:39`, `App.tsx:21`, `App.tsx:76` and
  `Audit.tsx:45`.

The tests check that those lines parse to exactly the Jarvis nav/route
difference between the baselines. When the local bytes are present, the tests
also check that the lines sit at their recorded positions.

## Drift check

The test does four things:

1. It reads the physical `dashboard/web/src/lib/routes.ts`, `App.tsx` and each
   mounted page module.
2. It parses nav entries (path, label, section), section labels, mounted
   pages and their module bindings, placeholder pages, aliases, `DEFAULT_ROUTE`, the `/` redirect and the
   catch-all.
3. It builds each baseline's expected shape from `ROUTE_INVENTORY`'s
   observations. It then requires the parsed source to match **exactly one**
   baseline, and prints missing/unexpected/changed items against both.
4. It always requires the fingerprinted files to be byte-identical (after LF
   normalization) to exactly one recorded baseline, and requires that baseline
   to be the structural match. An unknown fingerprint fails; this check never
   skips merely because source changed. Router or pinned Audit/Standup/Jarvis edits
   require a reviewed baseline update, even if the bounded parser reports no
   structural difference.

The parser is strict. A route line it cannot read, a `<Route>` outside the
recognized `<Switch>` lines, a route definition outside `ROUTES`, or a missing
block throws. Duplicate nav paths, section labels and page imports also throw
instead of overwriting earlier entries. A non-placeholder Audit page must
always retain its `/api/audit-log` source evidence, even when source hashes
change. Comments cannot supply a page import or Audit endpoint evidence;
locally declared mounted page components are rejected.
The bounded App/Audit reader rejects template literals and multiline quoted
text; App also rejects regular-expression and division syntax. Adopting those
shapes requires an explicit reader update. The mandatory fingerprint gate is
the backstop for binding, lexical and post-declaration mutations the bounded
reader may not understand, including replacing the home redirect's imported
`DEFAULT_ROUTE`. Nav observations describe the `ROUTES` literal, not filtering
or ordering performed by Sidebar or other consumers.

This checks source structure, not rendered behavior. Placeholder detection
recognizes the existing `Placeholder` component; it cannot prove that an
arbitrary replacement component provides the shipped capability. An endpoint
reference also cannot prove that a request executes or succeeds. The migration
tickets must verify those behaviors. Drift-sensitivity cases change the real
physical text to prove each of these is detected:

- a removed page;
- an added page;
- an alias retargeted, removed or added;
- a redirect replaced by a page;
- `DEFAULT_ROUTE` switched to `/chat`;
- a nav regroup;
- a nav entry with no route;
- the 404 removed;
- the Standup placeholder turned into a page.
- an unrelated page mounted at an existing path;
- a catch-all moved before the specific routes it would shadow.
- duplicate nav paths, section labels and page imports;
- a commented page import replaced by an inline component;
- an empty Audit page without active endpoint evidence.

`Social.tsx` is not fingerprinted. Its endpoint assertion checks the recorded
`/api/social/status` string in source; a comment or unused string can satisfy
that bounded check. It does not prove an active fetch or current Postiz health.

When the legacy router changes on purpose, update the affected
`observed`/`requirement` fields. If the new source is a baseline that must be
recognized, record its commit, blob IDs and hashes through review. Even an edit
with no structural difference must update the baseline; never generate
expectations automatically from whatever source is present.

## Ownership

| Owner | Destinations |
|-------|--------------|
| #589 | `/mission`, `/scheduled`, `/mobile`, `/usage`, `/audit` (real capability), `/standup` (placeholder), `/settings`, 404 |
| #590 | `/work` (later ledger enrichment #591), `/convoy`, `/agents`, `/agents/:id`, `/agents/:id/files`, `/social`, alias `/tasks` |
| #592 | `/runs` |
| #593 | `/cabinet`, alias `/warroom` |
| #594 | `/voices`, `/talk` |
| #595 | `/browser`, `/ghost` |
| #596 | `/memories`, `/hive`, aliases `/memory`, `/hive-mind`, `/hivemind` |
| #597 | `/teams`, alias `/team` |
| #713 | `/chat`, the `/` home redirect |
| #716 | `/capabilities`, alias `/gateway` |
| #721 (decision only) | `/jarvis`: disposition undecided, no migration owner |

Each alias takes its target page's owner. `/standup` stays a truthful
placeholder. No standup feature and no keep/delete decision is implied.

## How #581 consumes it

- **Route manifest.** Import `ROUTE_INVENTORY` and compare the React router's
  registrations against the entries with `disposition: 'retain'`. Fail on a
  missing or unexpected registration. Keep `/jarvis` visible as undecided:
  neither register nor drop it on this inventory's authority.
- **Home.** `/` currently lands on `target` (`/mission`). It moves to
  `futureTarget` (`/chat`) only when #713 lands the chat-first home. `/mission`
  and `/work` stay reachable.
- **Grouped navigation.** `NAV_SECTIONS` plus each `nav-page` entry's
  `section`/`label` is the observed legacy grouping, and the starting point for
  #713's grouped navigation. Include only entries whose
  `requirement.disposition` is `retain`; exclude undecided Jarvis until #721
  records its decision. Within-section display order is not frozen by
  this inventory; #713 owns that presentation choice. The drift check compares
  membership and section assignment, not list order.
- **Legacy drift.** Keep `routeInventory.test.ts` running. That way the Preact
  router cannot change underneath the frozen manifest.

## Outstanding differences

Neither is implemented here.

- **Audit restoration.** `requirementGaps('origin-master')` returns `['/audit']`.
  On the published branch Audit is still a placeholder, because Phase 0 P0-5
  (the `/api/audit-log` Hono proxy and the real page) is not there. The
  requirement is the real capability. #589 must deliver it, not port the
  placeholder.
- **Jarvis mounting.** Phase 0 P0-4 mounted Jarvis locally only. The published
  branch has the page file but no route or nav entry. Either state is only an
  observation. #721 must record an explicit disposition before final route
  acceptance.

## Verify

```powershell
cd dashboard\web-next
npm ci
npm test
npm run typecheck
npm run build
```
