# Intentional Deviations — dashboard/web

**Purpose.** This file documents URLs and affordances from the donor ClaudeClaw
upstream (`~/.refs/claudeclaw-os/`) that the Phase 3 port
INTENTIONALLY drops or rewires, with a one-line rationale per entry.

R3 corrected scope (owner 2026-05-06): Phase 3 PORTS donor pages, it does
not strip them. Every dead button gets a backing endpoint OR is documented
here as an intentional deviation. The donor route manifest test
(`dashboard/web/src/__tests__/donor-route-manifest.test.ts`) scans every
`/api/` literal in `dashboard/web/src/**/*.{ts,tsx}` and asserts each one
is EITHER present in `ROUTE_MANIFEST` (exported from
`dashboard/server/src/routes.ts`) OR documented in this file. Adding a
literal that is neither will fail the test.

This file was renamed from `STRIPPED_AFFORDANCES.md` per the R3 corrected
scope — Phase 3's stripping is minimal, mostly ADD.

## Intentional URL drops

### `/api/agents/create`

- **Donor reference:** `~/.refs/claudeclaw-os/web/src/pages/Agents.tsx:444`
  uses `apiPost('/api/agents/create', ...)` for the agent-create wizard.
- **Why dropped:** Pure URL alias of canonical `POST /api/agents` — no
  functional difference. The donor exposed both for client convenience.
- **Replacement:** every wizard create call routes through canonical
  `POST /api/agents`. `dashboard/web/src/lib/api.ts` MUST NOT contain
  `/api/agents/create` as a literal (the manifest test enforces this).
- **Verified:** R3 dashboard-spec criterion
  `donor_create_alias_dropped_in_favor_of_canonical_post` (threshold 7).

## Intentional UI affordance drops

### `AgentSuggestions` component (and `useAgentSuggestions` hook)

- **Donor reference:** `web/src/components/AgentSuggestions.tsx`,
  `web/src/pages/Agents.tsx:11,37,40-69,116-153,180-181,199-204`.
- **Why dropped:** Per `PRPs/planning/PRD-8-claudeclaw-frontend-swap-phase-3-analysis.md` §10.
  WS3 keeps the `/api/agents/suggestions` endpoint (returns static rotation),
  but the donor's AI-generated "spin off this agent" wizard flow + Haiku
  scan pipeline is out of scope for Phase 3.
- **Replacement:** none for Phase 3. The endpoint stays mounted; the UI
  affordance ships in a later phase if/when YourBusiness multi-persona drives
  the need.

### Donor `useFetch.ts` module-level `Map<string, unknown>` cache

- **Donor reference:** `web/src/lib/useFetch.ts:16` (`const _cache = new Map<...>()`).
- **Why dropped:** Rule 2 violation (module-level mutable cache surviving
  across requests = stale-state class-of-bug per `MEMORY.md → Reference →
  Global Rules`). The donor pattern works for ClaudeClaw because their
  hot-paint UX is measured at navigation — but the same shape silently
  caches stale persona/agent state when one tab edits and another tab reads.
- **Replacement:** `useFetch` ports the same hook signature without the
  cache. First paint flashes a spinner instead of stale data. Test
  `anti-patterns.test.tsx` greps the source for module-level
  `new Map(...)` and asserts zero matches under `src/lib/`.

### Donor `personalization.ts` (most of it)

- **Donor reference:** `web/src/lib/personalization.ts` — workspaceName,
  collapsedSections, hotkeyMod, missionColumnOrder, missionColumnWidths.
- **Why simplified:** Phase 3 ships a thin port — the dashboard reads
  `/api/dashboard/settings` for sidebar collapsed sections and hotkey
  mod, but mission column ordering / widths are deferred. Donor settings
  that don't have a backing route in WS3's locked surface are local-only
  (signal-backed, no PATCH).
- **Replacement:** `lib/sidebar-prefs.ts` (collapsed sections + hotkey mod
  via `/api/dashboard/settings`). Mission column state is in-memory only.

### Donor `WorkspaceSwitcher` component

- **Donor reference:** `web/src/components/WorkspaceSwitcher.tsx` — donor
  multi-workspace switcher.
- **Why simplified:** The Homie has ONE operator + ONE backing framework
  — no multi-workspace concept. Replaced with a simple branded header.

### Donor `PrivacyToggle` component

- **Donor reference:** `web/src/components/PrivacyToggle.tsx` — donor
  privacy mode toggle (blurs cost/usage figures).
- **Why dropped:** No backing route in WS3, and the cost-blurring use-case
  is already handled by `theme.showCosts` which the WS3 settings route
  exposes.

## Intentional rename

### Donor `WarRoom.tsx` → `Cabinet.tsx`

- **Donor reference:** `web/src/pages/WarRoom.tsx`, registered at
  `/warroom` route.
- **Why renamed:** Q-naming lock per PRP-prd-8-phase-3-dashboard-port.md
  R7 ADOPT-WITH-FIXES — the framework names this surface "Cabinet" not
  "War Room." The route is `/cabinet`. The page is a placeholder in
  Phase 3 (full cabinet voice room ships in Phase 5+).
