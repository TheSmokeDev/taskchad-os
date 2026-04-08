# Architecture

The Homie follows a **vertical slice architecture** with two implementation surfaces:

- `thehomie` (this repo) — runtime, memory, CLI, adapters, hooks, cognition
- `mission-control` (optional) — GUI / control-plane dashboard

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
