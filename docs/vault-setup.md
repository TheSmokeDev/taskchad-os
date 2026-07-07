# Vault Setup

The Homie uses an Obsidian-compatible vault as its persistent memory substrate —
not a notes folder it writes to, but the substrate it thinks on. Every recall,
every reflection, every promotion reads and writes here. When you edit
`SOUL.md`, you're editing the agent's personality. When `concepts/<topic>.md`
accumulates a new section, the agent learned something.

## Quick Start

1. Copy `templates/memory/` to your vault directory (default: `vault/memory/`)
2. Customize `SOUL.md` with your agent's personality
3. Customize `USER.md` with your profile and preferences
4. Set the vault path in `.env` if using a non-default location

**Is Obsidian required?** No. The vault is plain Markdown — edit it with
anything. Obsidian is the *recommended* editor because the wikilinks,
backlinks, graph view, Dataview, and canvas all light up natively. The Homie
itself only needs the files.

**Where does the vault live?** Default `vault/memory/`, override with
`HOMIE_VAULT_DIR=/path/to/your/vault` (the env var is honored across runtime,
bootstrap, heartbeat, team memory, finance, and the sanitizer).

## Vault Layout

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

## Memory Files

Auto-loaded at session start, provider-agnostic — the same files feed Claude
SDK, Codex, Gemini, and OpenRouter runs.

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

## Memory Search

```bash
cd .claude/scripts

uv run python memory_search.py "query"                    # Hybrid (recommended)
uv run python memory_search.py "query" --mode keyword     # Fast, exact
uv run python memory_search.py "query" --mode semantic    # Conceptual match
uv run python memory_search.py "topic" --path-prefix daily/

uv run python memory_index.py --stats    # Index stats
uv run python memory_index.py --rebuild  # Force full reindex (~80MB ONNX model, one-time download)
```

## Knowledge Compilation

Ported from [Karpathy's LLM Wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f):
when a document is ingested, the compilation engine extracts entities, creates
concept pages, detects connections, and flags contradictions. The vault
compounds automatically.

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

**Vault health:** `vault_lint.py` runs 8 checks (orphans, broken wikilinks,
frontmatter, tag audit against SCHEMA.md, stale content, page size, index
completeness, contradiction scan). Zero LLM cost — pure Python. Wired into
daily reflection as an automatic post-step.

Provider-agnostic: entity extraction is pure Python (heuristic — headings,
bold, wikilinks, frontmatter). No API calls needed. Heading numbers (`1. `,
`3- `) are auto-stripped from slugs. The ingest workflow can enhance extraction
when running in an LLM context.
