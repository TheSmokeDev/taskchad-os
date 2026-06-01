# Runtime Status And Model Control

Status: active baseline
Owner: lane-first runtime selection
Last updated: 2026-05-31

## What It Does

Runtime status and model control let the operator inspect and change the active
lane/model without editing config files. The contract is lane-first: operator
surfaces should talk about lanes first and keep provider-specific details behind
the runtime layer.

## Operator Entry Points

- Chat/Telegram: `/provider`, `/model`, `/diagnostics`
- CLI: `thehomie status --json`, `thehomie doctor`,
  `thehomie chat -m <lane-or-provider>`
- Dashboard: `/agents`, `/usage`, `/jarvis`
- API: `/api/agents/model`, `/api/tokens`, `/api/jarvis/status`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Runtime selection | `.claude/scripts/runtime/selection.py`, `.claude/scripts/runtime/lane_router.py`, `.claude/scripts/runtime/registry.py` |
| Chat/router | `.claude/chat/commands.py`, `.claude/chat/core_handlers.py`, `.claude/chat/cli.py`, `.claude/chat/diagnostics.py` |
| Dashboard API | `.claude/scripts/dashboard_api.py` |
| Dashboard web | `dashboard/web/src/pages/Agents.tsx`, `dashboard/web/src/pages/Usage.tsx`, `dashboard/web/src/pages/Jarvis.tsx` |
| Tests | `.claude/scripts/tests/test_runtime_selection.py`, `.claude/scripts/tests/test_cli.py`, `.claude/scripts/tests/test_diagnostics.py`, `.claude/scripts/tests/test_dashboard_api.py` |

## Safety Boundaries

- Preserve lane-first wording.
- Quiet-mode JSON is a machine contract; keep stable fields such as `success`,
  `error`, `session_id`, `lane`, `provider`, `model`, `cost_usd`,
  `tool_calls`, and `execution_time_ms`.
- Do not merge Claude Max subscription semantics with API cost semantics.
- Runtime selection changes go through canonical selection helpers.

## How To Run It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run thehomie chat -q "/provider" -Q
uv run thehomie chat -q "/model auto" -Q
uv run thehomie status --json
uv run thehomie doctor
```

## How To Test It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run pytest tests/test_runtime_selection.py tests/test_diagnostics.py tests/test_runtime_registry.py tests/test_cli.py tests/test_lane_router.py tests/test_runtime_routing.py tests/test_chat_runtime_engine.py -q
```

## Latest Live Proof

Use current CLI/status checks before making a new live claim. Tracker entries
record multiple runtime proofs for TaskChad drill and Team Room runtime lanes.

## Related Handoffs

- `PRPs/active/TRACKER.md`
- `AGENTS.md`

## Public Export Status

Runtime surfaces are framework core; public export status depends on the slice
and must be verified through `scripts/sanitize.py` and the public mirror.

## Next Slices

- Manual page for provider catalog/runtime overlays.
- Dashboard-specific lane/model diagnostics page if `/agents` grows too dense.
