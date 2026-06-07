# Cabinet Rooms

Status: shipped baseline with deep manual
Owner: Python orchestration and Cabinet room state
Last updated: 2026-06-01

## What It Does

Cabinet is the multi-persona room surface. It supports text meetings, roster
snapshots, participant controls, slash commands, dashboard streams, and
chat-routed operator commands for meetings, standups, and discussions.

## Operator Entry Points

- Chat/Telegram: `/cabinet`, `/standup`, `/discuss`
- Dashboard: `/cabinet`, `/standup`
- API: `/api/cabinet/*`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/cabinet/room_state.py`, `.claude/scripts/cabinet/room_commands.py`, `.claude/scripts/dashboard_api.py` |
| Chat/router | `.claude/chat/core_handlers.py`, `.claude/chat/commands.py`, `.claude/scripts/integrations/cabinet_api.py` |
| Hono/dashboard server | `dashboard/server/src/routes/cabinet.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/Cabinet.tsx`, `dashboard/web/src/lib/cabinet-stream.ts` |
| Tests | `.claude/scripts/tests/test_cabinet_*.py`, `dashboard/server/src/__tests__/cabinet.test.ts`, `dashboard/web/src/__tests__/cabinet.test.tsx` |
| Docs/proof | `docs/cabinet-room-manual.md`, `docs/cabinet-voice-setup.md`, `docs/manual/features/cabinet-voice.md` |

## Safety Boundaries

- Room state mutation belongs to the Cabinet/Python layer.
- Chat/router and dashboard paths reach Cabinet through the orchestration HTTP
  API; do not use process-local channel registries across boundaries.
- Participant turns default-deny tools unless an explicit room allowlist exists.
- Dashboard/Hono stay thin over Python-owned room state and stream contracts.

## How To Run It

```powershell
cd <repo>\.claude\scripts
uv run thehomie chat -q "/cabinet list" -Q
uv run thehomie chat -q "/standup What matters today?" -Q
```

Dashboard:

```text
http://127.0.0.1:5173/cabinet
```

## How To Test It

Use the focused Cabinet suites listed in `docs/cabinet-room-manual.md`.

Common dashboard checks:

```powershell
cd <repo>\dashboard\server
npm run test -- src/__tests__/cabinet.test.ts src/__tests__/routes-manifest.test.ts
npm run typecheck
```

```powershell
cd <repo>\dashboard\web
npm run test -- src/__tests__/cabinet.test.tsx
npm run typecheck
```

## Latest Live Proof

See `docs/cabinet-room-manual.md` and tracker entries for the current Cabinet
baseline. Cabinet Voice now has a V1 launcher/control-plane page at
`docs/manual/features/cabinet-voice.md`; the deep Cabinet manual remains
authoritative for room-state details.

## Related Handoffs

- `docs/cabinet-room-manual.md`
- `docs/cabinet-voice-setup.md`
- `docs/manual/features/cabinet-voice.md`

## Public Export Status

Cabinet behavior is part of the framework surface; public export status varies
by slice and should be checked in tracker/handoffs before claiming current.

## Next Slices

- Keep the deep Cabinet manual updated when Cabinet behavior changes.
- Add Python-owned Cabinet voice status/start controls as the next bounded
  voice slice.
