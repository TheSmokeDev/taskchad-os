# Memory, Hive, And Chat Observer

Status: active baseline
Owner: Python memory/brain APIs and read-only dashboard views
Last updated: 2026-05-31

## What It Does

Memory, Hive, and Chat Observer expose Homie memory and conversation state in
the dashboard. `/memories` and `/hive` render memory graphs, brain topology,
and recent activity; `/chat` observes conversations without becoming a browser
write or runtime mutation surface.

## Operator Entry Points

- Dashboard: `/memories`, `/hive`, `/chat`
- API: `/api/memories`, `/api/memory/graph`, `/api/brain/graph`,
  `/api/hive-mind/recent`, `/api/conversation/:id/stream`
- CLI/Telegram memory commands: `/search`, `/file`, `/working`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/dashboard_api.py`, memory/recall modules under `.claude/scripts/` and `.claude/chat/` |
| Hono/dashboard server | `dashboard/server/src/routes/memories.ts`, `dashboard/server/src/routes/brain.ts`, `dashboard/server/src/routes/hive-mind.ts`, `dashboard/server/src/routes/conversation.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/Memories.tsx`, `dashboard/web/src/pages/HiveMind.tsx`, `dashboard/web/src/pages/Chat.tsx`, graph components/hooks |
| Tests | `dashboard/web/src/__tests__/memory-graph.test.tsx`, `dashboard/web/src/__tests__/brain-graph-3d.test.tsx`, `dashboard/server/src/__tests__/brain.test.ts`, SSE/token-hardening tests |

## Safety Boundaries

- Dashboard chat observer is read-only unless a feature explicitly adds a
  write path.
- SSE query tokens are limited to the SSE route contract and must be scrubbed.
- Graph/list views must use scoped API contracts, not ad hoc vault mutation.
- Memory writes belong to canonical memory APIs and cognition policy gates.

## How To Run It

```text
http://127.0.0.1:5173/memories
http://127.0.0.1:5173/hive
http://127.0.0.1:5173/chat
```

## How To Test It

```powershell
cd C:\Users\YourUser\thehomie\dashboard\web
npm run test -- src/__tests__/memory-graph.test.tsx src/__tests__/brain-graph-3d.test.tsx
npm run typecheck
```

```powershell
cd C:\Users\YourUser\thehomie\dashboard\server
npm run test -- src/__tests__/brain.test.ts src/__tests__/routes-manifest.test.ts
npm run typecheck
```

Run the matching `.claude/scripts/tests/test_dashboard_api.py` cases when API
shapes change.

## Latest Live Proof

Tracker records multiple Unified Brain and memory graph browser validations
from May 2026. Re-run Browser validation for current UI claims.

## Related Handoffs

- `PRPs/active/TRACKER.md`
- `docs/vault-setup.md`

## Public Export Status

Dashboard/memory graph slices have been exported in prior framework work; verify
current public mirror state before making a new claim.

## Next Slices

- Split Unified Brain, memory graph, and chat observer into deeper pages.
- Add current graph counts/proof after the next memory UI change.
