---
description: Load the chat engine signal flow — router, engine, cognition, recall, cognitive pass
---

# Prime: Signal Slice

## Objective

Build understanding of The Homie's chat engine signal flow — how messages are received, routed, processed through the engine with cognition modules, and how responses are delivered. Signal = the message-processing pipeline: incoming message -> router -> engine -> cognition -> runtime -> response.

## Key Files to Read

Read these files in order. The chat engine lives in `.claude/chat/`; cognition modules live in `.claude/chat/cognition/`.

### Architecture docs (read first)
@.claude/sections/01_architecture.md
@.claude/sections/02_chat_interface.md

### Router and engine (message processing core)
@.claude/chat/router.py
@.claude/chat/engine.py
@.claude/chat/models.py
@.claude/chat/extension_manager.py

### Recall pipeline
@.claude/chat/recall_service.py
@.claude/chat/cognition/recall.py

### Cognitive pass (think before speaking)
@.claude/chat/cognition/cognitive_pass.py
@.claude/chat/cognition/processes.py

## Import Convention Caveat

The chat slice uses **flat sys.path imports** (`import voice`, `import config`, `from voice_markers import ...`) — launchers put `.claude/chat/` on `sys.path`. No static import resolver can see intra-slice edges. A "dead code" verdict inside `.claude/chat/` requires grep confirmation, never resolver/graph output alone.

## Slice Boundaries

- **Owns**: message routing (slash commands vs engine), runtime-backed conversations, recall pipeline (tier classification, dual search), region assembly, cognitive pass, extension/intent detection, session persistence
- **Does NOT own**: memory pipelines (`.claude/scripts/memory_*.py` — see `/prime-memory`), orchestration (`.claude/scripts/orchestration/` — see `/prime-orchestration`), Obsidian vault (`vault/memory/`)
- **Cross-slice touchpoints**: engine calls `run_with_fallback()` from runtime for LLM execution; router dispatches to extension handlers; recall reads from the memory index

## Key Architecture Concepts

1. **Lane-first routing**: tasks route by lane (`claude_native` vs `generic_runtime`), then by provider. Business behavior stays lane-agnostic.
2. **Default-deny mutation policy**: any surface that mutates the outside world ships default-denied with explicit gates and audit trails.
3. **Two paths for data queries**: data-only (0 tokens, ~1-3s) vs analysis (TEXT_REASONING, ~15-20s). Router decides based on intent detection + analysis signals.

## Output

After reading, provide:

### Signal Flow Overview
- Message lifecycle: adapter -> router -> engine -> runtime -> response
- Slash command vs engine routing decision
- Data-only vs analysis path split

### Key Patterns
- Recall tier classification and search modes
- Cognitive pass gating criteria
- Extension manager intent detection
- Session persistence and lifecycle

### Current State
- Adapter count and types (Telegram, CLI, web, Slack, Discord, WhatsApp)
- Registered slash commands
- Active extensions
