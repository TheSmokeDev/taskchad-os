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
- CLI: through `thehomie chat -q "/teamroom ..."`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/orchestration/team_room.py` |
| Chat/router | `.claude/chat/core_handlers.py`, `.claude/chat/commands.py` |
| Hono/dashboard server | `dashboard/server/src/routes/mission.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/Teams.tsx` |
| Tests | `.claude/scripts/tests/test_team_room_workflow.py`, `dashboard/web/src/__tests__/panels.test.tsx`, `dashboard/server/src/__tests__/mission.test.ts` |
| Docs/proof | `docs/HANDOFF-team-room-v3-live-proof-closeout-2026-05-31.md`, `docs/HANDOFF-team-room-dashboard-controls-closeout-2026-05-31.md` |

## Safety Boundaries

- Python/orchestration remains the source of truth for meeting behavior and
  metadata serialization.
- Dashboard and Hono stay thin over the Python result.
- Dashboard artifact panels render Python-owned result/session metadata; they
  must not invent meeting state locally.
- Default behavior remains deterministic unless runtime is explicitly requested.
- Runtime turns use the no-tools path by default.
- Safe metadata serialization must not expose prompts, runtime session IDs,
  cookies, tokens, or private browser state.

## How To Run It

Deterministic chat path:

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run thehomie chat -q "/teamroom --v2 How do we grow TaskChad?" -Q
```

Dashboard path:

```text
http://127.0.0.1:5173/teams
```

## How To Test It

```powershell
cd C:\Users\YourUser\thehomie\.claude\scripts
uv run python -m py_compile orchestration/team_room.py ../chat/core_handlers.py
uv run pytest tests/test_team_room_workflow.py -q
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\web
npm run test -- src/__tests__/panels.test.tsx
npm run typecheck
```

## Latest Live Proof

- Date: 2026-05-31
- Surface: Telegram Web to `@YourBot`
- Command: `/teamroom --v2 How do we get TaskChad to one million dollars with V3 office behavior proof?`
- Result: team `#24`, convoy `#34`, progress `21/21`, mode
  `facilitated_boardroom`, confidence `0.77`, four votes, five interrupts,
  runtime off.
- Python DB confirmed `meeting_behavior_version=v3`, role memory carried from
  meeting `#23`, vote board, agreements, and disagreements.
- Dashboard `/browser` observed the same persistent CDP session as read-only
  proof.

Dashboard artifact panel proof:

- Date: 2026-05-31
- Surface: Tailscale raw-IP Vite URL, `http://<tailscale-ip>:5173/teams`
- Result: Hono `POST /api/team/room/run` created team `#25`, convoy `#35`,
  progress `21/21`, mode `facilitated_boardroom`, confidence `0.77`, four
  votes, five interrupts, runtime off.
- Session metadata confirmed `meeting_behavior_version=v3`, role memory `4`,
  vote board `4`, interrupts `5`, synthesis confidence `0.77`, and no
  `session_id`.
- In-app browser DOM confirmed `Team Room V3 Artifacts`, `Vote + Confidence
  Board`, `Role Memory`, `Interrupts + Challenges`, and `Agreements /
  Disagreements`; console warnings/errors were empty. Browser screenshot
  capture timed out through CDP after DOM proof passed.

## Related Handoffs

- `docs/HANDOFF-team-room-v3-live-proof-closeout-2026-05-31.md`
- `docs/HANDOFF-team-room-dashboard-controls-closeout-2026-05-31.md`

## Public Export Status

Public-exported through `scripts/sanitize.py`.

- Private V3 commit: `9daecff`
- Public V3 commit: `3d909be`
- Dashboard controls private commit: `5f6d39d`
- Dashboard controls public commit: `5b6d4ec`

## Next Slices

- Runtime-turn observability for each role turn.
- Artifact history browsing/search across prior Team Room sessions.
