/**
 * Hono ROUTE_MANIFEST — the single source of truth for every `/api/*`
 * path the dashboard server proxies.
 *
 * Consumed by:
 *   - dashboard/web/src/__tests__/donor-route-manifest.test.ts
 *     scans `dashboard/web/src/**\/*.{ts,tsx}` for `/api/` literals and
 *     asserts each one resolves to either a manifest entry or an entry
 *     in `dashboard/web/INTENTIONAL_DEVIATIONS.md`.
 *
 * Maintenance contract (R3 NB1):
 *   - Add a new entry whenever a new route is mounted in any file under
 *     `dashboard/server/src/routes/`.
 *   - Path parameters use the colon-style Hono prefix (`/:id`) — the
 *     manifest test normalizes both `${...}` and `:id` shapes to `:id`
 *     before checking membership.
 *   - The list is kept in declaration order across files: health, agents,
 *     conversation, scheduled, memories, hive-mind, settings, mission.
 *   - Mission proxy routes are mounted as wildcards on the Hono side
 *     (`/api/convoy/*` etc) — those framework endpoints are documented
 *     in `docs/mc-profile-contract.md` § 3.2 but the wildcards are listed
 *     here as a single literal so the donor-route-manifest test can
 *     match every concrete sub-path against the wildcard.
 *
 * What this file is NOT:
 *   - It is NOT runtime — `app.ts` mounts the route modules directly.
 *   - It is NOT a Hono router — exporting a `Hono` instance from here
 *     would create two parallel mounts and bypass `app.ts` ordering.
 *   - It is NOT the authoritative URL doc — `docs/mc-profile-contract.md`
 *     is the public contract; this file is the test-time invariant.
 */

export const ROUTE_MANIFEST: readonly string[] = [
  // health.ts
  '/api/health',
  '/api/info',

  // agents.ts — list / create
  '/api/agents',

  // agents.ts — static routes (declared before dynamic /:id per FastAPI parity)
  '/api/agents/suggestions',
  '/api/agents/suggestions/refresh',
  '/api/agents/templates',
  '/api/agents/model',
  '/api/agents/validate-id',
  '/api/agents/validate-token',

  // agents.ts — dynamic per-persona
  '/api/agents/:id',
  '/api/agents/:id/full',
  '/api/agents/:id/avatar',
  '/api/agents/:id/activate',
  '/api/agents/:id/deactivate',
  '/api/agents/:id/restart',
  '/api/agents/:id/model',
  '/api/agents/:id/files',
  '/api/agents/:id/files/:filename',
  '/api/agents/:id/files/history',
  '/api/agents/:id/conversation',
  '/api/agents/:id/tokens',
  '/api/agents/:id/tasks',

  // conversation.ts — SSE stream
  '/api/conversation/:id/stream',

  // scheduled.ts
  '/api/scheduled',
  '/api/scheduled/:taskId',

  // memories.ts
  '/api/memories',
  '/api/tokens',

  // hive-mind.ts
  '/api/hive-mind/recent',

  // settings.ts
  '/api/dashboard/settings',

  // mission.ts — wildcard pass-throughs to the orchestration framework
  // surface (docs/mc-profile-contract.md § 3.2). Listed as both the
  // wildcard mount points AND the canonical concrete paths so the
  // donor-route-manifest test can match by prefix.
  '/api/convoy',
  '/api/convoy/:id',
  '/api/convoy/:id/status',
  '/api/convoy/:id/subtasks',
  '/api/convoy/:id/ready',
  '/api/convoy/:id/subtask/:sid',
  '/api/convoy/:id/subtask/:sid/dispatch',
  '/api/convoy/:id/subtask/:sid/complete',
  '/api/convoy/:id/subtask/:sid/fail',
  '/api/convoy/:id/subtask/:sid/transition',
  '/api/convoy/:id/subtask/:sid/progress',
  '/api/mailbox',
  '/api/mailbox/send',
  '/api/mailbox/inbox/:agent',
  '/api/mailbox/claim/:agent',
  '/api/mailbox/ack/:delivery_id',
  '/api/team',
  '/api/team/:id',
  '/api/team/:id/members',
  '/api/team/:id/shutdown',
  '/api/team/:id/memory',
  '/api/executor/callback',
] as const;
