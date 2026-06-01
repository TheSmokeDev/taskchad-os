# Jarvis Cognitive Loop

Status: shipped, live-runtime activated, Telegram E2E proven
Owner: chat cognition/runtime layers
Last updated: 2026-05-31

## What It Does

Jarvis Cognitive Loop is the Homie cognition/autonomy stack: active self-model,
scheduled reflection/weekly/dream processing, policy-gated self-amendment,
WorkingMemory prompt state, proactive brief generation, and status/diagnostic
truth surfaces.

## Operator Entry Points

- Dashboard: `/jarvis`
- CLI/status: `thehomie status --json`, `thehomie doctor`, `/diagnostics`
- Scheduled loops: reflection, weekly, dream, heartbeat probes
- Telegram: live bot status/runtime proof paths

## Source Of Truth Files

| Layer | Files |
|---|---|
| Cognition/runtime | `.claude/chat/cognition/*`, `.claude/chat/engine.py`, `.claude/chat/diagnostics.py`, `.claude/chat/cli.py` |
| Dashboard API | `.claude/scripts/dashboard_api.py`, `.claude/scripts/tests/test_jarvis_dashboard_status.py` |
| Dashboard server/web | `dashboard/server/src/routes/jarvis.ts`, `dashboard/web/src/pages/Jarvis.tsx` |
| Tests | cognition/status/scheduled-loop tests under `.claude/scripts/tests/`, `dashboard/server/src/__tests__/jarvis.test.ts` |
| Tracker/proof | `PRPs/active/TRACKER.md` Jarvis autonomy entries |

## Safety Boundaries

- Automatic durable-memory writes stay policy-gated.
- Self-amendment uses proposal/ledger/rollback paths; do not silently rewrite
  SELF/SOUL/USER/MEMORY.
- Scheduled probes should support no-write/no-external-send test modes.
- Live proof must distinguish root checkout, worktree checkout, and live bot
  process state.

## How To Run It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run thehomie status --json
uv run thehomie doctor
uv run thehomie chat -q "/diagnostics" -Q
```

Dashboard:

```text
http://127.0.0.1:5173/jarvis
```

## How To Test It

Use focused cognition/status/scheduled-loop tests for the touched slice. For
dashboard status:

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run pytest tests/test_jarvis_dashboard_status.py -q
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\server
npm run test -- src/__tests__/jarvis.test.ts
npm run typecheck
```

## Latest Live Proof

Tracker records the Jarvis autonomy closeout: live-runtime activation,
real-vault apply proof, Telegram E2E proof, Langfuse trace, Sentry event, and
public export. Recheck current live bot process state before claiming a new live
proof.

## Related Handoffs

- `PRPs/active/TRACKER.md`
- `docs/production-v1-lessons-to-v2-framework.md`

## Public Export Status

Public-exported in the Jarvis autonomy slice; current public mirror state
should be rechecked before repeating export claims.

## Next Slices

- Fold detailed cognition-loop subfeatures into separate manual pages:
  scheduled cognition, WorkingMemory, self-amendment, proactive brief, and
  status/diagnostics.
