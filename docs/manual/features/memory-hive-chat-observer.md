# Memory, Hive, And Dashboard Chat

Status: active baseline
Owner: Python memory/brain APIs, chat router, and dashboard views
Last updated: 2026-06-08

## What It Does

Memory, Hive, and Dashboard Chat expose Homie memory and conversation state in
the dashboard. `/memories` and `/hive` render memory graphs, brain topology,
and recent activity. `/chat` is now a dashboard-native WEB conversation that
can send text, slash commands, and follow-up controls through the same
Python-owned chat router used by CLI and channel adapters.

## Operator Entry Points

- Dashboard: `/memories`, `/hive`, `/chat`
- API: `/api/memories`, `/api/memory/graph`, `/api/brain/graph`,
  `/api/hive-mind/recent`, `/api/conversation/:id/stream`,
  `/api/conversation/:id/history`, `/api/conversation/:id/send`
- CLI/Telegram memory commands: `/search`, `/file`, `/working`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/dashboard_api.py`, `.claude/hooks/session-end-flush.py`, `.claude/scripts/memory_flush.py`, `.claude/scripts/entity_extractor.py`, `.claude/scripts/vault_lint.py`, memory/recall modules under `.claude/scripts/` and `.claude/chat/` |
| Hono/dashboard server | `dashboard/server/src/routes/memories.ts`, `dashboard/server/src/routes/brain.ts`, `dashboard/server/src/routes/hive-mind.ts`, `dashboard/server/src/routes/conversation.ts`, `dashboard/server/src/routes.ts` |
| Dashboard web | `dashboard/web/src/pages/Memories.tsx`, `dashboard/web/src/pages/HiveMind.tsx`, `dashboard/web/src/pages/Chat.tsx`, graph components/hooks |
| Tests | `dashboard/web/src/__tests__/memory-graph.test.tsx`, `dashboard/web/src/__tests__/brain-graph-3d.test.tsx`, `dashboard/web/src/__tests__/chat.test.tsx`, `dashboard/web/src/__tests__/chat-stream.test.ts`, `dashboard/server/src/__tests__/brain.test.ts`, `dashboard/server/src/__tests__/conversation.test.ts`, SSE/token-hardening tests |

## Safety Boundaries

- Dashboard-native chat writes are text/slash-command/button only. File
  uploads, voice/mic input, browser control, and arbitrary external sends are
  not part of this slice.
- Linked channel streams opened by legacy `chatId` stay read-only in the
  dashboard.
- Python owns routing, runtime invocation, session persistence, and safety
  gates. Hono and the web UI stay thin over those contracts.
- SSE query tokens are limited to the SSE route contract and must be scrubbed.
- Graph/list views must use scoped API contracts, not ad hoc vault mutation.
- Memory writes belong to canonical memory APIs and cognition policy gates.
- `/clear` session-end flush admits any session with at least two readable
  conversation turns; `memory_flush.py` remains the semantic save/drop gate.
- Vault `compile`, `backfill`, and `sweep` refuse source Markdown missing
  `tags` or `date` frontmatter before writing concept pages, build logs,
  indexes, source `related:` updates, or reindex side effects.

## How To Run It

```text
http://127.0.0.1:5173/memories
http://127.0.0.1:5173/hive
http://127.0.0.1:5173/chat
```

## How To Test It

```powershell
cd <repo>\dashboard\web
npm run test -- src/__tests__/memory-graph.test.tsx src/__tests__/brain-graph-3d.test.tsx
npm run typecheck
```

```powershell
cd <repo>\dashboard\server
npm run test -- src/__tests__/brain.test.ts src/__tests__/routes-manifest.test.ts
npm run typecheck
```

Run the matching `.claude/scripts/tests/test_dashboard_api.py` cases when API
shapes change.

Memory flush and vault frontmatter tests:

```powershell
cd <repo>\.claude\scripts
uv run pytest tests/test_memory_flush_gate.py tests/test_entity_extractor.py tests/test_vault_lint.py tests/test_chat_lifecycle_hooks.py tests/test_repository_memory.py tests/test_url_fetch.py -q
```

Dashboard chat write-path tests:

```powershell
cd <repo>\.claude\scripts
uv run pytest tests/test_dashboard_endpoints_sse.py -q
```

```powershell
cd <repo>
npm --prefix dashboard/server test -- src/__tests__/conversation.test.ts
npm --prefix dashboard/web test -- src/__tests__/chat.test.tsx src/__tests__/chat-stream.test.ts
```

## Latest Live Proof

- Date: 2026-06-08
- Local memory/frontmatter proof: focused memory/frontmatter tests passed; adjacent
  lifecycle, vault lint, repository-memory prompt, URL ingest, and concept
  drafter compile tests passed. No Telegram/Discord restart or public export
  was included.
- Date: 2026-06-07
- Dashboard chat reliability pass ran on isolated ports `45139/33157`.
- `/chat` sent a real model-backed `/linkedin ... Do not post.` command
  through the dashboard API and Python-owned chat router. The new stored user
  row preserved the raw operator slash command instead of the internal
  command-expansion prompt.
- A follow-up turn used the prior response as context, then an in-flight
  follow-up rendered existing `Queue Next` and `Steer Current` controls.
  `Steer Current` posted the button action, showed a steer acknowledgement,
  and produced a founder-announcement revision after the active turn finished.
- Progress rendered as one replaceable status card during work instead of
  stacking multiple `Thinking...` / `Working...` bubbles.
- Reloaded history showed the raw slash command and follow-up text, with no
  new internal `Writing Rules` prompt leakage.
- `/browser` rendered Browser Viewer readiness, visible CDP guard, tab count,
  live stream state, and read-only controls; `/teams` rendered Operating Room.
  Neither route showed raw `TypeError: Failed to fetch`.
- The isolated services were stopped and ports `45139/33157` were confirmed
  closed afterward. Existing local services remained on their normal ports.
- Date: 2026-06-06
- Isolated current-code stack passed on alternate ports `45139/33157`.
- `/chat` rendered in the in-app browser and sent real dashboard-composer
  messages:
  - `hello` streamed a normal Homie assistant response.
  - `/provider` streamed `Runtime Provider Status`.
  - `/status` streamed `Session Status`.
- The proof used the dashboard WEB adapter and Python-owned chat routing, not a
  mocked UI response.
- Browser validation showed no raw `TypeError: Failed to fetch`, no console
  warnings, and no `Message failed` toast after the isolated stack was ready.
- Shutdown stopped the isolated Hono and Python services; ports `45139/33157`
  were closed afterward.
- Memory graph and brain views retain prior May 2026 browser validation; re-run
  Browser validation when those UI claims change.

## Public Export Status

Dashboard/memory graph slices have been exported in prior framework work; verify
current public mirror state before making a new claim.

## Next Slices

- Split Unified Brain, memory graph, and dashboard chat into deeper pages.
- Add current graph counts/proof after the next memory UI change.
- Add dashboard file uploads only after the channel attachment contract is
  explicitly designed and tested.
