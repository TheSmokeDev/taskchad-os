# Autonomous Team Scheduler

Status: shipped and Telegram-proven
Owner: Python orchestration team loop
Last updated: 2026-05-31

## What It Does

The Autonomous Team Scheduler lets a team advance work without dashboard-local
worker logic. It can tick a team, let a member claim work, write a bounded
handoff/reply, and advance the bound subtask. Later slices added runtime-lane
team member replies while preserving no-tools defaults and Python-owned state
transitions.

## Operator Entry Points

- Chat/Telegram: `/teamtick <team_id>`, `/teamtick <team_id> --complete-running`
- CLI: `thehomie team tick`
- Dashboard: `/teams`
- API: `POST /api/team/:id/tick`, `POST /api/team/:id/loop-step`,
  `POST /api/team/:id/executor-step`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/orchestration/team_loop.py`, `.claude/scripts/orchestration/team_executor.py`, `.claude/scripts/orchestration/team_state.py` |
| Chat/router | `.claude/chat/core_handlers.py`, `.claude/chat/commands.py` |
| API/dashboard | `.claude/scripts/orchestration/api.py`, `dashboard/web/src/pages/Teams.tsx`, `dashboard/server/src/routes.ts` |
| Tests | `.claude/scripts/tests/test_team_loop.py`, `.claude/scripts/tests/test_team_executor.py`, `.claude/scripts/tests/test_team_cli.py`, dashboard Teams tests |

## Safety Boundaries

- Python owns task selection, mailbox claim, reply generation, and state
  transition.
- Dashboard provides controls/status only; it must not run worker logic locally.
- Runtime-backed replies are explicit and must preserve no-tools defaults.
- Proof must distinguish local dashboard smoke, CLI proof, and official
  Telegram proof.

## How To Run It

```powershell
cd <repo>\.claude\scripts
uv run thehomie team tick <team_id>
uv run thehomie chat -q "/teamtick <team_id>" -Q
```

Dashboard:

```text
http://127.0.0.1:5173/teams
```

## How To Test It

```powershell
cd <repo>\.claude\scripts
uv run pytest tests/test_team_loop.py tests/test_team_executor.py tests/test_team_cli.py -q
```

Run dashboard Teams tests when changing the UI controls.

## Latest Live Proof

- Date: 2026-05-25
- Surface: chat adapter and orchestration database verification.
- Commands: `/teamtick 5` and `/teamtick 5 --complete-running`
- Result: database verification showed the active convoy/subtask completed.

## Public Export Status

Check the tracker and public framework repo before claiming current export
state for this slice.

## Next Slices

- Better dashboard timeline for team ticks and runtime-backed replies.
- Consolidate executor-step, loop-step, and tick operator docs into this page.
