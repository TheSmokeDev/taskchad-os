# Capability Gateway

Status: Read-only normalized catalog deployed
Owner: Python orchestration/runtime
Last updated: 2026-08-26

## What It Does

Capability Gateway is the read-only operator inventory for Homie's runtime
lane, model, toolsets, direct integrations, BrowserOps readiness, outbound
messaging readiness, and approval policy. `capabilities.items` preserves the
original chat-extension/runtime-overlay envelope for existing consumers.
`capabilities.catalog.items` is the bounded normalized projection of tools,
toolsets, skills, trusted MCP servers, integrations, and capability plugins
under stable namespaced IDs; `legacy_items` remains an alias of the original
envelope during adoption.

## Operator Entry Points

- Dashboard: `/capabilities`
- API: `GET /api/capabilities/status`
- CLI: `thehomie desktop` launches the dashboard stack that includes the page

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/personas/capability_catalog.py`, `.claude/scripts/orchestration/capability_gateway.py`, `.claude/scripts/runtime/tool_registry.py`, `.claude/scripts/runtime/toolsets.py`, `.claude/scripts/runtime/framework_registry.py`, `.claude/scripts/integrations/registry.py` |
| Chat/router | status/doctor still use existing diagnostics paths |
| Hono/dashboard server | `dashboard/server/src/routes/mission.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/CapabilityGateway.tsx`, `dashboard/web/src/App.tsx`, `dashboard/web/src/lib/routes.ts` |
| Tests | `.claude/scripts/tests/test_persona_capability_catalog.py`, `.claude/scripts/tests/test_capability_gateway.py`, `.claude/scripts/tests/test_orchestration_api.py`, `dashboard/server/src/__tests__/mission.test.ts` |
| Docs/proof | this page |

## Safety Boundaries

- v1 is read-only.
- The normalized catalog and per-persona projection are diagnostic read models,
  never authorization checks. Execution still goes through the owning registry,
  persona scope, runtime transport, action policy, and dedicated gates.
- Availability never implies assignment. Plugin discovery/load never equips a
  persona automatically.
- Catalog collection reads the current physical registries and never initializes
  or registers tools as a side effect.
- An owner process with a live capability-plugin kernel passes its typed
  `PluginInstanceView` rows into the Gateway. Unloaded trusted manifests remain
  visible, while the injected physical lifecycle view wins for the same plugin.
- Source failures produce `status: partial`, a bounded safe error receipt, and
  preserve healthy source rows; the Gateway does not fabricate a green empty
  catalog.
- Dashboard mode is reported as `read_only`.
- Mutating actions remain default-denied unless a later slice adds explicit
  approval UX and policy enforcement.
- Outbound messaging is reported as `policy_gated` when send/post actions are
  present.
- Status output must not expose credential values or raw token material.
- Serialization omits filesystem paths, channel IDs, profile memory, and all
  configuration values. Only safe requirement names are reported.

## State Semantics

Persona projections report `available`, `assigned`, `configured`, `callable`,
and `enabled` independently. `enabled` is the final conjunction after ordered
blocked reasons, explicit disables, owner-plugin lifecycle, dependencies, and
carrying runtime transport are evaluated. Readiness adapters retain all six
axes and supported-surface statuses for legacy consumers.

Catalog queries are case-folded, deterministic, capped at 100 rows per page,
and use an opaque cursor derived from the last stable `(kind, id)` rather than a
mutable array index.

`GET /api/capabilities/status` accepts `search`, repeated `kinds`, repeated
`sources`, `limit`, `cursor`, and optional `persona_id` query parameters. The
normalized page is returned under `capabilities.catalog`; a persona query joins
that page to the same persona-grained state projection without changing the
legacy Gateway counts or rows.

The framework canonical default persona is `default`. The dashboard uses
`main`; Hono translates `main` to `default` on the capability query and maps the
response back to `main` at that single boundary.

## How To Run It

```powershell
curl http://127.0.0.1:4322/api/capabilities/status
```

Dashboard:

```text
http://127.0.0.1:5173/capabilities
```

## How To Test It

```powershell
cd .claude\scripts
uv run pytest tests/test_persona_capability_catalog.py tests/test_capability_gateway.py tests/test_persona_readiness.py tests/test_framework_registry.py tests/test_orchestration_api.py::test_capability_status_exposes_bounded_search_query_contract tests/test_orchestration_api.py::test_capability_status_first_request_bootstraps_all_six_kinds_in_fresh_process tests/test_orchestration_api.py::test_capability_status_projects_real_missing_handler_as_blocked -q
```

```powershell
cd dashboard\server
npm test -- src/__tests__/mission.test.ts
```

## Latest Proof

- Date: 2026-08-26
- Git: #571 merged in PR #610 at `3007fae7`; deployment checkout is clean
  current master `b7e3bf1d`.
- Local preflight: 132 capability, readiness, lifecycle-switch, and watchdog
  tests passed before cutover.
- Runtime: The Homie `1.7.1` runs from
  `C:\Users\YourUser\thehomie-runtime-capabilities-20260825`; the health
  owner PID command line points to that checkout.
- Bot health: `status=ok`; Telegram polls as `@YourBot`, Discord gateway
  is ready, web relay is connected, and both Telegram and Discord registered
  72 native slash commands.
- Memory: 9,840 indexed documents, embedding status `ready`, and the canonical
  vault is `C:\Users\YourUser\thehomie\TheHomie\Memory`.
- Gateway: the live `4322` orchestration process runs from the same release
  checkout and returned 157 deterministic records with zero catalog errors:
  74 tools, 21 toolsets, 43 skills, 7 trusted MCP servers, 11 integrations,
  and 1 capability plugin. Dashboard mode remained `read_only`.
- Negative proof: the focused HTTP suite includes a physically assigned tool
  with no handler and reports it blocked rather than green; the fresh-process
  test prevents a first request from returning an empty green tool catalog.

## Related Handoffs

- Private proof handoffs stay outside `docs/manual`.

## Public Export Status

Public-export eligible through `scripts/sanitize.py`. Export must be run before
any public push.

## Next Slices

- Gated write-capability execution.
- Per-tool approval records and audit trails.
- Capability health probes for unavailable integrations.
