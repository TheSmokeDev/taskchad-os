### Vertical Slice Architecture

The Homie is one framework with two implementation surfaces:

- `thehomie` = runtime, memory, CLI, adapters, hooks, and cognition
- `mission-control` = optional consumer surface at `C:\Users\YourUser\mission-control` (not part of the framework — one consumer of `thehomie`'s runtime, memory, and orchestration APIs)

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
| `.claude/scripts/dashboard_*.py` + `dashboard/server/` + `dashboard/web/` | Dashboard slice — framework HTTP API on port 4322 (dashboard_api.py + dashboard_db.py + dashboard_bot_lifecycle.py), Hono thin proxy on port 3141 (dashboard/server/), Vite+Preact web bundle (dashboard/web/) |
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

**Claude Agent SDK + Max subscription — policy and why the lanes stay separate:**

The `claude_native` lane runs through the Claude Agent SDK backed by a personal Max plan subscription (`~/.claude/.credentials.json`). Anthropic's policy (clarified February 2026, enforced April 2026) is:

- **Allowed**: Personal/local development and experimentation with the Agent SDK using your own Max subscription. This is explicitly encouraged by Anthropic ("We want to encourage local development and experimentation with the Agent SDK and claude -p.").
- **Not allowed**: Third-party harnesses (e.g. OpenClaw) using a subscriber's OAuth tokens — banned April 4, 2026. Also not allowed: building a product for others using the SDK backed by your own subscription.
- **The Homie falls in the allowed bucket** — it is owner's personal agent, running on owner's own subscription, for owner's own use. Not a distributed product, not a third-party harness.

Do not switch the `claude_native` lane to an API key path — that loses the Max plan compute budget and agentic tool access. Do not try to add API-key-based features (e.g. `cache_control` breakpoints) to the Agent SDK path — the SDK goes through `cli.js` and does not expose those knobs to the caller. Keep the two lanes clean and separate.


### Framework vs. Adapter Boundary

The Homie is provider-agnostic. Claude Code, Codex, Gemini, Kimi, OpenRouter — they're interchangeable batteries. The framework doesn't care which one is running.

This creates two layers with different rules:

| Layer | Examples | Design Rule |
|-------|----------|-------------|
| **Framework** | bootstrap.py, engine.py, recall, memory, cognition, adapters | Must work for ANY provider. No assumptions about provider-specific features (Read tool, CLAUDE.md, hooks). Self-contained. |
| **Adapter** | CLAUDE.md, `.claude/hooks/`, `.claude/settings.json`, MCP bridges | Provider-specific integration surface. Can assume provider capabilities. Not part of the framework. |

Key implications:

- `build_session_start_context()` in bootstrap.py is **framework-level** — its output is the AI's orientation regardless of provider. Design it to be self-contained, not reliant on CLAUDE.md being present.
- CLAUDE.md is **adapter-level** — it tells Claude Code CLI how the codebase works. It is NOT part of The Homie framework. When heartbeat/reflection/synthesis run through Codex or Gemini fallback, nothing from CLAUDE.md is needed or used.
- SOUL.md, SELF.md, USER.md, MEMORY.md are **framework identity** — they travel with the framework and must be injected by bootstrap.py for any provider.
- Don't optimize framework behavior around one provider's specific capabilities (e.g., "the AI can just Read the file" assumes Claude Code's Read tool).

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
