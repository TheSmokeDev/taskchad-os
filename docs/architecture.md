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

## The 9-Layer Cognitive Stack

```
L9  SELF-EVOLUTION    Belief + contradiction engine (operator_beliefs.py,
                      belief_conflicts.py); identity-file amendments behind a
                      default-deny evidence + policy gate (amendments.py,
                      evidence_gate.py); Evolve replay-veto harness
L8  CONTINUITY        Session persistence, full cognition on resume (no skip),
                      recent_conversation region (600 tok), compaction flush,
                      open-loop tracking
L7  THINKING          Immutable WorkingMemory + gated cognitive pass that never
                      enters the transcript (working_memory.py, cognitive_pass.py)
L6  LEARNING          Auto-capture → staging → promotion → skills → inference
L5  RECALL            3-tier gate + dual (keyword+vector) search + 1-hop graph
                      traversal + hub-score boost + Tier-1 haiku re-rank (recall.py)
L4  MEMORY            MEMORY.md + daily/weekly logs + hybrid search index
L3  UNDERSTANDING     USER.md + Theory of Mind (inference tracker, confidence)
L2  SELF-AWARENESS    SELF.md — capabilities, patterns, failure modes
L1  IDENTITY          SOUL.md — personality, values, boundaries, tone
L0  FOUNDATION        Obsidian vault graph + MOCs + autolink
```

29 cognition modules live in `.claude/chat/cognition/`, covered by 606 tests
across 25 files (recall, beliefs, episodes, working memory, session briefs).
Every L5–L9 claim above resolves to a named module and test file — see the
subsystem test map in the README's
[Testing & Quality](../README.md#testing--quality) section. PageRank and
Brandes betweenness are implemented in `graph.py`, but the live recall path
boosts by a simpler link-centrality (hub) score; treat the heavier centrality
measures as available, not as what currently drives ranking.

## The 5 Dimensions of The Homie

L0–L9 is the engineering view. The product story has five dimensions; the
operator-facing public map lives in [manual/README.md](manual/README.md).
Private PRDs, PRPs, and vault notes stay outside the public framework export.

| Dimension | The question it answers | Status |
|-----------|-------------------------|--------|
| **1. Identity** | Who am I, and how do I know? | ✅ SOUL/SELF/USER injected every turn + session-opening briefing engine |
| **2. Memory** | What do I know, and how do I find it cheaply? | ✅ Vault + FTS5 + 768-dim BGE vector + graph + Tier-1 LLM re-rank + briefing compression |
| **3. Continuity** | Do I remember yesterday, and can I pick up mid-thought? | ✅ WORKING.md scratchpad + full cognition on resume + dream consolidation |
| **4. Ambient Awareness** | Am I watching when you're not here? | 🔄 Heartbeat live; reliability hardening + ambient monitor tasks in flight |
| **5. Self-Evolution** | Can I grow without manual edits? | ✅ Belief/contradiction engine + identity amendments behind a default-deny evidence + policy gate; 🔄 broader auto-apply scope still expanding |

## Framework Invariants

| | Invariant | Rule |
|---|---|---|
| I-1 | Canonical Ingress | All 6 channels enter `ChatRouter._handle_inner()`. No bypasses. |
| I-2 | Durable Session Identity | `session_key` (conversation) separated from `request_id` (transport). |
| I-3 | One Recall Service | `recall_service.recall()` is the sole entrypoint — chat, heartbeat, reflection, weekly. |
| I-4 | UI Through APIs | Mission Control calls framework APIs, not raw DB. |
| I-5 | Runtime Contract | Provider invocation only through `runtime/`. No leaky provider hints. |

## Orchestration

The local API (port 4322) exposes convoy, mailbox, and team endpoints. The
public operator map starts in [manual/README.md](manual/README.md); private
agent instructions are not part of the public export.

Team dispatch uses a `BackendSelector` with `auto → paperclip → workflow →
local` fallback. Team memory is stored per team-id in the vault with secret
guardrails (8 credential patterns rejected before write).

## Observability

Langfuse self-hosted or cloud — every message produces a single nested trace:

```
chat_message (ROOT)
  ├─ session_lookup
  ├─ process_detection
  ├─ recall (classify_tier + recall_pipeline)
  ├─ region_assembly
  ├─ runtime execution  ← model/provider/cost tracked where the active runtime exposes it
  └─ post_response
```

Set `LANGFUSE_ENABLED=true` in `.env` and point `LANGFUSE_BASE_URL` at your
instance. A validated cognitive-loop trace covering the full message lifecycle
is documented in [LANGFUSE-PROOF.md](../LANGFUSE-PROOF.md). With `SENTRY_DSN`
configured, unexpected orchestration errors are captured via Sentry/GlitchTip.
