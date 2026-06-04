# Capability Gateway

Status: Read-only v1 implemented
Owner: Python orchestration/runtime
Last updated: 2026-06-04

## What It Does

Capability Gateway is the read-only operator inventory for Homie's runtime
lane, model, toolsets, direct integrations, BrowserOps readiness, outbound
messaging readiness, and approval policy.

## Operator Entry Points

- Dashboard: `/capabilities`
- API: `GET /api/capabilities/status`
- CLI: `thehomie desktop` launches the dashboard stack that includes the page

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/orchestration/capability_gateway.py`, `.claude/scripts/runtime/capabilities.py`, `.claude/scripts/runtime/toolsets.py`, `.claude/scripts/integrations/registry.py` |
| Chat/router | status/doctor still use existing diagnostics paths |
| Hono/dashboard server | `dashboard/server/src/routes/mission.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/CapabilityGateway.tsx`, `dashboard/web/src/App.tsx`, `dashboard/web/src/lib/routes.ts` |
| Tests | `.claude/scripts/tests/test_operating_room.py`, `dashboard/web/src/__tests__/panels.test.tsx`, `dashboard/server/src/__tests__/mission.test.ts` |
| Docs/proof | this page |

## Safety Boundaries

- v1 is read-only.
- Dashboard mode is reported as `read_only`.
- Mutating actions remain default-denied unless a later slice adds explicit
  approval UX and policy enforcement.
- Outbound messaging is reported as `policy_gated` when send/post actions are
  present.
- Status output must not expose credential values or raw token material.

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
uv run pytest tests/test_operating_room.py::test_capability_gateway_status_shape -q
```

```powershell
cd dashboard\web
npm test -- panels.test.tsx
```

## Latest Proof

- Date: 2026-06-04
- Surface: focused Python and dashboard tests
- Result: status shape includes runtime, capabilities, toolsets, integrations,
  BrowserOps, outbound messaging, and default-deny approval policy.

## Related Handoffs

- Private proof handoffs stay outside `docs/manual`.

## Public Export Status

Public-export eligible through `scripts/sanitize.py`. Export must be run before
any public push.

## Next Slices

- Gated write-capability execution.
- Per-tool approval records and audit trails.
- Capability health probes for unavailable integrations.
