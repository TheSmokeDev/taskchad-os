### Vertical Slice Architecture

The Homie is one framework with two implementation surfaces:

- `thehomie` = runtime, memory, CLI, adapters, hooks, and cognition
- `mission-control` = official GUI / control-plane surface at `C:\Users\YourUser\mission-control`

Within that framework, behavior is organized as vertical slices. Group behavior by the owning surface and the owning slice instead of collapsing everything into one shared horizontal utility layer.

| Slice | Ownership |
|------|-----------|
| `.claude/chat/` | Operator interfaces, routing, session persistence, platform adapters |
| `.claude/scripts/runtime/` | Reasoning runtime boundary, provider selection, auth-profile resolution, fallback, Langfuse tracing |
| `.claude/hooks/` | Session lifecycle hooks and flush behavior |
| `.claude/scripts/` | Scheduled jobs, memory pipelines, orchestration scripts |
| `.claude/chat/cognition/` | Cognitive modules — recall, processes, regions, capture, promotion, graph, continuity, self-model |
| `.claude/scripts/orchestration/` | Convoy/mailbox service layer, executor adapters, local API (port 4322), contract |
| `.claude/scripts/integrations/` | Direct platform API integrations |
| `.claude/scripts/integrations/finance_*` | Personal finance: bank sync, budget queries, Teller/Plaid clients |
| `vault/memory/` | Canonical memory substrate |
| `C:\Users\YourUser\mission-control\src\app\api\` | Hub / Mission Control control-plane APIs |
| `C:\Users\YourUser\mission-control\src\components\` | Hub / Mission Control GUI panels and interaction surfaces |

Preferred change shape:

- extend the slice that owns the behavior
- preserve the runtime slice and GUI slice as separate implementation surfaces even when they serve the same product feature
- avoid bypassing slice ownership with ad hoc helpers in unrelated folders
- avoid creating cross-surface abstractions just because both repos touch the same feature
- avoid generic abstractions before a concrete second use exists
- if a change affects chat, runtime, and tests, land the whole vertical slice together

### Runtime And Auth Boundary

Reasoning execution now belongs behind `.claude/scripts/runtime/`, not in direct provider SDK calls spread across the codebase.

Current runtime/auth profile classes:

- `claude`: subscription-backed local Claude Code / Claude Agent SDK path
- `openai_codex`: subscription-backed ChatGPT / Codex path
- `openai-compatible`: API-key or gateway-backed path such as OpenAI API, OpenRouter, or compatible local endpoints

Important:

- `openai_codex` is not the same thing as `openai-compatible`
- provider identity and auth method are separate concerns
- voice STT/TTS stays separate from the main reasoning runtime
- business behavior and slash-command semantics must not depend on one vendor-specific auth path

### Canonical Memory Contract

The Obsidian vault remains the source of truth. `memory.db` and `chat.db` are derived state and caches.

Preserve these behaviors when changing execution:

- shared bootstrap context
- proactive recall
- session resume
- PreCompact / SessionEnd flush
- daily reflection
- weekly synthesis
- heartbeat
