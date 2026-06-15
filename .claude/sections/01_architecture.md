### Vertical Slice Architecture

The Homie is one framework with two implementation surfaces:

- `thehomie` = runtime, memory, CLI, adapters, hooks, and cognition
- `mission-control` = optional consumer surface (not part of the framework — one consumer of the runtime, memory, and orchestration APIs)

Within that framework, behavior is organized as vertical slices. Group behavior by the owning surface and the owning slice instead of collapsing everything into one shared horizontal utility layer.

| Slice | Ownership |
|------|-----------|
| `.claude/chat/` | Operator interfaces, routing, session persistence, platform adapters |
| `.claude/scripts/runtime/` | Reasoning runtime boundary, provider selection, auth-profile resolution, fallback, Langfuse tracing |
| `.claude/hooks/` | Session lifecycle hooks and flush behavior |
| `.claude/scripts/` | Scheduled jobs, memory pipelines, orchestration scripts |
| `.claude/chat/cognition/` | Cognitive modules — recall, processes, regions, capture, promotion, graph, continuity, self-model. **The Living Self slice** (cognitive system, 2026-06-13): `operator_beliefs` (Act 1 — model the operator from verbatim turns), `belief_conflicts` (Act 2 — the contradiction engine; distinct from `contradictions.py`, a docs-drift linter), `cognitive_pass` + `proactive_actions` (Act 3 — the gated inner monologue), `evidence_gate` + `amendments` (Act 4 — the evidence-read + default-deny adoption gate). Operator/architecture manual: `docs/the-living-self-manual.md` |
| `.claude/scripts/evolve/` | The Living Self test-and-keep engine (ASI-Evolve-inspired) — `evolve_loop.py` (the `propose` recall rail + the `propose-belief` identity rail), `judge.py` (scheduled belief judge), `belief_regression.py` (the deterministic floor), plus the recall replay/compare/veto harness. Archon drives candidate search; this slice is the fitness oracle |
| `.claude/scripts/orchestration/` | Convoy/mailbox service layer, executor adapters, local API (port 4322), contract |
| `.claude/scripts/integrations/` | Direct platform API integrations |
| `.claude/scripts/integrations/finance_*` | Personal finance: bank sync, budget queries, Teller/Plaid clients |
| `.claude/scripts/dashboard_*.py` + `dashboard/server/` + `dashboard/web/` | Dashboard slice — Python framework HTTP API (port 4322), Hono thin proxy (port 3141), Vite+Preact web bundle. Deep context: `dashboard/README.md` (canonical dashboard doc — components, ports, auth, routes) |
| `.claude/scripts/security/` | Cross-cutting security primitives — `patterns.py` (SECRET_PREFIXES single-source-of-truth, ≥27 vendor key prefixes, length-desc sorted), `kill_switches.py` (operator-toggleable refusal counters, KillSwitchDisabled exception, /api/health rich snapshot), `redact.py` (Hermes verbatim port — log-message secret scrubbing at all log call sites; default ON via `_REDACT_ENABLED` import-time snapshot; lazy `__getattr__` re-export so non-redact consumers don't snapshot config-dependent state). Module-only re-exports enforce Rule 3 across consumers (sanitize.py, runtime/subprocess_env.py, lane_router/registry/recall_service, heartbeat HARO, engine/memory_*, voice cascade, persona lifecycle/dashboard_api). PRD-8 Phase 7a introduced kill_switches/patterns; Phase 7b commit-1 added redact.py + voice + persona_mutation/persona_operations switches; Phase 7b commit-2 added cabinet kill-switches at chat-process chokepoints (handle_cabinet/standup/discuss in core_handlers.py) for symmetric refusal counting alongside Phase 5a's API-process gate. |
| `vault/memory/` | Canonical memory substrate |
| `mission-control/src/app/api/` | Hub / Mission Control control-plane APIs |
| `mission-control/src/components/` | Hub / Mission Control GUI panels and interaction surfaces |

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

**Lane-First Routing:**

Tasks route by lane first — `claude_native` vs `generic_runtime` — and only then by a provider inside the lane. Business behavior, slash-command semantics, and scheduled tasks stay lane-agnostic: the assembled prompt/request IS the task, and it must survive a Claude → Codex → Gemini fallback. Provider-specific model economy (e.g. a heartbeat-only Codex model override) belongs in provider profiles or per-request configuration, never in global chat/model state.

**Claude Agent SDK + Max subscription — policy and why the lanes stay separate:**

The `claude_native` lane runs through the Claude Agent SDK backed by a personal Max plan subscription (`~/.claude/.credentials.json`). Anthropic's policy (clarified February 2026, enforced April 2026) is:

- **Allowed**: Personal/local development and experimentation with the Agent SDK using your own Max subscription. This is explicitly encouraged by Anthropic ("We want to encourage local development and experimentation with the Agent SDK and claude -p.").
- **Not allowed**: Third-party harnesses (e.g. OpenClaw) using a subscriber's OAuth tokens — banned April 4, 2026. Also not allowed: building a product for others using the SDK backed by your own subscription.
- **The Homie falls in the allowed bucket** — it is owner's personal agent, running on owner's own subscription, for owner's own use. Not a distributed product, not a third-party harness.

Do not switch the `claude_native` lane to an API key path — that loses the Max plan compute budget and agentic tool access. Do not try to add API-key-based features (e.g. `cache_control` breakpoints) to the Agent SDK path — the SDK goes through `cli.js` and does not expose those knobs to the caller. Keep the two lanes clean and separate.


### Default-Deny Mutation Policy

Any surface that can mutate the outside world — post, send, edit, connect, DM,
write to an external account — ships **default-denied** and only acts through
an explicit, named gate with an audit trail. Discussing an action is never
authorization to run it; a new capability is OFF until an operator (or a
dedicated write PRP) turns the specific gate on.

The pattern: default-deny → explicit capability gate → audit row.

Existing implementations (greppable examples of the invariant):

1. **Integration actions** — `require_integration_action()` + the
   `IntegrationAction` policy in `.claude/scripts/integrations/capabilities.py`
   gate every mutating direct-integration entrypoint (send, post, archive,
   write). See `docs/manual/features/direct-integration-capability-contract.md`.
2. **Capability Gateway** — `.claude/scripts/orchestration/capability_gateway.py`
   (operating-room surface). See `docs/manual/features/capability-gateway.md`.
3. **BrowserOps** — the dashboard `/browser` viewer is read-only; navigation
   goes through registered workflow gates with audit rows; browser write
   actions (post/edit/DM/connect) are default-denied. See
   `docs/manual/features/browserops-browser-viewer.md`.
4. **Social-write gates** — `/linkedin_post`, `/linkedin_connect`, and
   `/reddit comment|post` writes shipped behind per-action operator-approval
   gates (commit `63f28827`): each fires only on the operator's verbatim
   trailing approval phrase, with an audit row + screenshot receipt per attempt.
   `/linkedin_profile edit` is still expected-blocked until a dedicated
   profile-write PRP lands; the nudge job DRAFTS and QUEUES only. See
   `docs/manual/features/social-write-executor.md` and
   `docs/linkedin-automation-playbook.md`.
5. **Cabinet participant turns** — room personas default-deny tools and answer
   directly. See `docs/manual/features/cabinet-rooms.md`.

Same family: the live-lane opt-in gate (dry-run is the default), operator
kill-switches (`.claude/scripts/security/kill_switches.py`), and the skill
discussion/action gates (talking about a skill is not authorization to run it).

When adding a new mutating surface, implement this invariant from day one:
default-deny, explicit gate, audit row.

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

The derived-state framing is an instance of Rule 2 (meta/cache is derived state, never source of truth) — canonical rule text: `vault/memory/MEMORY.md` → Reference → Global Rules.

Preserve these behaviors when changing execution:

- shared bootstrap context
- proactive recall
- session resume
- PreCompact / SessionEnd flush
- daily reflection
- weekly synthesis
- heartbeat
