# Architecture

The Homie follows a **vertical slice architecture** with one product and thin surfaces:

- `thehomie` (this repo) — runtime, memory, CLI, adapters, hooks, cognition
- `dashboard/` — the Homie Dashboard and Electron control plane

See `.claude/sections/01_architecture.md` for the full architectural guide.

## Key Slices

| Slice | Ownership |
|-------|-----------|
| `.claude/chat/` | Operator interfaces, routing, session persistence, platform adapters |
| `.claude/scripts/runtime/` | Reasoning runtime boundary, provider selection, fallback, tracing |
| `.claude/scripts/` | Scheduled jobs, memory pipelines, orchestration |
| `.claude/chat/cognition/` | Cognitive modules — recall, processes, regions, capture, promotion |
| `.claude/scripts/orchestration/` | Convoy/mailbox service layer, executor adapters, local API |
| `.claude/scripts/integrations/` | Direct platform API integrations |
