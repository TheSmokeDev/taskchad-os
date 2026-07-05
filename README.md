# The Homie

**An open-source cognitive agent OS — not a chat wrapper. A 9-layer cognitive stack (29 modules in `.claude/chat/cognition/`), DAG-based multi-agent orchestration, a typed inter-agent mailbox, and a provider-agnostic lane-first runtime, with 4,262 tests across 230 files.**

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)
![Public Preview](https://img.shields.io/badge/public%20preview-v0.1.0--alpha.1-blue?style=flat-square)
![Tests: 4262](https://img.shields.io/badge/tests-4%2C262%20across%20230%20files-brightgreen?style=flat-square)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Channels: 6](https://img.shields.io/badge/Channels-Telegram%20%C2%B7%20Slack%20%C2%B7%20Discord%20%C2%B7%20WhatsApp%20%C2%B7%20Web%20%C2%B7%20CLI-4A154B?style=flat-square)

Run it locally, on a VPS, or in Docker. Talk to it from Telegram, Slack, Discord, WhatsApp, the web, or the CLI — all six channels enter one canonical ingress and share one session model, one recall service, and one runtime. It monitors your world on a heartbeat, remembers what matters in a Markdown vault, coordinates multi-agent work over a dependency-tracked convoy graph, and runs the same on Claude, Codex, Gemini, or any OpenAI-compatible backend.

What sets it apart from a linear chat agent is the cognition stack underneath: a frozen, token-budgeted prompt-region system over an immutable working memory; tiered recall with keyword + vector search, graph traversal, and hub-score boosting; an operator-belief and contradiction engine; a gated inner monologue that never enters the transcript; and durable identity-file amendments that only land after passing a default-deny evidence + policy gate. Every one of those is a shipped, tested module — the [Cognition Stack](#the-9-layer-cognitive-stack) section maps each claim to its file and test count.

It is built to push back, not just please — identity (`SOUL.md` / `SELF.md`) is a first-class, version-controlled input to every turn, and the framework forms operator beliefs and flags contradictions instead of agreeing by default. That behavior is a property of the belief engine and the evidence gate below, not a tagline.

## Lineage + Provenance

The Homie is the original public Homie framework export maintained by The
Homie contributors. It evolved from Cole and the Dynamous Community's Claude
Code The Homie workshop, then grew into an identity-first agent OS with its
own memory, orchestration, multi-channel ingress, Operating Room, and desktop
surfaces.

OpenClaw, Hermes Agent, OpenSouls, and ClaudeClaw are credited as ecosystem
influences. The Homie is an independent project and is not affiliated with,
sponsored by, or endorsed by those projects. See [NOTICE.md](NOTICE.md) and
[AUTHORS.md](AUTHORS.md).

---

## Watch The Demo

![The Homie v0.1.0-alpha.1 dashboard product tour](https://github.com/TheSmokeDev/taskchad-os/releases/download/v0.1.0-alpha.1/the-homie-v0.1.0-alpha.1-demo-preview.gif)

45-second product tour: dashboard, Desktop Stack controls, Mobile Access,
Browser Viewer, Work Queue, Convoy, Operating Room, and clean shutdown proof.

Full-quality MP4 is attached on the
[v0.1.0-alpha.1 release](https://github.com/TheSmokeDev/taskchad-os/releases/tag/v0.1.0-alpha.1).

---

## Proof: Tests + Operator Loops

The suite is **4,262 test functions across 230 files** in `.claude/scripts/tests/`
(count it yourself: `git ls-files '.claude/scripts/tests/test_*.py' | wc -l` for
files, and a `def test_` grep for functions). Coverage is concentrated where the
moat is, not spread thin across getters:

| Subsystem | Tests | Where |
|-----------|-------|-------|
| Orchestration (convoy / mailbox / team / executor) | 322 across 13 files | `test_orchestration_api.py` (73), `test_executor_boundary.py` (45), team suite (136 / 8 files) |
| Cognition + memory (recall, beliefs, episodes, briefs) | 606 across 25 files | living-self acts (192 / 4 files), `test_living_memory.py` (48), `test_episodes.py` (58), `test_session_brief.py` (51), `test_recall_*.py` (24) |
| Runtime + lane routing | 70 across 6 files | `test_selection_*.py`, `test_lane_*.py`, `test_runtime_*.py` |
| Memory pipelines | 54 across 4 files | `test_memory_*.py` |
| Observability (Langfuse) | 27 | `test_langfuse.py` |

On top of unit coverage, the framework is exercised through operator-loop and
smoke testing, not just happy-path assertions:

- Fresh public Windows install smoke from a clean clone — install, setup check,
  real CLI chat, Desktop launch, route checks, clean shutdown.
- Real CLI chat proof after setup, using the same runtime path as channels.
- Desktop package and portable-app smokes with Python/Hono lifecycle startup,
  route checks, and clean shutdown.
- Dashboard route smokes across `/mission`, `/chat`, `/mobile`, `/browser`,
  `/work`, `/convoy`, and `/teams`.
- Langfuse trace validation for the message lifecycle: session lookup, process
  detection, recall, region assembly, runtime execution where supported, and
  post-response.
- Sanitizer/export leak checks before public release so private vault data,
  local tokens, and machine-specific proof artifacts stay out of the framework.

Numbers above are from the current export; rerun the grep after pulling to
confirm. Proof boundaries (what is *not* yet claimed) are listed under
[Current Proof Boundaries](#current-proof-boundaries).

---

## Quick Install

```bash
# Linux/macOS/WSL
curl -sSL https://raw.githubusercontent.com/TheSmokeDev/taskchad-os/master/install.sh | bash
```

```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/TheSmokeDev/taskchad-os/master/install.ps1 | iex
```

Manual path:

```bash
git clone https://github.com/TheSmokeDev/taskchad-os.git
cd taskchad-os/.claude/scripts
uv sync
cp .env.example .env
uv run python setup_wizard.py
uv run thehomie chat
```

## Getting Started

```bash
thehomie chat                    # Start a conversation
thehomie setup                   # Configure providers and integrations
thehomie setup --check           # Verify setup without changing anything
thehomie status --json           # Machine-readable health report
thehomie doctor                  # Diagnostics with fix hints
thehomie desktop --shell         # Launch the Desktop dashboard app
thehomie team list               # Inspect team sessions
```

## What You Get

<table>
<tr><td><b>Monitors your world proactively</b></td><td>Heartbeat every 30 min checks your email, calendar, tasks, and metrics. Direct integration actions have a canonical policy contract for read, write, send, archive, and external-post effects. Daily reflection at 8 AM promotes decisions to long-term memory. Weekly synthesis every Sunday detects patterns and updates goals — all running whether you're talking to it or not.</td></tr>
<tr><td><b>Remembers across every session</b></td><td>Local-first Obsidian-compatible vault (SOUL.md, USER.md, MEMORY.md, daily + weekly logs). Hybrid search — FTS5 keyword + FastEmbed ONNX vector (BGE-base-en-v1.5, 768-dim) + LLM re-ranking (haiku, Tier-1 queries only, hard timeout). Recall does 1-hop memory-graph traversal and boosts hub notes by link-centrality score (<code>cognition/recall.py</code>, <code>cognition/graph.py</code>; PageRank + Brandes betweenness are also implemented in the graph module). Proactive recall injected on every message; WORKING.md scratchpad carries open threads across sessions.</td></tr>
<tr><td><b>Compiles knowledge like code</b></td><td>Entity compilation engine (Karpathy LLM Wiki port): ingest a source → extract entities → create/update concept pages → detect connections → flag contradictions. Concept pages in <code>concepts/</code> accumulate claims from multiple sources. Connection articles in <code>connections/</code> link related concepts. Q&A answers filed via <code>/file</code> persist in <code>qa/</code>. Raw sources preserved immutably in <code>raw/</code>. Build log tracks every compilation. 8 entry points — fires automatically during ingest, daily reflection, weekly synthesis, and on-demand via <code>/file</code> or CLI.</td></tr>
<tr><td><b>Gets smarter from experience</b></td><td>Per-turn auto-capture (6 regex triggers) → staging store → batch promotion in daily reflection. Auto-skill generation after 5+ tool calls. InferenceTracker with confidence decay. An operator-belief + contradiction engine (<code>cognition/operator_beliefs.py</code>, <code>cognition/belief_conflicts.py</code>) extracts beliefs from verbatim operator turns and flags conflicts via an embedding pre-filter plus a batched LLM judge — explicit operator statements are never overruled by the model. Durable identity-file amendments (SELF/SOUL/USER/MEMORY) are proposed to an append-only ledger and only land after a default-deny evidence + policy gate (confidence floor, vault-confined evidence read, secret rejection), with a rollback snapshot per apply (<code>cognition/amendments.py</code>, <code>cognition/evidence_gate.py</code>).</td></tr>
<tr><td><b>One brain, six channels</b></td><td>Telegram, Slack, Discord, WhatsApp, Web relay, CLI — all enter through a single canonical ingress. One session model, one recall service, one runtime. Transport identity is separated from conversation identity so sessions survive reconnects.</td></tr>
<tr><td><b>Any model, no lock-in</b></td><td>Claude SDK, OpenAI Codex, Gemini CLI, OpenRouter, OpenAI-compatible — with health-aware fallback, manual <code>/provider</code> + <code>/model</code> control, lane-first runtime (<code>selection.py</code>, <code>lane_router.py</code>), cost tracking, and automatic retry on transient failures.</td></tr>
<tr><td><b>Many homies, one framework</b></td><td>Multi-persona roster — register specialized homies (a business homie, a finance homie, a sales homie), each with its own identity, memory, tools, and voice. Drop them in a Cabinet room and they debate, vote, and ship proof together, with roster and turn order owned by the framework, not improvised by the model.</td></tr>
<tr><td><b>Watch the browser homie work</b></td><td>The browser homie drives a real visible Chrome session you watch live in the dashboard's read-only viewer — not a headless black box. Navigation goes through workflow gates with audit rows, and write actions like posting, editing, and DMs are default-denied until you greenlight them.</td></tr>
<tr><td><b>Multi-agent orchestration</b></td><td>Convoy DAGs with real dependency edges: completing a subtask decrements <code>remaining_dependencies</code> on downstream tasks and releases the newly-ready ones (true parallel release, not a linear queue). Dispatch claims each subtask with a compare-and-swap before the executor is ever called, and external completion callbacks are exactly-once (UNIQUE <code>attempt_key</code> + <code>INSERT OR IGNORE</code> on an idempotency key), so duplicate webhooks can't corrupt convoy state. A typed inter-agent mailbox (<code>msg_type</code> payloads) with a claim → ack lifecycle and claim-token ownership checks. Team sessions with typed roles, <code>auto → paperclip → workflow → local</code> backend fallback, and per-team vault memory. Frozen state machine (transition maps, terminal sets, field allowlists) in <code>orchestration/contract.py</code>; all of it on a local API at port 4322. 322 tests across 13 files (<code>test_orchestration_api.py</code>, <code>test_executor_boundary.py</code>, the team suite).</td></tr>
<tr><td><b>Full observability</b></td><td>Every message → one nested Langfuse trace: session lookup → process detection → recall (tier + pipeline) → region assembly → runtime where supported → post-response. Cost, provider, model, and tool calls are tracked when the active runtime exposes them. Sentry/GlitchTip captures unexpected orchestration errors when a DSN is configured.</td></tr>
</table>

---

## Documentation

| Start here | What it covers |
|---|---|
| [Install Guide](INSTALL.md) | Prerequisites, setup wizard, channel credentials, Docker, systemd, vault setup |
| [Operator Manual](docs/manual/README.md) | Public feature map, source-of-truth files, operator entry points, tests, proof boundaries |
| [Desktop v0](docs/manual/features/desktop-v0.md) | Dashboard-first Electron app, portable/package smoke proof, Desktop/Hono/Python lifecycle |
| [Multi-Channel Adapters](docs/manual/features/multi-channel-adapters.md) | Telegram attachments, grouped documents, quick-turn batching, Queue/Steer controls |
| [Runtime Status And Model Control](docs/manual/features/runtime-status-model-control.md) | `/provider`, `/model`, lane-first runtime behavior, quiet JSON contract |
| `FRAMEWORK.md` | Compact development guide generated during public framework export |

## Current Proof Boundaries

- Desktop v0 proves the dashboard-first Electron app plus unpacked and
  portable no-admin Windows artifacts. A signed installer is not claimed yet.
- Fresh public Windows install smoke has proven install, setup check, real CLI
  chat, Desktop launch, route checks, and clean shutdown from a clean clone.
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

The load-bearing walls are up. The "while you were out" brief is a shipped feature — the Session Opening Brief composes fresh heartbeat observations, new threads, episodes written while you were away, and applied memory amendments into the first turn after an absence, with zero extra LLM calls (`cognition/proactive_brief.py`, 51 tests in `test_session_brief.py`). Vault, tiered recall, daily reflection, weekly synthesis, dream consolidation, WorkingMemory-owned prompt state, and the self-evolution replay loop all ship today. Ambient monitoring runs on the heartbeat; durable identity amendments only apply after clearing the default-deny evidence + policy gate described below.

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
Memory graph (1-hop + hub boost)  ──────────────────
                                  entity_extractor.py (pure Python heuristic)
                                  Ingest → extract → compile → connect
                                  → contradict → reindex → log → archive
                                  Fires automatically on ingest, /file,
                                  daily reflection, weekly synthesis (8 entry points)
```

### The 9-Layer Cognitive Stack

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

29 cognition modules in `.claude/chat/cognition/`, covered by 606 tests across
25 files (recall, beliefs, episodes, working memory, session briefs). Every L5–L9
claim above resolves to a named module and test file — see the
[Proof](#proof-tests--operator-loops) table. PageRank and Brandes betweenness are
implemented in `graph.py`, but the live recall path boosts by a simpler
link-centrality (hub) score; treat the heavier centrality measures as available,
not as what currently drives ranking. Full breakdown: [docs/architecture.md](docs/architecture.md).

### The 5 Dimensions of The Homie

L0-L9 is the engineering view. The product story has five dimensions; the
operator-facing public map lives in [docs/manual/README.md](docs/manual/README.md).
Private PRDs, PRPs, and vault notes stay outside the public framework export.

| Dimension | The question it answers | Status |
|-----------|-------------------------|--------|
| **1. Identity** | Who am I, and how do I know? | ✅ SOUL/SELF/USER injected every turn + session-opening briefing engine |
| **2. Memory** | What do I know, and how do I find it cheaply? | ✅ Vault + FTS5 + 768-dim BGE vector + graph + Tier-1 LLM re-rank + briefing compression |
| **3. Continuity** | Do I remember yesterday, and can I pick up mid-thought? | ✅ WORKING.md scratchpad + full cognition on resume + dream consolidation |
| **4. Ambient Awareness** | Am I watching when you're not here? | 🔄 Heartbeat live; reliability hardening + ambient monitor tasks in flight |
| **5. Self-Evolution** | Can I grow without manual edits? | ✅ Belief/contradiction engine + identity amendments behind a default-deny evidence + policy gate; 🔄 broader auto-apply scope still expanding |

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
- Node.js 22.12+ for dashboard and Desktop v0 assets
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
| `raw/` | Immutable original sources (never modified) | Vault ingest workflow |
| `BUILD-LOG.md` | Chronological record of every compilation run | Compilation cascade |

**When compilation fires automatically:**
- Vault ingest workflow — Steps 2.5 (raw copy), 3.5 (entity cascade), 3.6 (contradictions)
- `/file` — Instant filing of bot answers with entity cascade
- Daily reflection (8 AM) — Compiles entities from yesterday's log
- Weekly synthesis (Sunday 8 PM) — Compiles entities from the weekly note
- `/file` nudge — Auto-suggested after long analytical responses (>800 chars)

**Vault health:** `vault_lint.py` runs 8 checks (orphans, broken wikilinks, frontmatter, tag audit against SCHEMA.md, stale content, page size, index completeness, contradiction scan). Zero LLM cost — pure Python. Wired into daily reflection as an automatic post-step.

Provider-agnostic: entity extraction is pure Python (heuristic — headings, bold, wikilinks, frontmatter). No API calls needed. Heading numbers (`1. `, `3- `) auto-stripped from slugs. The ingest workflow can enhance extraction when running in an LLM context.

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

Identity files (`SOUL.md`, `SELF.md`, `USER.md`, `MEMORY.md`) are not edited blind. The self-evolution loop captures behavior corrections, accumulates evidence, and proposes amendments to an append-only ledger. A proposed amendment **only applies if it clears a default-deny gate** — a confidence floor, a vault-confined evidence read that bounds every cited path, secret rejection, and a deterministic regression floor — and writes a rollback snapshot before it touches the target file. Candidate identity/config deltas are additionally replayed against a stratified golden corpus and hard-vetoed on regression. The gate is the source of truth here, not a tagline: an empty-evidence, high-confidence "I read the doc" amendment is rejected on a real falsifiable check, leaving `SELF.md` byte-unchanged (see `tests/test_living_self_act4.py`).

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

**What's shipped:**
- ✅ Skills conflict guard + InferenceTracker wired into the engine
- ✅ WorkingMemory owns production chat prompt state; completed turns append back into WorkingMemory for before/after proof
- ✅ Unified proactive brief feeds session bootstrap, heartbeat, reflection, weekly synthesis, and dream consolidation
- ✅ Append-only amendment ledger + default-deny evidence/policy gate + rollback snapshots + bounded contradiction/drift findings
- ✅ Opt-in Langfuse replay tracing (default off, `EVOLVE_TRACE_REPLAYS` env var), isolated from production cost data
- ✅ Stratified goldens + bootstrap confidence intervals + regression hard-veto, plus a deterministic belief-regression floor
- 🔄 Broader auto-apply scope for durable identity changes is still expanding behind the same gate

**Two-phase ship rhythm:** every Evolve increment goes through ship → adversarial Codex review → harden → claim done. That review caught a recurring class of bug across five PRs (tunable config bound in default args, derived-cache trusted as source of truth, optional-provider calls bypassing the enabled-flag helper) that unit tests missed — the three anti-patterns are now written up as enforced review rules with grep checks.

---

## How It Compares

| | OpenClaw | Hermes Agent | The Homie |
|---|---|---|---|
| **Thesis** | Channel breadth - 25+ adapters | Self-improving skills loop | A real partner - identity + memory + proactive judgment + the nerve to push back |
| **Interface** | Many chat channels | TUI, CLI, gateway, and desktop workbench | CLI, Telegram/Slack/Discord/WhatsApp/web relay, dashboard, and Desktop v0 shell |
| **Runtime** | Adapter-first routing | Broad provider/model support plus terminal backends | Lane-first runtime with `/provider`, `/model`, status/doctor, and quiet JSON contract |
| **Learning loop** | Notes and commands | Skills from experience, skill improvement, memory nudges, session search | Belief/contradiction engine, evidence-gated identity amendments, staged memory promotion, replay-veto safety |
| **Memory** | Plain-text notes | MEMORY.md, user modeling, FTS session search | 9-layer vault: identity, graph traversal + hub boost, dual search, daily/weekly synthesis, staged promotion |
| **Knowledge graph** | No | Not the focus | Entity compilation engine: concept pages, connections, contradictions, Q&A filing, Tier-1 LLM re-ranking |
| **Operator surface** | Bot-style access | Gateway and terminal workbench | Operating Room, Capability Gateway, Team Room, Desktop v0, public manual surfaces |
| **Multi-agent** | No | Subagents and parallel workstreams | Convoy DAGs with dependency-edge parallel release + exactly-once executor callbacks, typed mailbox, team sessions, backend fallback |

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
cp .claude/scripts/.env.example .claude/scripts/.env
docker compose config
docker compose up    # bot + scheduler (heartbeat · reflection · weekly synthesis)
```

---

## License

MIT. Built by The Homie contributors.
