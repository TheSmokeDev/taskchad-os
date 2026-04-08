Five automated pipelines keep memory current. All run through the runtime layer and live in `.claude/scripts/`.

### Heartbeat (Every 30 min)

Proactively checks calendar, email, Asana, and content deadlines. Sends desktop notifications when something needs attention.

| File | Purpose |
|------|---------|
| `heartbeat.py` | Main script — gathers API data, runs runtime-backed reasoning |
| `config.py` | Path constants, active hours, timezone config |
| `notifications.py` | Cross-platform notifications (Windows toast / macOS osascript / Linux notify-send) |
| `shared.py` | State management, daily log helpers, file locking, bash validation |

**Flow:** OS scheduler → `uv run python heartbeat.py` → Python calls APIs → results fed into runtime prompt → runtime reasons → notification or `HEARTBEAT_OK`.

**Auth:** Default uses Claude Code CLI credentials (`~/.claude/.credentials.json`).
**State:** `.claude/data/state/heartbeat-state.json`
**Checklist:** `vault/memory/HEARTBEAT.md`

### Memory Search (On-demand)

Hybrid search (keyword + semantic) over all memory files. Fully local — no API calls.

1. Markdown files chunked into ~400-token overlapping segments
2. FTS5 keyword + sqlite-vec/pgvector semantic search
3. Embeddings via FastEmbed (ONNX, all-MiniLM-L6-v2, 384-dim)
4. Hybrid search: vector similarity (0.7) + keyword score (0.3)

| File | Purpose |
|------|---------|
| `db.py` | Database abstraction — SQLiteMemoryDB or PostgresMemoryDB |
| `memory_index.py` | Chunks markdown, generates embeddings, stores via db.py |
| `memory_search.py` | Keyword/semantic/hybrid search with CLI |
| `embeddings.py` | FastEmbed wrapper with lazy model loading |

```bash
cd .claude/scripts && uv run python memory_search.py "query" --mode keyword --limit 5
cd .claude/scripts && uv run python memory_search.py "query" --mode hybrid --limit 10
cd .claude/scripts && uv run python memory_search.py "topic" --mode hybrid --path-prefix drafts/sent --limit 3
```

**Data:** `.claude/data/memory.db` (git-ignored, regenerable via `memory_index.py`). Model cache at `.claude/data/models/`.

### Proactive Memory Recall (Chat Engine)

When the Telegram bot receives a message (> 20 chars), `engine.py → _recall_memory()` runs FTS5 keyword search (~50ms) and injects top 3 results into the system prompt.

```env
RECALL_ENABLED=true          # Toggle recall on/off
RECALL_MIN_SCORE=0.3         # Minimum FTS5 score to include
RECALL_MAX_RESULTS=3         # Max snippets injected
RECALL_MIN_MSG_LEN=20        # Skip short messages ("hi", "thanks")
```

### Unified Recall Service

`recall_service.py` is the sole runtime entrypoint for all recall (Invariant I-3). Wraps `cognition.recall` with lazy Langfuse imports, graceful degradation if cognition modules are unavailable, and injection defense via `cognition.injection`. Used by: chat engine, heartbeat, reflection, weekly synthesis.

| File | Purpose |
|------|---------|
| `recall_service.py` | Canonical recall interface — `recall(query, context)` → ranked snippets |
| `cognition/recall.py` | Core recall logic — tier classification, dual search, graph traversal, hub boosting |

### Entity Compilation Engine (Karpathy Port)

When a document is ingested, the compilation engine extracts key entities/concepts and creates or updates dedicated concept pages in `vault/memory/concepts/`. This turns the vault from a filing system into a deeply interlinked knowledge graph — ported from Karpathy's LLM Wiki pattern.

**Concept pages** accumulate claims from multiple sources over time. Each ingest can touch 5-15 concept pages. Contradictions between sources are automatically flagged as Obsidian callout blocks.

| File | Purpose |
|------|---------|
| `entity_extractor.py` | Core engine — extraction, compilation, contradiction detection, backfill, sweep, index, archive, CLI |
| `vault_lint.py` | 8 health checks — orphans, broken wikilinks, frontmatter, tags, stale content, page size, index, contradictions |
| `config.py` | `RECALL_RERANK_ENABLED`, `RECALL_RERANK_TOP_N`, `RECALL_RERANK_TIMEOUT_S` |
| `cognition/recall.py` | `_llm_rerank()` — haiku re-ranks top 10 results for Tier 1 queries |

**Compilation triggers (8 entry points):**

| Trigger | When | Automatic? |
|---------|------|-----------|
| `/vault-ingest` Step 3.5 | Ingest any doc | Manual |
| `/file` command | After a good bot answer | Manual (nudged) |
| `/file` nudge | After long analytical response (>800 chars + analysis signals) | Auto-suggested |
| Daily reflection hook | 8 AM daily (after `memory_reflect.py`) | Automatic |
| Weekly synthesis hook | Sunday 8 PM (after `memory_weekly.py`) | Automatic |
| Backfill | One-time bootstrap of all existing vault notes | Manual |
| Sweep | Find and compile notes without concept coverage | Schedulable |
| CLI direct | `entity_extractor.py compile/extract/contradictions` | Manual |

**CLI reference:**

```bash
# Extract entities from a source (prints JSON)
uv run python entity_extractor.py extract "path/to/source.md"

# Compile: extract entities + create/update concept pages
uv run python entity_extractor.py compile "path/to/source.md" --vault-dir "vault/memory"

# Compile from pre-extracted entities (LLM-curated JSON)
uv run python entity_extractor.py compile "source.md" --entities entities.json --vault-dir "vault/memory"

# Backfill: compile all uncompiled vault notes
uv run python entity_extractor.py backfill --vault-dir "vault/memory" --dry-run
uv run python entity_extractor.py backfill --vault-dir "vault/memory"

# Sweep: compile only notes without concept coverage
uv run python entity_extractor.py sweep --vault-dir "vault/memory"

# Check concept page for contradictions
uv run python entity_extractor.py contradictions "vault/memory/concepts/HERMES-AGENT.md"

# Generate/regenerate INDEX.md (grouped by entity type)
uv run python entity_extractor.py index --vault-dir "vault/memory"

# Archive stale orphan concept pages to _archive/concepts/
uv run python entity_extractor.py archive --vault-dir "vault/memory" --dry-run
uv run python entity_extractor.py archive --vault-dir "vault/memory" --page "SOME-SLUG"
uv run python entity_extractor.py archive --vault-dir "vault/memory" --days 180

# Run vault health lint (8 checks, zero LLM cost)
uv run python vault_lint.py --vault-dir "vault/memory"
uv run python vault_lint.py --vault-dir "vault/memory" --check broken_wikilinks --check orphan_pages
uv run python vault_lint.py --vault-dir "vault/memory" --format json
```

**Key design decisions:**
- Concept pages live in flat `vault/memory/concepts/` folder with `tags: [concept, auto-compiled]`
- Confidence threshold: 0.6 (extract up to 15, only compile those above threshold)
- Heuristic extraction (headings, bold, wikilinks, frontmatter) — no LLM API call. The vault-ingest skill's LLM layer enhances extraction when running in an LLM context.
- Heading number stripping: leading `N. ` / `N- ` prefixes removed from entity names before slugging (prevents `1-SYSTEM-ARCHITECTURE.md`)
- LLM re-ranking on recall: haiku model, Tier 1 only, 3s hard timeout, `RECALL_RERANK_ENABLED` env var kill switch
- All hooks are non-blocking (try/except wrapped) — compilation failure never breaks reflection/synthesis
- Dedup: same source can't add to a concept page twice. Different sources accumulate sections.
- Raw source preservation: `/vault-ingest` Step 2.5 copies original to `raw/` (immutable) before compilation
- Lint strips code blocks before wikilink scanning — template `[[Link-1]]` examples in SCHEMA.md won't trigger false positives
- Auto-generated files (daily logs, BUILD-LOG.md, team plans) excluded from frontmatter validation
- Connection articles include `date:` field in frontmatter (auto-generated)

### Daily Reflection (8 AM)

Reviews yesterday's daily log and promotes important items to MEMORY.md. After promotion, compiles entities from the reviewed daily log(s) into concept pages.

| File | Purpose |
|------|---------|
| `memory_reflect.py` | Main script — reviews logs, updates MEMORY.md |
| `run_reflect.bat/.sh` | OS scheduler wrappers |

**State:** `.claude/data/state/reflection-state.json`

### Weekly Synthesis (Sunday 8 PM)

Reviews 7 days of logs, creates `vault/memory/weekly/YYYY-WNN.md`, updates GOALS.md. After synthesis, compiles entities from the new weekly note into concept pages.

| File | Purpose |
|------|---------|
| `memory_weekly.py` | Main script — reviews 7 days, creates weekly summary |
| `run_weekly.bat/.sh` | OS scheduler wrappers |

```bash
uv run python memory_weekly.py              # Run weekly synthesis
uv run python memory_weekly.py --test       # Dry run
uv run python memory_weekly.py --days 14    # Two-week lookback
```

| | Daily Reflection | Weekly Synthesis |
|---|---|---|
| Schedule | Daily 8 AM | Sunday 8 PM |
| Lookback | 1 day | 7 days |
| Output | Updates MEMORY.md | Creates `weekly/YYYY-WNN.md` + updates MEMORY.md + GOALS.md |
| Max chars | 20,000 | 60,000 |
| Max turns | 20 | 30 |

**State:** `.claude/data/state/weekly-state.json`

### Dream Consolidation (Post-Weekly + Manual)

Deep memory consolidation — merges cross-session signal, prunes stale entries, normalizes dates, resolves contradictions. Runs as a post-step of weekly synthesis and is also callable standalone. Provider-agnostic via `run_with_fallback()`.

**4 Phases:**

| Phase | Type | What It Does |
|-------|------|-------------|
| 1. Orient | Pure Python | Reads MEMORY.md stats, lists recent logs, counts concepts |
| 2. Gather Signal | Pure Python | Regex grep for corrections, saves, stalls, repeated entities. Weighted scoring (threshold=4). If no signal → `DREAM_SILENT`, exits without LLM call |
| 3. Consolidate | LLM | Merges signal into MEMORY.md/SELF.md, normalizes dates, resolves contradictions |
| 4. Prune | LLM | Enforces 200-line limit, removes stale entries, verifies wikilink pointers |

| File | Purpose |
|------|---------|
| `memory_dream.py` | Main script — 4-phase pipeline |
| `config.py` | `DREAM_STATE_FILE`, `DREAM_MIN_INTERVAL_HOURS`, `DREAM_SIGNAL_THRESHOLD` |

```bash
uv run python memory_dream.py              # Run dream cycle
uv run python memory_dream.py --test       # Dry run (no file edits)
uv run python memory_dream.py --force      # Skip recency guard
uv run python memory_dream.py --days 14    # Scan 14 days of logs
```

**Key design decisions:**
- Phases 1-2 are zero-cost (pure Python grep) — LLM only invoked when signal found
- Weighted signal scoring: corrections=2, saves=2, stalls=1, repeated_entities=3. Threshold=4
- Crash-safe: state advanced before LLM phases. Failed runs (`result: "failed"`) bypass recency guard for immediate retry
- `post_weekly` flag warns LLM that weekly synthesis just ran (prevents duplication)
- Session flush files filtered by `mtime` (only recent files within `days` window)
- Hermes-inspired patterns: `[SILENT]` suppression, advance-before-execute, cross-platform file locking
- 18 tests (12 Phase 1-2 + 6 adversarial: happy path, failure retry, partial completion, post-weekly, threshold)

**State:** `.claude/data/state/dream-state.json`
**Trigger:** Post-step of weekly synthesis (Sunday 8 PM) + standalone CLI + `/vault-dream` skill (planned)
