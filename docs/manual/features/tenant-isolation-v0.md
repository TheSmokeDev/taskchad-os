# Tenant Isolation v0

Status: Shipped (Phase A foundation + Phase B enforcement), enforcement default-OFF via `HOMIE_TENANT_ENFORCEMENT`
Owner: `.claude/scripts/orchestration/` (route policy, auth middleware, token store) plus `.claude/chat/` (CLI) and the dashboard slice
Last updated: 2026-06-22

## What It Does

Tenant Isolation lets one shared orchestration + dashboard API serve more than
one tenant without leaking data across them. A *tenant* is a bearer token bound
to a `workspace_id` and a `persona_scope` (the set of persona ids that token may
touch). With enforcement on, a bound tenant token can only reach its own
workspace's convoys, mailbox, teams, and work tasks, plus its in-scope personas:

- a cross-tenant resource id (another workspace's convoy/subtask/team) returns
  `404` (it does not exist *in your workspace*, and a 404 does not confirm it
  exists elsewhere);
- an `admin` or `voice_query` route returns `403` ("admin-only");
- a persona outside the token's scope returns `403`;
- a route with **no declared policy** returns `403` — deny-by-default.

The whole layer is a no-op until an operator turns it on. Default deployment
(no `HOMIE_TENANT_ENFORCEMENT`, zero tenant tokens) is **byte-identical** to the
single-tenant behaviour that shipped before — the legacy `ORCHESTRATION_API_TOKEN`
path is preserved exactly.

The model is **process-per-tenant**: a tenant is a profile, and the persona/profile
boundary is the isolation grain. Phase B is the shared-API-surface enforcement
that closes the gap the persona system alone could not (one cloud API serving
many tenants' requests).

## Operator Entry Points

Mint, list, and revoke tenant tokens with the `thehomie tenant` CLI. Tokens are
stored **hashed** (SHA-256); the raw token is printed exactly **once** at create
time and is never recoverable.

```powershell
cd .claude/scripts

# Admin bootstrap FIRST — seed an admin row for the existing global token so it
# survives once multi-tenant mode engages (see Safety Boundaries).
uv run thehomie tenant create --admin --workspace 1 --token "<existing ORCHESTRATION_API_TOKEN>" --label operator

# Mint a tenant token bound to a workspace + persona scope (JSON array or comma list).
uv run thehomie tenant create --workspace 2 --persona-scope '["sales"]' --label client-a
uv run thehomie tenant create --workspace 3 --persona-scope sales,support --label client-b

# List (never prints the raw token or its hash).
uv run thehomie tenant list
uv run thehomie tenant list --json --active-only

# Revoke (physical state; effective on the next request — Rule 2).
uv run thehomie tenant revoke --id 4
```

Turn enforcement on with the env flag (default off):

```text
HOMIE_TENANT_ENFORCEMENT=true   # in .claude/scripts/.env
```

## Source Of Truth Files

| Concern | Files |
|---|---|
| Route policy registry | `.claude/scripts/orchestration/route_policy.py` (`ROUTE_POLICY` table, `resolve_route_template`, `resolve_policy`, `enforce_policy`, `all_registered_routes`) |
| Auth middleware | `.claude/scripts/orchestration/api.py` (the single `@app.middleware("http")` auth path: truth-table, deny-by-default, `request.state` defaults) |
| Token binding + scope | `.claude/scripts/orchestration/tenant_auth.py` (`resolve_tenant_binding`, `TenantBinding`, `is_multi_tenant_mode`, `parse_persona_scope`, `hash_token`) |
| Token store | `.claude/scripts/orchestration/db.py` (`tenant_tokens` schema, `insert_tenant_token`, `get_binding_by_hash`, `has_active_tenant_token`, `revoke_token`) |
| Operator CLI | `.claude/chat/cli.py` (the `tenant create\|list\|revoke` group) |
| Dashboard scoping | `.claude/scripts/dashboard_api.py` (`_scoped_conversation_id`, `_require_persona_in_scope`, `_require_scoped_persona_filter`, the destructive physical-state gate) |
| Tests | `.claude/scripts/tests/test_tenant_route_policy.py`, `test_tenant_isolation_phase_a.py`, `test_tenant_isolation_leak.py`, `test_orchestration_api.py`, `test_dashboard_api.py` |

## Safety Boundaries

Policy before mechanism. These invariants are what a 3-round adversarial review
hardened (it found and we closed 3 real cross-tenant bugs):

- **Deny-by-default route policy.** Every route declares exactly one of five
  policies — `public`, `tenant_workspace`, `tenant_persona`, `admin`,
  `voice_query`. A bound tenant token on a route with **no** policy gets `403`.
  A CI invariant asserts `set(ROUTE_POLICY) == all_registered_routes(app)`
  bidirectionally, so a newly added route cannot silently default open — the
  test fails until the route is classified.
- **Route TEMPLATE matching, not raw URL.** `resolve_route_template` replays
  Starlette route matching and recurses `include_router` mounts, so a policy is
  keyed on `(method, "/api/convoy/{convoy_id}")` and matches regardless of the
  id value. (The mount recursion is load-bearing: a naive `app.routes` walk
  matches the mount wrapper, not the dashboard's real templates.)
- **Activation truth-table.** Enforcement engages iff
  `HOMIE_TENANT_ENFORCEMENT` is truthy AND at least one non-revoked non-admin
  `tenant_tokens` row exists (`is_multi_tenant_mode`). Zero such rows ⇒ the
  legacy global-token path runs unchanged, every request binds to
  `workspace_id = 1` / `persona_scope = None`, byte-identical.
- **Admin rows do not engage MT mode.** `is_admin=1` rows carry the
  global/operator token so it is not stranded when the first tenant onboards.
  Bootstrap an admin row *before* (or with) the first non-admin token, or the
  global token will `401` on admin routes once MT mode engages.
- **Workspace threading on mutations.** Every `tenant_workspace` handler threads
  `request.state.workspace_id` into the actual service mutation/read (convoy,
  mailbox, team, work tasks, executor dispatch). Without it a tenant's write
  defaults to workspace 1 and lands on another tenant's data — this was one of
  the three closed blockers.
- **Scope fails closed (the None-vs-empty rule).** `persona_scope = None` means
  admin / single-tenant **allow-all**; an **empty** `frozenset()` means a
  non-admin token with **zero** allowed personas — **deny-all**. A non-admin
  token can never carry `None`, so a malformed or missing scope denies every
  persona instead of opening up.
- **Revocation is physical state.** The `revoked_at IS NULL` filter lives in the
  read, so a revoked token stops resolving on the next request with no cache to
  invalidate (Rule 2).
- **Workspace-scoped dashboard storage keys.** Dashboard conversation keys are
  scoped by workspace (`_scoped_conversation_id`) so two tenants both defaulting
  to a shared conversation id never read each other's thread; single-tenant
  (workspace 1) keeps the exact pre-existing key.

## How It Works

1. The middleware sets `request.state.workspace_id` / `persona_scope` / `is_admin`
   defaults **first**, before any exempt early-return, so an exempt handler
   reading `request.state` can never `AttributeError`/500.
2. If enforcement is off OR there are no non-admin tenant rows, the request runs
   the legacy single-tenant path and returns — unchanged.
3. Otherwise it resolves the bearer token to a `TenantBinding`
   (`workspace_id`, `persona_scope`, `is_admin`) via the hashed `tenant_tokens`
   lookup; an unknown/revoked token in MT mode is `401`.
4. It resolves the matched route template and looks up the policy. `None` ⇒
   `403` (deny-by-default). `admin`/`voice_query` ⇒ `403` for a tenant token.
   `public` ⇒ allow. `tenant_workspace`/`tenant_persona` ⇒ admitted, and the
   handler enforces the concrete id gate using `request.state`.
5. Handlers thread `workspace_id` into the service call (cross-workspace id ⇒
   `404`) and check `persona_id ∈ persona_scope` (out-of-scope ⇒ `403`).

## v0 Scope And B6 Deferrals

In scope for v0: the orchestration convoy/mailbox/team/work surfaces and the
dashboard persona/agent surfaces, gated through the one shared middleware.

Out of scope / deferred to **B6** (named, intentional):

- **Personal finance is OUT.** The finance store is single-user operator data,
  not a tenant surface; it is never workspace-scoped here.
- **Persona-grain boundary.** Isolation assumes each tenant's `persona_scope`
  is disjoint. If an operator deliberately grants the **same** persona id to two
  tenants, they share that persona's data by that explicit choice — distinct
  personas remain isolated.
- **Admin-only chat reads.** `/api/agents/{persona_id}/conversation`,
  `/api/agents/{persona_id}/tokens`, and `/api/hive-mind/recent` read the shared
  chat store keyed by persona with no workspace column, so they are classified
  `admin` (tenant tokens `403`) until B6 adds a `workspace_id` column to
  `chat_sessions` and they can be safely tenant-scoped. Cabinet and scheduled
  routes are `admin` for the same reason (no workspace column in v0).

## How To Run It

```powershell
cd .claude/scripts
# 1. Seed an admin token for the existing global token.
uv run thehomie tenant create --admin --workspace 1 --token "<global token>" --label operator
# 2. Mint a non-admin tenant token (inert until the flag is on).
uv run thehomie tenant create --workspace 2 --persona-scope '["sales"]' --label client-a
# 3. Enable enforcement (HOMIE_TENANT_ENFORCEMENT=true in .claude/scripts/.env), restart the API.
# 4. Verify: the tenant token reaches only workspace 2 + persona "sales"; admin routes 403.
```

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_tenant_route_policy.py tests/test_tenant_isolation_phase_a.py tests/test_tenant_isolation_leak.py tests/test_orchestration_api.py tests/test_dashboard_api.py -q
```

The suites prove: the per-route policy matrix (tenant `403`/`404` vs admin),
real deny-by-default (a dummy unregistered route `403`s a bound tenant), the CI
count invariant, the truth-table (zero rows ⇒ legacy byte-unchanged; ≥1 row ⇒
two distinct tenant tokens coexist), cross-tenant `404` on every team/convoy/
mailbox mutator with the target row provably unchanged, the dashboard
conversation isolation, and single-tenant parity. Several tests are
discriminating by construction — they fail if the workspace threading is removed.

## Latest Live Proof

- Commit `fa30a59e` on `feat/tenant-isolation-v0` (Phase A foundation rebased
  in + Phase B enforcement).
- 380 tests pass in the combined Phase B gate.
- A 3-round adversarial review found and closed 3 real cross-tenant bugs, each
  locked with a fail-without-fix test:
  1. team mutators defaulted `workspace_id` → a tenant could shut down/delete
     another tenant's team;
  2. dashboard conversations keyed by a shared default id → cross-tenant chat
     read even with disjoint persona scopes;
  3. three persona chat reads keyed by persona with no workspace column →
     reclassified `admin` (deny-by-default) until B6.

## Common Failure Modes

- **The global token starts 401ing on admin routes.** Multi-tenant mode engaged
  (a non-admin token exists) but no admin row was seeded for the global token.
  Run `tenant create --admin --token "<global token>"`.
- **A tenant token gets 403 on a route it should reach.** The route may be
  `admin`-classified in v0 (cabinet/scheduled/the three chat reads — B6
  deferred), or the persona id is outside the token's scope.
- **A newly added API route breaks the suite.** The CI invariant requires every
  route to declare a policy; classify the new route in `ROUTE_POLICY`.
- **Nothing is enforced.** `HOMIE_TENANT_ENFORCEMENT` is unset, or there are no
  non-admin tenant tokens — that is the default, no-op state.

## Public Export Status

The orchestration + dashboard code ships through the normal framework export
path (`scripts/sanitize.py`). This manual page is public-safe by construction
(mechanism only, relative paths, placeholder ids, no personal data). Because
`docs/` is in the sanitizer `DENY_DIRS`, this page ships only through its
explicit per-file lift in the sanitizer `INCLUDE_FILES` list. Private design
artifacts (the PRD/PRP) stay in `DENY_DIRS` and are never exported.

## Next Slices

- **B6** — add a `workspace_id` column to `chat_sessions` (+ cabinet/scheduled
  tables) so the admin-deferred reads can return to `tenant_persona`/
  `tenant_workspace` with real per-row scoping.
- A live two-bot smoke (two profiles, two tokens) proving end-to-end isolation
  against a running API.
- Per-service token/scope segmentation beyond the shared bearer.
