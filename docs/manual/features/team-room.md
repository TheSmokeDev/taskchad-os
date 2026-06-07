# Team Room

Status: V3 shipped, dashboard-artifacted, public-exported, and live-proven
Owner: Python orchestration
Last updated: 2026-05-31

## What It Does

Team Room runs a facilitated cross-functional meeting for a goal. The current
V3 baseline makes the meeting feel more like an office/team room: facilitator
control, explicit department votes and confidence, interrupts/challenges, role
memory between meetings, and clearer synthesis of agreements/disagreements.

## Operator Entry Points

- Chat/Telegram: `/teamroom [--v2] [--runtime] [--lane <lane>] <goal>`
- Dashboard: `/teams` Team Room run controls, result panel, and persisted V3
  artifact panels
- API: `POST /api/team/room/run`
- Product wrapper: `POST /api/team/operating-room/run` composes Team Room plus
  optional Team Tick into a sanitized proof packet.
- CLI: through `thehomie chat -q "/teamroom ..."`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/orchestration/team_room.py` |
| Chat/router | `.claude/chat/core_handlers.py`, `.claude/chat/commands.py` |
| Hono/dashboard server | `dashboard/server/src/routes/mission.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/Teams.tsx` |
| Tests | `.claude/scripts/tests/test_team_room_workflow.py`, `dashboard/web/src/__tests__/panels.test.tsx`, `dashboard/server/src/__tests__/mission.test.ts` |

## Safety Boundaries

- Python/orchestration remains the source of truth for meeting behavior and
  metadata serialization.
- Dashboard and Hono stay thin over the Python result.
- Operating Room may wrap Team Room results, but Team Room remains the owner of
  meeting behavior and serialization.
- Dashboard artifact panels render Python-owned result/session metadata; they
  must not invent meeting state locally.
- Default behavior remains deterministic unless runtime is explicitly requested.
- Runtime turns use the no-tools path by default.
- Safe metadata serialization must not expose prompts, runtime session IDs,
  cookies, tokens, or private browser state.

## How To Run It

Deterministic chat path:

```powershell
cd <repo>\.claude\scripts
uv run thehomie chat -q "/teamroom --v2 How should the team prioritize the next release?" -Q
```

Dashboard path:

```text
http://127.0.0.1:5173/teams
```

## How To Test It

```powershell
cd <repo>\.claude\scripts
uv run python -m py_compile orchestration/team_room.py ../chat/core_handlers.py
uv run pytest tests/test_team_room_workflow.py -q
```

```powershell
cd <repo>\dashboard\web
npm run test -- src/__tests__/panels.test.tsx
npm run typecheck
```

## Latest Live Proof

- Date: 2026-05-31
- Surface: chat adapter and dashboard observer path.
- Result: facilitated boardroom mode completed, vote/confidence board and
  interrupt register were populated, role memory carried forward, and runtime
  remained off for deterministic proof.

Dashboard artifact panel proof:

- Date: 2026-05-31
- Surface: Tailscale raw-IP Vite URL, `http://<tailscale-ip>:5173/teams`
- Result: Hono `POST /api/team/room/run` created a facilitated boardroom result
  with completed progress, role votes, interrupts/challenges, agreements, and
  disagreements.
- Session metadata confirmed V3 meeting behavior, role memory, vote board,
  interrupts, synthesis confidence, and no runtime `session_id`.
- In-app browser DOM confirmed `Team Room V3 Artifacts`, `Vote + Confidence
  Board`, `Role Memory`, `Interrupts + Challenges`, and `Agreements /
  Disagreements`; console warnings/errors were empty. Browser screenshot
  capture timed out through CDP after DOM proof passed.

## Public Export Status

Public-exported through `scripts/sanitize.py`.

## Next Slices

- Runtime-turn observability for each role turn.
- Artifact history browsing/search across prior Team Room sessions.
