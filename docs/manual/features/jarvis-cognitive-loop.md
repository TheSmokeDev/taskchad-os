# Cognitive Loop

Status: shipped, live-runtime activated, Telegram E2E proven
Owner: chat cognition/runtime layers
Last updated: 2026-05-31

## What It Does

The cognitive loop is the Homie cognition/autonomy stack: active self-model,
scheduled reflection/weekly/dream processing, policy-gated self-amendment,
WorkingMemory prompt state, proactive brief generation, and status/diagnostic
truth surfaces. Some internal files still carry the legacy Jarvis name.

## Operator Entry Points

- Dashboard: internal status component hidden from the public nav
- CLI/status: `thehomie status --json`, `thehomie doctor`, `/diagnostics`
- Scheduled loops: reflection, weekly, dream, heartbeat probes
- Telegram: live bot status/runtime proof paths

## Source Of Truth Files

| Layer | Files |
|---|---|
| Cognition/runtime | `.claude/chat/cognition/*`, `.claude/chat/engine.py`, `.claude/chat/diagnostics.py`, `.claude/chat/cli.py` |
| Dashboard API | `.claude/scripts/dashboard_api.py`, `.claude/scripts/tests/test_jarvis_dashboard_status.py` |
| Dashboard server/web | `dashboard/server/src/routes/jarvis.ts`, `dashboard/web/src/pages/Jarvis.tsx` remains an internal status component; it is hidden from the public dashboard nav until the public surface is re-proven |
| Tests | cognition/status/scheduled-loop tests under `.claude/scripts/tests/`, `dashboard/server/src/__tests__/jarvis.test.ts` |

## Safety Boundaries

- Automatic durable-memory writes stay policy-gated.
- Self-amendment uses proposal/ledger/rollback paths; do not silently rewrite
  SELF/SOUL/USER/MEMORY.
- Scheduled probes should support no-write/no-external-send test modes.
- Live proof must distinguish root checkout, worktree checkout, and live bot
  process state.

## How To Run It

```powershell
cd <repo>\.claude\scripts
uv run thehomie status --json
uv run thehomie doctor
uv run thehomie chat -q "/diagnostics" -Q
```

The public dashboard shell does not advertise the internal cognitive status
component.

## How To Test It

Use focused cognition/status/scheduled-loop tests for the touched slice. For
dashboard status:

```powershell
cd <repo>\.claude\scripts
uv run pytest tests/test_jarvis_dashboard_status.py -q
```

```powershell
cd <repo>\dashboard\server
npm run test -- src/__tests__/jarvis.test.ts
npm run typecheck
```

## Latest Live Proof

The cognition-loop closeout covered live-runtime activation, durable-memory
apply proof, adapter proof, observability proof, and public export. Recheck
current live bot process state before claiming a new live proof.

## Public Export Status

Public-exported in the cognition-loop slice; current public mirror state
should be rechecked before repeating export claims.

## Next Slices

- Fold detailed cognition-loop subfeatures into separate manual pages:
  scheduled cognition, WorkingMemory, self-amendment, proactive brief, and
  status/diagnostics.
