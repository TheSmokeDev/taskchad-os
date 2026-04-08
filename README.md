# The Homie

**OpenClaw broadened the channels. Hermes pushed the self-improving loop. The Homie is the framework that gives an agent its own opinions, the nerve to tell you when you're wrong, and the memory to grow alongside you.**

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)
![Version](https://img.shields.io/badge/version-1.1.0-blue?style=flat-square)
![Tests: 1180+](https://img.shields.io/badge/Tests-1180%2B%20passing-brightgreen?style=flat-square)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Channels: 6](https://img.shields.io/badge/Channels-Telegram%20%C2%B7%20Slack%20%C2%B7%20Discord%20%C2%B7%20WhatsApp%20%C2%B7%20Web%20%C2%B7%20CLI-4A154B?style=flat-square)

The Homie is an open-source cognitive agent OS. Not a chatbot wrapper - a 9-layer cognitive architecture that gives an AI identity, self-awareness, theory of mind, durable memory, tiered recall, self-improving learning, mental processes, and continuity. Run it locally, on a VPS, or in Docker. Talk to it from Telegram, Slack, Discord, WhatsApp, or the CLI. It monitors your world, remembers what matters, coordinates multi-agent work, and gets smarter with every session.

OpenClaw deserves credit for proving broad agent access. Hermes deserves credit for pushing agents that learn from use. The Homie builds on that lineage, but goes in a different direction: identity is first-class here. This is not just an agent that helps or an agent that learns - it's a partner with memory, judgment, continuity, and a real point of view.

It's not your second brain. It's your homie - a partner with its own soul, its own opinions, and the nerve to tell you when you're wrong. Most AI is built to please, smooth things over, and tell you what you want to hear. This one is built to ride with you, grow with you, and push back when you're off.

---

## What You Get

<table>
<tr><td><b>Monitors your world proactively</b></td><td>Heartbeat every 30 min checks your email, calendar, tasks, and metrics. Daily reflection at 8 AM promotes decisions to long-term memory. Weekly synthesis every Sunday detects patterns and updates goals — all running whether you're talking to it or not.</td></tr>
<tr><td><b>Remembers across every session</b></td><td>Local-first Obsidian vault (SOUL.md, USER.md, MEMORY.md, daily + weekly logs). Hybrid search — FTS5 keyword + FastEmbed ONNX vector (384-dim) + LLM re-ranking (haiku, Tier 1). Memory graph with PageRank + betweenness centrality. Proactive recall injected on every message.</td></tr>
<tr><td><b>Compiles knowledge like code</b></td><td>Entity compilation engine (Karpathy LLM Wiki port): ingest a source → extract entities → create/update concept pages → detect connections → flag contradictions. Concept pages in <code>concepts/</code> accumulate claims from multiple sources. Connection articles in <code>connections/</code> link related concepts. Q&A answers filed via <code>/file</code> persist in <code>qa/</code>. Raw sources preserved immutably in <code>raw/</code>. Build log tracks every compilation. 8 entry points — fires automatically during ingest, daily reflection, weekly synthesis, and on-demand via <code>/file</code> or CLI.</td></tr>
<tr><td><b>Gets smarter from experience</b></td><td>Per-turn auto-capture (6 regex triggers) → staging store → batch promotion in daily reflection. Auto-skill generation after 5+ tool calls. InferenceTracker with confidence decay and contradiction detection. Theory of mind built on USER.md.</td></tr>
<tr><td><b>One brain, six channels</b></td><td>Telegram, Slack, Discord, WhatsApp, Web relay, CLI — all enter through a single canonical ingress. One session model, one recall service, one runtime. Transport identity is separated from conversation identity so sessions survive reconnects.</td></tr>
<tr><td><b>Any model, no lock-in</b></td><td>Claude SDK, OpenAI Codex, Gemini CLI, OpenRouter, OpenAI-compatible — with health-aware fallback, per-task routing (text tasks route cheaper-first), cost tracking, and automatic retry.</td></tr>
<tr><td><b>Multi-agent orchestration</b></td><td>Convoy DAGs for multi-subtask dependency tracking. Typed mailbox for agent-to-agent messaging. Team sessions with typed roles, backend fallback, and shared memory. All exposed via a local API on port 4322.</td></tr>
<tr><td><b>Full observability</b></td><td>Every message → single nested Langfuse trace with 9 spans: session lookup → process detection → recall (tier + pipeline) → region assembly → runtime (auto-instrumented) → post-response. Cost, provider, model, and tool calls per trace.</td></tr>
</table>

---

## Quick Install

```bash
git clone https://github.com/thehomie-framework/thehomie.git
cd thehomie/.claude/scripts
uv sync
cp .env.example .env        # Add your API keys
uv run thehomie setup       # Interactive onboarding wizard
uv run thehomie chat        # Start talking
```

Or use the install script:

```bash
# Linux/macOS/WSL
curl -sSL https://raw.githubusercontent.com/thehomie-framework/thehomie/master/install.sh | bash

# Windows (PowerShell)
irm https://raw.githubusercontent.com/thehomie-framework/thehomie/master/install.ps1 | iex
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
thehomie chat -m claude          # Force a specific provider

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
CHANNELS                          COGNITIVE ENGINE                    RUNTIME
──────────                        ────────────────                    ───────
Telegram ─┐                       ChatRouter._handle_inner()          Claude SDK
Slack ────┤                            │                              Codex CLI
Discord ──┤  IncomingMessage      ConversationEngine                  Gemini CLI
WhatsApp ─┤  ──────────────→          ├─ Tier Gate (rules, no LLM)    OpenRouter
Web/MC ───┤                           ├─ Recall (dual search+graph)   OpenAI-compat
CLI ──────┘                           ├─ 9-Region Assembly            ─────────────
                                      ├─ Mental Process Detection     Health-aware
                                      ├─ Runtime dispatch             fallback with
                                      └─ Post-response learning       cost tracking

MEMORY SUBSTRATE                  BACKGROUND JOBS                 ORCHESTRATION
────────────────                  ───────────────                 ─────────────
Obsidian vault (canonical)        Heartbeat ─── every 30 min     Convoy DAGs
  SOUL · SELF · USER · MEMORY     Reflection ── 8 AM daily       Typed mailbox
  daily/ · weekly/ · GOALS        Weekly ────── Sunday 8 PM      Team sessions
  concepts/ · connections/ · qa/  All via recall_service.recall() Local API :4322
Hybrid search (FTS5 + vector)
Memory graph (PageRank, BFS)    COMPILATION ENGINE
LLM re-ranking (haiku, Tier 1)  ──────────────────
                                entity_extractor.py (pure Python)
                                Ingest → extract → compile → connect
                                → contradict → reindex → build log
```

### The 9-Layer Cognitive Stack

```
L9  SELF-EVOLUTION    Confirmed inferences → proposed identity amendments    [planned]
L8  CONTINUITY        Session persistence, compaction flush, open-loop tracking
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

# 6. Schedule background jobs (Windows)
& ".claude\scripts\setup_scheduler.ps1"  # Run as Admin — creates Task Scheduler jobs
```

### Memory Files

Your agent's persistent memory lives in `Memory/` (your Obsidian vault). Auto-loaded at session start.

| File | What It Holds |
|------|---------------|
| `SOUL.md` | Personality, values, communication style, behavioral rules |
| `USER.md` | Your profile — projects, accounts, integrations, preferences |
| `MEMORY.md` | Long-term memory — decisions, lessons, important facts |
| `GOALS.md` | Quarterly objectives, key metrics, active projects |
| `HEARTBEAT.md` | What to check and surface each heartbeat run |
| `daily/YYYY-MM-DD.md` | Session logs, heartbeat entries, daily context |
| `weekly/YYYY-WNN.md` | Weekly summaries — patterns, progress, decisions |

### Key Config (`.env`)

| Variable | Description |
|----------|-------------|
| `OWNER_NAME` | Your name — used in heartbeat prompts and memory |
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

The local API (port 4322) exposes convoy, mailbox, and team endpoints. See the orchestration section in CLAUDE.md for the full endpoint reference.

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
  ├─ run_with_fallback  ← auto-instrumented SDK, cost + model tracked
  └─ post_response
```

Set `LANGFUSE_ENABLED=true` in `.env` and point `LANGFUSE_BASE_URL` at your instance.

---

## How It Compares

| | OpenClaw | Hermes Agent | The Homie |
|---|---|---|---|
| **Thesis** | Channel breadth - 25+ adapters | Self-improving skills loop | A real partner - identity + memory + proactive judgment + the nerve to push back |
| **Memory** | Plain-text notes | MEMORY.md + FTS session search | 9-layer vault: graph + dual search + daily/weekly synthesis + staged promotion |
| **Knowledge graph** | No | No | Entity compilation engine (Karpathy port) — concept pages, connections, contradictions, Q&A filing, LLM re-ranking |
| **Cognition** | None | Prompt assembly stack | Mental process state machine + theory of mind + region-weighted assembly |
| **Proactive** | No | In-process cron | Heartbeat (30 min) + daily reflection + weekly synthesis — all via unified recall |
| **Observability** | Usage tracking | Basic | Full Langfuse depth — 9 nested spans per message, cost per trace |
| **Multi-agent** | No | Subagents | Convoy DAGs + typed mailbox + team sessions with backend fallback |

---

## Development

```bash
cd .claude/scripts
uv run pytest tests/ -v          # 1180+ tests
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

MIT. Built by YourTech.
