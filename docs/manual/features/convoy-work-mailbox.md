# Convoy, Work Queue, And Mailbox

Status: active baseline
Owner: Python orchestration services
Last updated: 2026-05-31

## What It Does

Convoy, Work Queue, and Mailbox are the Homie orchestration backbone for
multi-step work. Convoys track task DAGs/subtasks, mailbox entries coordinate
handoffs between agents/team members, and the Work Queue exposes actionable
tasks in the dashboard.

## Operator Entry Points

- Dashboard: `/convoy`, `/work`, team mailbox panes under `/teams`
- CLI: `thehomie convoy`, `thehomie mailbox`
- API: `/api/convoy/*`, `/api/work/tasks/*`, `/api/mailbox/*`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/orchestration/convoy_service.py`, `.claude/scripts/orchestration/mailbox_service.py`, `.claude/scripts/orchestration/api.py` |
| Hono/dashboard server | `dashboard/server/src/routes/mission.ts`, `dashboard/server/src/routes/work.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/Convoy.tsx`, `dashboard/web/src/pages/WorkQueue.tsx`, `dashboard/web/src/pages/Teams.tsx` |
| Tests | `.claude/scripts/tests/test_convoy_service.py`, `.claude/scripts/tests/test_mailbox_service.py`, `.claude/scripts/tests/test_typed_mailbox.py`, `dashboard/server/src/__tests__/mission.test.ts`, `dashboard/server/src/__tests__/work.test.ts` |

## Safety Boundaries

- Python owns state transitions.
- Avoid dual writes between dashboard and orchestration services.
- Preserve valid status transitions and claim/ack semantics.
- Hono routes stay pass-through/thin.
- Mailbox payloads may contain sensitive task context; do not dump raw bodies in
  public docs or broad UI surfaces.

## How To Run It

```powershell
cd <repo>\.claude\scripts
uv run thehomie convoy list
uv run thehomie mailbox --help
```

Dashboard:

```text
http://127.0.0.1:5173/convoy
http://127.0.0.1:5173/work
```

## How To Test It

```powershell
cd <repo>\.claude\scripts
uv run pytest tests/test_convoy_service.py tests/test_mailbox_service.py tests/test_typed_mailbox.py -q
```

```powershell
cd <repo>\dashboard\server
npm run test -- src/__tests__/mission.test.ts src/__tests__/work.test.ts
npm run typecheck
```

## Latest Live Proof

Recent Team Room and team scheduler proofs created and completed convoys
through this backbone. Use the feature-specific proof pages for details.

## Public Export Status

Framework core. Verify current public mirror state before export claims.

## Next Slices

- Better work queue/operator manual with screenshots and task-state examples.
- Tie convoy/mailbox proof rows into feature pages automatically.
