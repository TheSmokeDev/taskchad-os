# Persona Lifecycle And Files

Status: active baseline
Owner: Python persona lifecycle with dashboard controls
Last updated: 2026-05-31

## What It Does

Persona lifecycle and file surfaces let the operator create, inspect, activate,
deactivate, restart, and manage Homie personas from the dashboard while keeping
identity files and bot lifecycle under Python-owned validation and redaction.

## Operator Entry Points

- Dashboard: `/agents`, `/agents/:id`, `/agents/:id/files`
- API: `/api/agents/*`, `/api/conversation/:id/stream`
- CLI/profile commands: `thehomie profile ...`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/dashboard_api.py`, `.claude/scripts/personas/*`, `.claude/scripts/dashboard_bot_lifecycle.py` |
| Hono/dashboard server | `dashboard/server/src/routes/agents.ts`, `dashboard/server/src/routes/conversation.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/Agents.tsx`, `dashboard/web/src/pages/AgentDetail.tsx`, `dashboard/web/src/pages/AgentFiles.tsx`, `dashboard/web/src/components/AgentCreateWizard.tsx` |
| Tests | `.claude/scripts/tests/test_dashboard_api.py`, `.claude/scripts/tests/test_dashboard_bot_lifecycle.py`, dashboard agent tests |

## Safety Boundaries

- Validate persona IDs and paths.
- Preserve browser-facing `main` to backend `default` translation boundaries.
- File access must stay allowlisted and traversal-safe.
- Bot tokens and secret env values must be redacted and never committed.
- Avatar/file upload paths need type, size, and path checks.

## How To Run It

Dashboard:

```text
http://127.0.0.1:5173/agents
```

CLI:

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run thehomie profile list
uv run thehomie profile show default
```

## How To Test It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run pytest tests/test_dashboard_api.py tests/test_dashboard_bot_lifecycle.py -q
```

Run dashboard agent/component tests for UI changes.

## Latest Live Proof

Use current dashboard/API state before making a new live claim. Existing
tracker entries cover profile lifecycle and multi-persona Windows support.

## Related Handoffs

- `PRPs/active/TRACKER.md`
- `docs/mc-profile-contract.md`

## Public Export Status

Framework core. Verify the public mirror before claiming current export state.

## Next Slices

- Split detailed profile lifecycle/import/export docs into a dedicated manual
  pass.
- Add current dashboard screenshot/proof when persona UI changes next.
