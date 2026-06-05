# The Homie

**OpenClaw broadened the channels. Hermes pushed the self-improving loop. The Homie is the framework that gives an agent its own opinions, the nerve to tell you when you're wrong, and the memory to grow alongside you.**

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)
![Version](https://img.shields.io/badge/version-1.1.0-blue?style=flat-square)
![Tests](https://img.shields.io/badge/Tests-active%20suite-brightgreen?style=flat-square)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Channels: 6](https://img.shields.io/badge/Channels-Telegram%20%C2%B7%20Slack%20%C2%B7%20Discord%20%C2%B7%20WhatsApp%20%C2%B7%20Web%20%C2%B7%20CLI-4A154B?style=flat-square)

The Homie is an open-source cognitive agent OS. Not a chatbot wrapper - a 9-layer cognitive architecture that gives an AI identity, self-awareness, theory of mind, durable memory, tiered recall, self-improving learning, mental processes, and continuity. Run it locally, on a VPS, or in Docker. Talk to it from Telegram, Slack, Discord, WhatsApp, or the CLI. It monitors your world, remembers what matters, coordinates multi-agent work, and gets smarter with every session.

OpenClaw deserves credit for proving broad agent access. Hermes deserves credit for pushing agents that learn from use. The Homie builds on that lineage, but goes in a different direction: identity is first-class here. This is not just an agent that helps or an agent that learns - it's a partner with memory, judgment, continuity, and a real point of view.

It's not your second brain. It's your homie - a partner with its own soul, its own opinions, and the nerve to tell you when you're wrong. Most AI is built to please, smooth things over, and tell you what you want to hear. This one is built to ride with you, grow with you, and push back when you're off.

---

## Start Here

- Install and first run: [INSTALL.md](INSTALL.md), `install.ps1`, and
  `install.sh`.
- Operator manual: [docs/manual/README.md](docs/manual/README.md). This is
  the public feature map for what shipped, how to run it, how to test it, and
  where each source-of-truth file lives.
- Desktop app: [docs/manual/features/desktop-v0.md](docs/manual/features/desktop-v0.md)
  and [docs/manual/features/desktop-dev-launcher.md](docs/manual/features/desktop-dev-launcher.md).
- Channels and Telegram attachment behavior:
  [docs/manual/features/multi-channel-adapters.md](docs/manual/features/multi-channel-adapters.md).
- Runtime/model control:
  [docs/manual/features/runtime-status-model-control.md](docs/manual/features/runtime-status-model-control.md).

The public mirror also generates `FRAMEWORK.md`, a compact development guide
for framework contributors.

## Current Public Surface

- Local CLI and runtime: `thehomie chat`, `thehomie status`, `thehomie doctor`,
  `/provider`, and `/model`.
- Multi-channel ingress: Telegram, Slack, Discord, WhatsApp, web relay, and
  CLI share one canonical message/session path. Telegram document attachments,
  attachment groups, quick-turn batching, and Queue/Steer follow-up controls
  are documented and live-proven.
- Operating Room and Capability Gateway: dashboard/API operator surfaces stay
  thin over Python-owned orchestration and capability state.
- Desktop v0: Electron shell, local process lifecycle, logs/status, static
  dashboard launch, and an unpacked Windows package smoke. Electron stays thin;
  Python/orchestration remains the source of truth.
- Multi-agent work: convoy DAGs, typed mailbox, team sessions, Team Room, and
  scheduler surfaces.
- Memory and learning: local Markdown vault, hybrid recall, daily/weekly
  synthesis, knowledge compilation, and human-gated self-evolution.

## Proof Boundaries

- Desktop v0 currently proves the Electron shell and unpacked Windows package.
  A signed installer or no-admin installer flow is not claimed yet.
- Cabinet Voice has lifecycle controls and a partial LiveKit spike. The browser
  mic -> transcript -> Cabinet reply path is not claimed ready.
- Optional integrations require user-owned credentials. No private account
  data, local tokens, or machine-specific proof artifacts belong in the public
  export.

---

## What This Feels Like

It's 6:30am. You open a session.

Instead of *"Hi, how can I help you today?"* — you get:

> *"Morning. While you were out — your business had 3 new leads overnight, the loan you flagged is 5 days from maturity, and there's an inbound email from a backlink partner worth reviewing. Yesterday you were mid-decision on the routing refactor. Pick that up, or hit the leads first?"*

You didn't set up a notification. You didn't write a morning brief. The Homie was watching. Its memory isn't a static file you load — it's a living record tended between sessions. Its identity isn't a document you edit — it's a self that amends when the evidence is strong enough.

That's the target. Most of the load-bearing walls are already up: vault, recall, daily reflection, weekly synthesis, dream consolidation, WorkingMemory-owned prompt state, unified proactive briefs across chat and scheduled loops, and the self-evolution replay loop ship today. Ambient monitoring is live through heartbeat; automatic durable-memory amendments remain human-gated.

---

## What You Get

<table>
<tr><td><b>Monitors your world proactively</b></td><td>Heartbeat every 30 min checks your email, calendar, tasks, and metrics. Direct integration actions have a canonical policy contract for read, write, send, archive, and external-post effects. Daily reflection at 8 AM promotes decisions to long-term memory. Weekly synthesis every Sunday detects patterns and updates goals — all running whether you're talking to it or not.</td></tr>
<tr><td><b>Remembers across every session</b></td><td>Local-first Obsidian-compatible vault (SOUL.md, USER.md, MEMORY.md, daily + weekly logs). Hybrid search — FTS5 keyword + FastEmbed ONNX vector (BGE-base-en-v1.5, 768-dim) + LLM re-ranking (haiku, Tier 1). Memory graph with PageRank + betweenness centrality. Proactive recall injected on every message. WORKING.md scratchpad carries open threads across sessions.</td></tr>
<tr><td><b>Compiles knowledge like code</b></td><td>Entity compilation engine (Karpathy LLM Wiki port): ingest a source → extract entities → create/update concept pages → detect connections → flag contradictions. Concept pages in <code>concepts/</code> accumulate claims from multiple sources. Connection articles in <code>connections/</code> link related concepts. Q&A answers filed via <code>/file</code> persist in <code>qa/</code>. Raw sources preserved immutably in <code>raw/</code>. Build log tracks every compilation. 8 entry points — fires automatically during ingest, daily reflection, weekly synthesis, and on-demand via <code>/file</code> or CLI.</td></tr>
<tr><td><b>Gets smarter from experience</b></td><td>Per-turn auto-capture (6 regex triggers) → staging store → batch promotion in daily reflection. Auto-skill generation after 5+ tool calls. InferenceTracker with confidence decay and contradiction detection. Theory of mind built on USER.md. Human-gated amendment proposals can target SELF/SOUL/USER/MEMORY without auto-applying durable identity changes.</td></tr>
<tr><td><b>One brain, six channels</b></td><td>Telegram, Slack, Discord, WhatsApp, Web relay, CLI — all enter through a single canonical ingress. One session model, one recall service, one runtime. Transport identity is separated from conversation identity so sessions survive reconnects.</td></tr>
<tr><td><b>Any model, no lock-in</b></td><td>Claude SDK, OpenAI Codex, Gemini CLI, OpenRouter, OpenAI-compatible — with health-aware fallback, manual <code>/provider</code> + <code>/model</code> control, lane-first runtime (<code>selection.py</code>, <code>lane_router.py</code>), cost tracking, and automatic retry on transient failures.</td></tr>
<tr><td><b>Multi-agent orchestration</b></td><td>Convoy DAGs for multi-subtask dependency tracking. Typed mailbox for agent-to-agent messaging. Team sessions with typed roles, backend fallback, and shared memory. All exposed via a local API on port 4322.</td></tr>
<tr><td><b>Full observability</b></td><td>Every message → one nested Langfuse trace: session lookup → process detection → recall (tier + pipeline) → region assembly → runtime where supported → post-response. Cost, provider, model, and tool calls are tracked when the active runtime exposes them. Sentry/GlitchTip captures unexpected orchestration errors when a DSN is configured.</td></tr>
</table>

---

## The Vault — Brain Substrate, Not Storage

The vault is where The Homie's mind actually lives. Not a notes folder it writes to — the substrate it thinks on. Every recall, every reflection, every promotion reads and writes here. When you edit `SOUL.md`, you're editing the agent's personality. When `concepts/YourBusiness.md` accumulates a new section, the agent learned something.

| Layer | What's in it |
|-------|--------------|
| **Identity** | `SOUL.md` (personality, values, tone), `SELF.md` (self-model — capabilities, failure modes), `USER.md` (you — projects, accounts, preferences) |
| **Memory** | `MEMORY.md` (long-term decisions/lessons), `GOALS.md` (objectives + metrics), `daily/YYYY-MM-DD.md`, `weekly/YYYY-WNN.md`, `WORKING.md` (cross-session scratchpad) |
| **Knowledge graph** | `concepts/` (auto-compiled entity pages), `connections/` (cross-domain insight articles), `qa/` (filed Q&A from `/file`), `raw/` (immutable original sources) |
| **Indexes & log** | `INDEX.md` (whole-wiki catalog, auto-refreshed), `concepts/INDEX.md` (concept drill-down), `LOG.md` (append-only compilation timeline) |
| **Structure** | wikilinks (`[[YourBusiness]]`), backlinks, MOCs, dashboards, Dataview queries, canvases, graph view |
| **Tooling** | `vault_lint.py` (8 health checks, zero LLM cost), `entity_extractor.py` (extract / compile / contradictions / backfill / sweep / index / preserve-raw / archive), automatic raw-source preservation |
| **Pipelines** | daily reflection (8 AM), weekly synthesis (Sunday 8 PM), dream consolidation (post-weekly + on-demand) |
| **Sync state** | `_state/` — memory candidates, self-model inferences, sync manifest. Optional Obsidian Sync via `_state/` exclusion patterns. |

**Is Obsidian required?** No. The vault is plain Markdown — edit it with anything. Obsidian is the *recommended* editor because the wikilinks, backlinks, graph view, Dataview, and canvas all light up natively. The Homie itself only needs the files.

**Where does the vault live?** Default `vault/memory/`, override with `HOMIE_VAULT_DIR=/path/to/your/vault` (env var honored across runtime, bootstrap, heartbeat, team memory, finance, sanitizer).

### Framework vs. adapter

The Homie is provider-agnostic. Claude SDK, Codex, Gemini, OpenRouter, OpenAI-compatible — interchangeable batteries. The framework runs the same regardless. Editor adapters (Claude Code project instructions, hooks, MCP bridges) are integration surfaces *layered on top of* the framework, not part of it. When the heartbeat runs through Codex or Gemini fallback, those editor instructions are not touched.

---

## Quick Install

```bash
git clone https://github.com/your-github-user/thehomie-framework.git
cd thehomie-framework/.claude/scripts
uv sync
cp .env.example .env                  # Add your API keys
uv run python setup_wizard.py         # Interactive onboarding wizard
uv run thehomie chat                  # Start talking
```

Or use the install script:

```bash
# Linux/macOS/WSL
curl -sSL https://raw.githubusercontent.com/your-github-user/thehomie-framework/master/install.sh | bash

# Windows (PowerShell)
irm https://raw.githubusercontent.com/your-github-user/thehomie-framework/master/install.ps1 | iex
```

---

## CLI

```bash
# Chat
thehomie chat                    # Interactive REPL
thehomie chat -q "hello"         # Single query, stdout response
thehomie chat -q "hello" -Q      # JSON output (machine/API contract)
thehomie chat --resume <id>      # Resume session by ID
thehomie chat -c                 # Resume most recent session
thehomie chat -m claude          # Force a specific provider/lane

# In-chat commands (any channel)
/working                         # Show open threads / hypotheses / questions
/working add "text"              # Append to scratchpad
/working resolve <N>             # Move item N to archived
/file                            # File the last bot answer as a vault note (with entity cascade)

# Budget (personal finance, optional)
/budget                          # Snapshot — balances, bills, loans, allocations
/budget transactions             # Last 20 bank transactions
/budget spending                 # Spending by category (current month)
/budget accounts                 # Connected bank accounts
/budget connect                  # Connect new bank (Teller / Plaid)
/forecast                        # Forecast cash flow + bill timing

# System
thehomie setup                   # Interactive onboarding wizard
thehomie setup --check           # Verify all integrations without changing anything
thehomie status                  # System health overview
thehomie status --json           # JSON health report
thehomie doctor                  # Deep diagnostics with actionable fix hints

# Multi-agent convoy
thehomie convoy create ...       # Create convoy with subtasks + deps
thehomie convoy list             # List convoys (optional: --status active)
thehomie convoy show <id>        # Convoy detail + subtask status
thehomie convoy dispatch <sid>   # Dispatch a subtask via executor
thehomie convoy complete <sid>   # Mark subtask complete
thehomie convoy fail <sid>       # Mark subtask failed
thehomie convoy cancel <id>      # Cancel convoy
thehomie convoy add-task <id>    # Add subtask to existing convoy

# Mailbox
thehomie mailbox send ...        # Send typed inter-agent message
thehomie mailbox inbox <agent>   # Check agent inbox
thehomie mailbox claim <agent>   # Claim deliveries
thehomie mailbox ack <did>       # Acknowledge delivery

# Team sessions
thehomie team list               # List active team sessions
thehomie team status <id>        # Team detail + members + mailbox backlog
thehomie team members <id>       # Member list with roles
thehomie team shutdown <id>      # Request graceful shutdown
thehomie team ping <id>          # Bump activity timestamp
thehomie team close <id>         # Force-close team session
```

---

## Architecture

```
CHANNELS                          COGNITIVE ENGINE                    RUNTIME (lane-first)
──────────                        ────────────────                    ─────────────────────
Telegram ─┐                       ChatRouter._handle_inner()          selection.py
Slack ────┤                            │                              lane_router.py
Discord ──┤  IncomingMessage      ConversationEngine                       │
WhatsApp ─┤  ──────────────→          ├─ Tier Gate (rules, no LLM)         ├─ Claude SDK (Max sub)
Web/MC ───┤                           ├─ Recall (dual search+graph)        ├─ Codex CLI (ChatGPT sub)
CLI ──────┘                           ├─ Region Assembly (frozen)          └─ openai-compatible
                                      │   identity · self · user · durable    (Gemini · OpenRouter ·
                                      │   working · recent_conversation       OpenAI · local)
                                      │   + dynamic regions
                                      ├─ Mental Process Detection
                                      ├─ Runtime dispatch              Health-aware fallback,
                                      └─ Post-response learning        manual /provider control,
                                                                       cost tracking, retry

MEMORY SUBSTRATE (the vault)      BACKGROUND PIPELINES               ORCHESTRATION
────────────────                  ────────────────────               ─────────────
Obsidian-compatible Markdown      Heartbeat ───── every 30 min       Convoy DAGs
  SOUL · SELF · USER · MEMORY     Reflection ──── 8 AM daily         Typed mailbox
  GOALS · WORKING · HEARTBEAT     Weekly ──────── Sunday 8 PM        Team sessions
  daily/ · weekly/                Dream ───────── post-weekly +      Backend fallback
  concepts/ · connections/                          on-demand          (auto → paperclip
  qa/ · raw/ · _state/            All via recall_service.recall()      → workflow → local)
  INDEX.md · LOG.md · MOCs        (sole entrypoint, Invariant I-3)   Local API :4322
Hybrid search (FTS5 + 768-dim
  BGE vector + LLM re-rank)       COMPILATION ENGINE
Memory graph (PageRank, BFS)      ──────────────────
                                  entity_extractor.py (pure Python heuristic)
                                  Ingest → extract → compile → connect
                                  → contradict → reindex → log → archive
                                  Fires automatically on ingest, /file,
                                  daily reflection, weekly synthesis (8 entry points)
```

### The 9-Layer Cognitive Stack

```
L9  SELF-EVOLUTION    Replay-veto self-amendment loop                          [partial]
                      InferenceTracker + Evolve subsystem live (Tasks 1+2);
                      SOUL/USER auto-amendments unlock 2026-04-28
L8  CONTINUITY        Session persistence, full cognition on resume (no skip),
                      recent_conversation region (600 tok), compaction flush,
                      open-loop tracking
L7  THINKING          WorkingMemory (immutable), MentalProcess state machine
L6  LEARNING          Auto-capture → staging → promotion → skills → inference
L5  RECALL            3-tier gate, dual search, graph traversal, hub boosting
L4  MEMORY            MEMORY.md + daily/weekly logs + hybrid search index
L3  UNDERSTANDING     USER.md + Theory of Mind (inference tracker, confidence)
L2  SELF-AWARENESS    SELF.md — capabilities, patterns, failure modes
L1  IDENTITY          SOUL.md — personality, values, boundaries, tone
L0  FOUNDATION        Obsidian vault graph + MOCs + autolink
```

18 cognition modules in `.claude/chat/cognition/`. See [docs/architecture.md](docs/architecture.md) for the full breakdown.

### The 5 Dimensions of The Homie

L0-L9 is the engineering view. The product story has five dimensions; the
operator-facing public map lives in [docs/manual/README.md](docs/manual/README.md).
Private PRDs, PRPs, and vault notes stay outside the public framework export.

| Dimension | The question it answers | Status |
|-----------|-------------------------|--------|
| **1. Identity** | Who am I, and how do I know? | ✅ SOUL/SELF/USER + briefing engine; ⏳ self-amendment unlocks 2026-04-28 |
| **2. Memory** | What do I know, and how do I find it cheaply? | ✅ Vault + FTS5 + 768-dim BGE vector + graph + LLM re-rank + briefing compression |
| **3. Continuity** | Do I remember yesterday, and can I pick up mid-thought? | ✅ WORKING.md scratchpad + full cognition on resume + dream consolidation |
| **4. Ambient Awareness** | Am I watching when you're not here? | 🔄 Heartbeat live; reliability hardening + ambient monitor tasks in flight |
| **5. Self-Evolution** | Can I grow without manual edits? | 🔄 InferenceTracker + Evolve replay-veto live; SOUL/USER amendments gated on trace clock |

### Framework Invariants

| | Invariant | Rule |
|---|---|---|
| I-1 | Canonical Ingress | All 6 channels enter `ChatRouter._handle_inner()`. No bypasses. |
| I-2 | Durable Session Identity | `session_key` (conversation) separated from `request_id` (transport). |
| I-3 | One Recall Service | `recall_service.recall()` is the sole entrypoint — chat, heartbeat, reflection, weekly. |
| I-4 | UI Through APIs | Mission Control calls framework APIs, not raw DB. |
| I-5 | Runtime Contract | Provider invocation only through `runtime/`. No leaky provider hints. |

---

## Running Your Instance

### Prerequisites

- Python 3.12+ with [uv](https://docs.astral.sh/uv/)
- Claude Code CLI — `npm install -g @anthropic-ai/claude-code` (handles auth + model access)

### Setup

```bash
# 1. Dependencies
cd .claude/scripts && uv sync

# 2. Configure
cp .env.example .env    # Add TELEGRAM_BOT_TOKEN, OWNER_NAME, provider keys

# 3. Integrations (Google OAuth, Asana, Slack)
uv run python setup_auth.py           # Walk through each integration
uv run python setup_auth.py --check   # Verify everything is connected

# 4. Build the memory search index
uv run python memory_index.py --rebuild   # ~80MB ONNX model, one-time download

# 5. Start the agent
uv run python ../chat/main.py             # Foreground
bash ../chat/run_chat.sh                  # Background (writes bot.log, bot.pid)

# 6. Schedule background jobs (Windows — Task Scheduler)
#    Creates: heartbeat (30 min), daily reflection (8 AM),
#             weekly synthesis (Sun 8 PM), dream consolidation (post-weekly + on-demand)
powershell -ExecutionPolicy Bypass -File .claude/scripts/setup_scheduler.ps1   # Run as Admin
```

### Memory Files

Your agent's persistent memory lives in `vault/memory/` by default — override with `HOMIE_VAULT_DIR=/path/to/your/vault`. Auto-loaded at session start (provider-agnostic — works for Claude SDK, Codex, Gemini, OpenRouter).

| File | What It Holds |
|------|---------------|
| `SOUL.md` | Personality, values, communication style, behavioral rules |
| `SELF.md` | Self-model — capabilities, patterns, failure modes |
| `USER.md` | Your profile — projects, accounts, integrations, preferences |
| `MEMORY.md` | Long-term memory — decisions, lessons, important facts |
| `GOALS.md` | Quarterly objectives, key metrics, active projects |
| `HEARTBEAT.md` | What to check and surface each heartbeat run |
| `WORKING.md` | Cross-session scratchpad — open threads, hypotheses, unresolved questions |
| `daily/YYYY-MM-DD.md` | Session logs, heartbeat entries, daily context |
| `weekly/YYYY-WNN.md` | Weekly summaries — patterns, progress, decisions |
| `concepts/`, `connections/`, `qa/`, `raw/` | Auto-compiled knowledge graph (see [Knowledge Compilation](#knowledge-compilation)) |

### Runtime Model Control

Runtime selection is lane-first: `/model claude`, `/model codex`, `/model gemini`, `/model openrouter`, `/model openai`, and `/model auto` choose where the next request runs. Provider-specific model pins use `provider:model`, but Codex also accepts short GPT-style aliases:

```bash
uv run thehomie chat -q "/model codex:default" -Q   # Codex plan default; no --model flag passed
uv run thehomie chat -q "/model codex:gpt-5.5" -Q   # Pin a concrete Codex model
uv run thehomie chat -q "/model gpt5.5" -Q           # Same pin, easier shorthand
uv run thehomie chat -q "/model codex 5.5" -Q        # Same pin, provider + version shorthand
uv run thehomie chat -m codex:gpt-5.5 -q "Reply OK" -Q
```

`codex:default`, `codex latest`, and `gpt latest` clear the Codex model pin and leave the Codex CLI/ChatGPT plan to choose its hidden backend model. Pinned values such as `codex:gpt-5.5`, `gpt5.5`, `gpt 5.5`, `gbt 5.5`, `codex 5.5`, and `codec 5.5` are normalized to `gpt-5.5`.

`/provider`, `/diagnostics`, and `thehomie status --json` report the configured model. When Codex is set to `chatgpt-plan-default`, the CLI/ChatGPT plan chooses the concrete backend model and The Homie reports that backend as unobserved.

### Key Config (`.env`)

| Variable | Description |
|----------|-------------|
| `OWNER_NAME` | Your name — used in heartbeat prompts and memory |
| `HOMIE_VAULT_DIR` | Absolute path to your vault (default `vault/memory/`) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | Comma-separated user IDs allowed to chat |
| `HEARTBEAT_TIMEZONE` | IANA timezone (e.g. `America/Chicago`) |
| `LANGFUSE_SECRET_KEY` | Langfuse API key for observability (optional) |
| `ORCHESTRATION_API_TOKEN` | Bearer token for the local orchestration API (optional) |

Full reference: [INSTALL.md](INSTALL.md)

---

## Memory Search

```bash
cd .claude/scripts

uv run python memory_search.py "query"                    # Hybrid (recommended)
uv run python memory_search.py "query" --mode keyword     # Fast, exact
uv run python memory_search.py "query" --mode semantic    # Conceptual match
uv run python memory_search.py "topic" --path-prefix daily/

uv run python memory_index.py --stats    # Index stats
uv run python memory_index.py --rebuild  # Force full reindex
```

---

## Knowledge Compilation

Ported from [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f): when a document is ingested, the compilation engine extracts entities, creates concept pages, detects connections, and flags contradictions. The vault compounds automatically.

```bash
cd .claude/scripts

# Extract entities from any document (prints JSON)
uv run python entity_extractor.py extract "path/to/doc.md"

# Compile: extract + create/update concept pages + connections
uv run python entity_extractor.py compile "path/to/doc.md" --vault-dir "vault/memory"

# Bootstrap: compile ALL existing vault notes (one-time)
uv run python entity_extractor.py backfill --vault-dir "vault/memory" --dry-run
uv run python entity_extractor.py backfill --vault-dir "vault/memory"

# Sweep: compile only notes without concept coverage
uv run python entity_extractor.py sweep --vault-dir "vault/memory"

# Check contradictions on a concept page
uv run python entity_extractor.py contradictions "vault/memory/concepts/LANGFUSE.md"

# Generate/regenerate concepts/INDEX.md (grouped by entity type)
uv run python entity_extractor.py index --vault-dir "vault/memory"

# Generate/regenerate root INDEX.md (whole-wiki catalog: identity + MOCs + concepts + dirs)
uv run python entity_extractor.py index-root --vault-dir "vault/memory"

# Preserve a source into raw/ as an immutable archive (Karpathy raw/ pattern)
uv run python entity_extractor.py preserve-raw "path/to/source.md" --vault-dir "vault/memory"

# Archive stale orphan concept pages
uv run python entity_extractor.py archive --vault-dir "vault/memory" --dry-run
uv run python entity_extractor.py archive --vault-dir "vault/memory" --page "SOME-SLUG"

# Vault health lint (8 checks, zero LLM cost)
uv run python vault_lint.py --vault-dir "vault/memory"
uv run python vault_lint.py --vault-dir "vault/memory" --check broken_wikilinks
uv run python vault_lint.py --vault-dir "vault/memory" --format json
```

**Knowledge graph structure:**

| Folder | Contents | Created By |
|--------|----------|-----------|
| `concepts/` | Auto-compiled entity pages — accumulate claims from multiple sources | Compilation cascade |
| `connections/` | Cross-cutting insight articles linking 2+ related concepts | Compilation cascade |
| `qa/` | Filed Q&A answers from `/file` bot command | `/file` command |
| `raw/` | Immutable original sources (never modified) | `/vault-ingest` |
| `BUILD-LOG.md` | Chronological record of every compilation run | Compilation cascade |

**When compilation fires automatically:**
- `/vault-ingest` — Steps 2.5 (raw copy), 3.5 (entity cascade), 3.6 (contradictions)
- `/file` — Instant filing of bot answers with entity cascade
- Daily reflection (8 AM) — Compiles entities from yesterday's log
- Weekly synthesis (Sunday 8 PM) — Compiles entities from the weekly note
- `/file` nudge — Auto-suggested after long analytical responses (>800 chars)

**Vault health:** `vault_lint.py` runs 8 checks (orphans, broken wikilinks, frontmatter, tag audit against SCHEMA.md, stale content, page size, index completeness, contradiction scan). Zero LLM cost — pure Python. Wired into daily reflection as an automatic post-step.

Provider-agnostic: entity extraction is pure Python (heuristic — headings, bold, wikilinks, frontmatter). No API calls needed. Heading numbers (`1. `, `3- `) auto-stripped from slugs. The vault-ingest skill's LLM enhances extraction when running in an LLM context.

---

## Orchestration

The local API (port 4322) exposes convoy, mailbox, and team endpoints. The
public operator map starts in [docs/manual/README.md](docs/manual/README.md);
private agent instructions are not part of the public export.

Team dispatch uses a `BackendSelector` with `auto → paperclip → workflow → local` fallback. Team memory is stored per team-id in the vault with secret guardrails (8 credential patterns rejected before write).

---

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

Set `LANGFUSE_ENABLED=true` in `.env` and point `LANGFUSE_BASE_URL` at your instance. The cognitive-loop smoke was validated locally with trace `c14af2029d3188b8a6f7526cda68946d`, which captured root `chat_message` plus `session_lookup`, `process_detection`, `region_assembly`, `recall`, `recall_pipeline`, `classify_tier`, and `post_response`. With `SENTRY_DSN` configured, the SDK also returned an event id for an isolated controlled exception.

---

## Self-Evolution & Replay Safety

Identity files (`SOUL.md`, `USER.md`) don't update by hand. The self-evolution loop captures behavior corrections, accumulates evidence, proposes amendments — and **never auto-applies**. Every proposed amendment is replayed against a stratified golden corpus and rejected if it regresses.

| Component | Where | What it does |
|-----------|-------|--------------|
| **InferenceTracker** | `cognition/self_model.py` | Captures confirmed observations, decays old inferences, surfaces high-confidence beliefs |
| **Skills conflict guard** | `skills.py` | Prevents duplicate skill registration during auto-generation |
| **`evolve` subsystem** | `.claude/scripts/evolve/` | Replay engine for proposed identity / config deltas |
| └─ `replay.py` | | Runs candidate overrides against `golden_queries.json` |
| └─ `replay_tracing.py` | | Tags replay runs into a dedicated `evolve-replay` Langfuse namespace (opt-in via `--trace`, isolated from production cost data) |
| └─ `regression.py` | | Bootstrap CIs + hard-veto on regression against `regression_queries.json` |
| └─ `goldens.py` | | Stratified golden-query management |
| └─ `veto.py` | | Configurable veto rules (schema in `veto_rules.schema.json`) |
| └─ `compare.py` / `statistics.py` | | Side-by-side replay comparison with confidence intervals |

**Status (as of 2026-05-22):**
- ✅ Skills conflict guard + InferenceTracker wired into engine
- ✅ WorkingMemory owns production chat prompt state and completed turns append back into WorkingMemory for before/after proof
- ✅ Unified proactive brief feeds session bootstrap, heartbeat, reflection, weekly synthesis, and dream consolidation
- ✅ Human-gated amendment proposal ledger + bounded contradiction/roadmap-drift findings
- ✅ Phase 2.4: opt-in Langfuse replay tracing (default off, `EVOLVE_TRACE_REPLAYS` env var)
- ✅ Phase 2.6: stratified goldens + bootstrap CIs + regression hard-veto
- ⏳ Automatic durable-memory apply remains intentionally gated behind human approval

**Two-phase ship rhythm:** every Evolve increment goes through ship → adversarial Codex review → harden → claim done. This pattern caught five class-of-bug fixes in one week that unit tests missed and is now part of the project review discipline.

---

## How It Compares

| | OpenClaw | Hermes Agent | The Homie |
|---|---|---|---|
| **Thesis** | Channel breadth - 25+ adapters | Self-improving skills loop | A real partner - identity + memory + proactive judgment + the nerve to push back |
| **Memory** | Plain-text notes | MEMORY.md + FTS session search | 9-layer vault: graph + dual search + daily/weekly synthesis + staged promotion |
| **Knowledge graph** | No | No | Entity compilation engine (Karpathy port) — concept pages, connections, contradictions, Q&A filing, LLM re-ranking |
| **Cognition** | None | Prompt assembly stack | Mental process state machine + theory of mind + region-weighted assembly |
| **Proactive** | No | In-process cron | Heartbeat (30 min) + daily reflection + weekly synthesis — all via unified recall |
| **Observability** | Usage tracking | Basic | Langfuse trace tree per message + Sentry/GlitchTip error capture when configured |
| **Multi-agent** | No | Subagents | Convoy DAGs + typed mailbox + team sessions with backend fallback |

---

## Development

```bash
cd .claude/scripts
uv run pytest tests/ -v          # full active suite
uv run ruff check .              # Lint
uv run ruff format .             # Format
uv run thehomie --help           # Verify CLI
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor guide.

---

## Docker

```bash
docker compose up    # bot + scheduler (heartbeat · reflection · weekly synthesis)
```

---

## License

MIT. Built by The Homie contributors.
