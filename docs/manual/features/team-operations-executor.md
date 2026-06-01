# Team Operations And Executor

Status: active baseline
Owner: Python team services
Last updated: 2026-05-31

## What It Does

Team Operations covers team sessions, team members, loop steps, executor steps,
team memory, and scheduler ticks. It is the shared substrate used by Team Room,
TaskChad drills, autonomous team scheduler, and dashboard `/teams` controls.

## Operator Entry Points

- Dashboard: `/teams`
- CLI: `thehomie team ...`
- Chat/Telegram: `/teamtick`, `/taskchaddrill`, `/teamroom`
- API: `/api/team`, `/api/team/:id`, `/api/team/:id/members`,
  `/api/team/:id/loop-step`, `/api/team/:id/tick`,
  `/api/team/:id/executor-step`, `/api/team/:id/memory`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/orchestration/team_service.py`, `.claude/scripts/orchestration/team_loop.py`, `.claude/scripts/orchestration/team_executor.py`, `.claude/scripts/orchestration/team_memory.py`, `.claude/scripts/orchestration/team_state.py` |
| Chat/router | `.claude/chat/core_handlers.py`, `.claude/chat/commands.py` |
| API/dashboard | `.claude/scripts/orchestration/api.py`, `dashboard/web/src/pages/Teams.tsx`, `dashboard/server/src/routes.ts` |
| Tests | `.claude/scripts/tests/test_team_state.py`, `.claude/scripts/tests/test_team_loop.py`, `.claude/scripts/tests/test_team_executor.py`, `.claude/scripts/tests/test_team_memory.py`, `.claude/scripts/tests/test_team_cli.py` |

## Safety Boundaries

- Python owns team state, runtime execution, executor approvals, and team memory.
- Executor commands must stay in approved roots and command presets.
- Team memory must reject secrets and traversal.
- Runtime is optional and must preserve no-tools defaults unless explicitly
  expanded by a slice.
- Dashboard must not implement worker/executor logic locally.

## How To Run It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run thehomie team list
uv run thehomie team --help
```

Dashboard:

```text
http://127.0.0.1:5173/teams
```

## How To Test It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run pytest tests/test_team_state.py tests/test_team_loop.py tests/test_team_executor.py tests/test_team_memory.py tests/test_team_cli.py -q
```

## Latest Live Proof

Team Operations is proven indirectly through late-May Team Room, TaskChad, and
autonomous team scheduler proofs. Use those feature pages for current IDs and
proof details.

## Related Handoffs

- `docs/HANDOFF-taskchad-team-drill-2026-05-27.md`
- `docs/HANDOFF-taskchad-runtime-mode-drill-closeout-2026-05-29.md`
- `docs/HANDOFF-team-room-v3-live-proof-closeout-2026-05-31.md`

## Public Export Status

Framework core. Verify current public mirror state before export claims.

## Next Slices

- Consolidate executor-step, loop-step, and team memory examples.
- Add dashboard proof screenshots after the next `/teams` artifact-panel slice.
