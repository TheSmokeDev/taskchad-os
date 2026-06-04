# Operating Room

Status: Product slice implemented, local proof path in progress
Owner: Python orchestration
Last updated: 2026-06-04

## What It Does

Operating Room is the product wrapper over Team Room and Team Tick. It runs a
goal through the existing team meeting behavior, optionally advances the team
with one bounded tick, and returns one public-safe proof packet for dashboard
and demo use.

## Operator Entry Points

- Dashboard: `/teams`
- API: `POST /api/team/operating-room/run`
- CLI: `thehomie desktop` opens the local dashboard stack at `/teams`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/orchestration/operating_room.py`, `.claude/scripts/orchestration/team_room.py`, `.claude/scripts/orchestration/team_loop.py` |
| Chat/router | none in this slice |
| Hono/dashboard server | `dashboard/server/src/routes/mission.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/Teams.tsx` |
| Tests | `.claude/scripts/tests/test_operating_room.py`, `dashboard/web/src/__tests__/panels.test.tsx`, `dashboard/server/src/__tests__/mission.test.ts` |
| Docs/proof | `docs/manual/fixtures/operating-room-proof-demo.json` |

## Safety Boundaries

- Python/orchestration remains the source of truth for meeting behavior,
  continuation choice, and proof serialization.
- Hono forwards the request. It does not interpret team state.
- The dashboard renders proof and controls. It does not own meeting logic.
- Proof packets are sanitized and must not contain prompts, runtime session
  IDs, tokens, claim tokens, cookies, credentials, or raw env values.
- This slice is Cabinet-free. Cabinet LiveKit voice is not part of the proof
  target for Operating Room.

## How To Run It

```powershell
cd .claude\scripts
uv run thehomie desktop
```

For an API-only local run:

```powershell
curl -X POST http://127.0.0.1:4322/api/team/operating-room/run `
  -H "Content-Type: application/json" `
  -d "{\"goal\":\"Run a public-safe Operating Room demo\",\"run_tick\":true}"
```

## Proof Packet

`POST /api/team/operating-room/run` returns:

- `run_id`
- `created_at`
- `team_room`
- `tick`
- `proof_packet`

The proof packet includes:

- `run_id`, `goal`, `workflow_id`, `meeting_mode`
- `team_id`, `convoy_id`, `progress`
- vote board, interrupts, decisions, owner actions, open questions
- tick/executor summary when `run_tick=true`
- final brief
- `sanitized=true`

## How To Test It

```powershell
cd .claude\scripts
uv run pytest tests/test_operating_room.py -q
```

```powershell
cd dashboard\server
npm test -- mission.test.ts routes-manifest.test.ts
```

```powershell
cd dashboard\web
npm test -- panels.test.tsx donor-route-manifest.test.ts
npm run build
```

## Latest Proof

- Date: 2026-06-04
- Surface: local tests and dashboard fixture
- Result: Operating Room proof packet shape, sanitization, Team Room
  composition, tick inclusion, Capability Gateway shape, Hono pass-through, and
  dashboard render states are covered by focused tests.
- Public demo fixture:
  `docs/manual/fixtures/operating-room-proof-demo.json`

## Related Handoffs

- Private proof handoffs stay outside `docs/manual`.

## Public Export Status

Public-export eligible through `scripts/sanitize.py`. Export must be run before
any public push.

## Next Slices

- Browser DOM plus API-state local demo proof.
- Richer proof history browsing on `/teams`.
- Signed Desktop installer distribution around the same Python-owned lifecycle.
