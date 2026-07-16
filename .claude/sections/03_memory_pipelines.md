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

### Background Model Tiers (cost guard)

Scheduled jobs must **never inherit the operator's interactive flagship model**
(`SECOND_BRAIN_CLAUDE_MODEL`, e.g. Opus) — a cron job reasoning over pre-gathered
data has no business burning Opus tokens ~48×/day. Each scheduled `RuntimeRequest`
passes an explicit cheap `model=` resolved by `config.get_background_models()`
(Rule 1, call-time), with two tiers:

| Tier | Default | Jobs | Knob |
|------|---------|------|------|
| `fast` | `haiku` | heartbeat (main reasoning + alert formatter + HARO pitch) | `SECOND_BRAIN_BACKGROUND_FAST_MODEL` |
| `quality` | `sonnet` | daily reflection, weekly synthesis, dream (consolidate + prune) | `SECOND_BRAIN_BACKGROUND_QUALITY_MODEL` |

**Lane caveat:** these are Claude-lane aliases applied via `RuntimeRequest.model`.
On generic lanes (Codex/Gemini) `request.model` is ignored and the provider's
own configured model is used (`SECOND_BRAIN_CODEX_MODEL` etc.); the heartbeat
keeps its tested `HEARTBEAT_CODEX_MODEL` override for the codex lane. Per-lane
cheap-background for generic providers is a separate follow-up. The interactive
chat path is unaffected — it still uses `SECOND_BRAIN_CLAUDE_MODEL`.

### Memory Search (On-demand)

Hybrid search (keyword + semantic) over all memory files. Fully local — no API calls.

1. Markdown files chunked into ~400-token overlapping segments
2. FTS5 keyword + sqlite-vec/pgvector semantic search
3. Embeddings via FastEmbed (ONNX, BAAI/bge-base-en-v1.5, 768-dim native)
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
RECALL_ENABLED=true              # Toggle recall on/off
RECALL_MIN_SCORE=0.3             # Minimum merged score (hybrid/vector scale)
RECALL_KEYWORD_MIN_SCORE=0.02    # Floor for keyword-only recall — raw FTS5 scores are 1/(1+|bm25|), a different scale
RECALL_MAX_RESULTS=3             # Max snippets injected
RECALL_MIN_MSG_LEN=20            # Skip short messages ("hi", "thanks")
```

### Unified Recall Service

`recall_service.py` is the sole runtime entrypoint for all recall (Invariant I-3). Wraps `cognition.recall` with lazy Langfuse imports, graceful degradation if cognition modules are unavailable, and injection defense via `cognition.injection`. Used by: chat engine, heartbeat, reflection, weekly synthesis.

| File | Purpose |
|------|---------|
| `recall_service.py` | Canonical recall interface — `recall(query, context)` → ranked snippets |
| `cognition/recall.py` | Core recall logic — tier classification, dual search, graph traversal, hub boosting |

### Identity Payload Consolidation

`cognition/identity_payload.py` is the canonical reader for identity-file context (SOUL / SELF / USER / MEMORY / GOALS / WORKING). The chat hot path and the cron memory pipelines share this single shim instead of duplicating `read_file_safe()` calls — preventing the "two parallel readers" drift class-of-bug when a new identity file gets added (e.g., a future HABITS.md only needs to be added to `DEFAULT_INCLUDE` and every consumer picks it up). PRD-8 Phase 2 (WS2 + WS3 + WS4).

```python
def build_identity_payload(
    memory_dir: Path,
    *,
    include: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Returns dict keyed by uppercase name. Missing files → key ABSENT."""
```

Fail-open contract (matches `read_file_safe`): missing files surface as absent keys (not empty string, not `None`); empty `memory_dir` → `{}`; OS errors swallowed by `read_file_safe` and never escape. Consumers keep their own prompt assembly, ordering, and headers — the shim only hands back raw content. No file-content caching at module scope (Rule 2); `include` is a `None` sentinel resolved inside the body (Rule 1).

| Consumer | Call site | What it consumes |
|----------|-----------|------------------|
| `engine.py:182` | `_build_frozen_regions()` | SOUL / SELF / USER / MEMORY / WORKING — wired into `PromptRegion`s with `config.REGION_BUDGETS` (env-overridable, UNCHANGED). Interleaved `user_inferences` (between user_model and durable_memory) and `procedural_memory` (after working_memory) regions stay verbatim — the shim does NOT touch them. |
| `memory_reflect.py:202` | Daily reflection prompt prologue | MEMORY + USER + SOUL |
| `memory_weekly.py:188` | Weekly synthesis prompt prologue | MEMORY + USER + SOUL + GOALS |
| `memory_dream.py:321` | Dream consolidate phase | MEMORY + SELF + GOALS |
| `memory_dream.py:431` | Dream prune phase | MEMORY |

Parity tests prove byte-equality with the pre-refactor inline-read path: `tests/test_chat_runtime_engine.py::test_frozen_regions_parity_with_shim`, `tests/test_memory_reflect.py::test_prompt_parity_with_shim`, `tests/test_memory_weekly.py::test_prompt_parity_with_shim`, `tests/test_memory_dream.py::test_consolidate_prompt_parity_with_shim` + `::test_prune_prompt_parity_with_shim`.

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
| `/vault-ops ingest` Step 3.5 | Ingest any doc | Manual |
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

# Generate/regenerate concepts/INDEX.md (grouped by entity type)
uv run python entity_extractor.py index --vault-dir "vault/memory"

# Generate/regenerate root INDEX.md (whole-wiki catalog: identity + MOCs + concepts + dirs)
uv run python entity_extractor.py index-root --vault-dir "vault/memory"

# Preserve a source into raw/ as an immutable archive (Karpathy raw/ pattern)
uv run python entity_extractor.py preserve-raw "path/to/source.md" --vault-dir "vault/memory"
uv run python entity_extractor.py preserve-raw "statement.pdf" --vault-dir "finance-vault" --date-prefix

# Archive stale orphan concept pages to _archive/concepts/
uv run python entity_extractor.py archive --vault-dir "vault/memory" --dry-run
uv run python entity_extractor.py archive --vault-dir "vault/memory" --page "SOME-SLUG"
uv run python entity_extractor.py archive --vault-dir "vault/memory" --days 180

# Run vault health lint (8 checks, zero LLM cost)
uv run python vault_lint.py --vault-dir "vault/memory"
uv run python vault_lint.py --vault-dir "vault/memory" --check broken_wikilinks --check orphan_pages
uv run python vault_lint.py --vault-dir "vault/memory" --format json
```

**Vault artifacts (Karpathy LLM Wiki port):**
- `{vault}/INDEX.md` — whole-wiki catalog (auto-refreshed by `compile_entities()` + `index-root` CLI). Single first-read surface covering identity files, MOCs, concepts-by-type (capped at 25/type), and top-level directories.
- `{vault}/concepts/INDEX.md` — concept-only drill-down catalog.
- `{vault}/LOG.md` — append-only chronological timeline of wiki-evolution events (`ingest`, `compile`, `reflect`, `weekly`, `dream`, `archive`). Grep-friendly: `grep "^## \[" LOG.md | tail -5`. Heartbeat excluded (too noisy).
- `{vault}/raw/` — immutable original sources. Never modified. Preserved via `preserve_raw()` helper, invoked by `/vault-ops ingest` Step 2.5 and `finance_ingest.py`.

**Key design decisions:**
- Concept pages live in flat `vault/memory/concepts/` folder with `tags: [concept, auto-compiled]`
- Confidence threshold: 0.6 (extract up to 15, only compile those above threshold)
- Heuristic extraction (headings, bold, wikilinks, frontmatter) — no LLM API call. The vault-ops ingest pipeline's LLM layer enhances extraction when running in an LLM context.
- Heading number stripping: leading `N. ` / `N- ` prefixes removed from entity names before slugging (prevents `1-SYSTEM-ARCHITECTURE.md`)
- LLM re-ranking on recall: haiku model, Tier 1 only, 3s hard timeout, `RECALL_RERANK_ENABLED` env var kill switch
- All hooks are non-blocking (try/except wrapped) — compilation failure never breaks reflection/synthesis
- Dedup: same source can't add to a concept page twice. Different sources accumulate sections.
- Raw source preservation: `/vault-ops ingest` Step 2.5 copies original to `raw/` (immutable) before compilation
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
| 2. Gather Signal | Pure Python | Regex grep over daily logs, raw flush leftovers (STATE_DIR), and open episodes (`episodes/` — Living Mind Act 3) for corrections, saves, stalls, repeated entities. Weighted scoring (threshold=4). If no signal → `DREAM_SILENT`, exits without LLM call |
| 3. Consolidate | LLM | Merges signal into MEMORY.md/SELF.md via the amendment ledger, normalizes dates, resolves contradictions. Prompt carries a capped `## Recent Episodes (open)` digest when open episodes exist; after a successful return, reviewed episodes flip to `status: consolidated` + `consolidated_at:` (pure Python, own try/except — flip failure never fails the dream; consolidate failure leaves episodes open for retry) |
| 4. Prune | LLM | Enforces 200-line limit, removes stale entries, verifies wikilink pointers. Never touches `episodes/` (insert-only autobiography) |

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

### Working Memory (Living Mind Phase 1)

Cross-session scratchpad — open threads, hypotheses, unresolved questions. Solves amnesia between sessions: a file-based curated middle tier between per-session continuity and long-term MEMORY.md. Gary Tan "LLM Memory Unsolved" thesis implementation.

| File | Purpose |
|------|---------|
| `living_memory.py` | Read/write/age primitives — file_lock + atomic writes, regex section parsing, 3-day dedup, insert-only archive |
| `vault/memory/WORKING.md` | Canonical scratchpad (5 fixed sections: Open Threads, Active Hypotheses, Unresolved Questions, Heartbeat Observations (live — ambient heartbeat bullets, Living Mind Act 2), Archived (Cold)) |
| `runtime/bootstrap.py` | `_extract_working_memory()` — compact ~400-char briefing block between rules and active projects |
| `chat/engine.py` | `working_memory` frozen region (600-token budget, full content, after `durable_memory`) |
| `hooks/session-end-flush.py` | `append_open_threads_from_flush()` — 6 regex signals (TODO, waiting on, next up, need to verify, etc.), capped at 3/session, no LLM. Win32 fix (Act 3): session-id sanitized at filename composition (`_safe_filename_component`, `[A-Za-z0-9._-]+`) so colon-bearing chat keys no longer kill the hook; raw id kept for dedup + payload. Spawned flush also writes an episode (see Episodes below) |
| `memory_dream.py` | Phase 2.5 — `archive_stale_working_items()` moves items >`WORKING_MEMORY_AGE_DAYS` (default 7) |

**Commands:**
- `/working` — show active sections (Open Threads / Hypotheses / Questions / Heartbeat Observations)
- `/working add "<text>"` — append to Open Threads with today's date
- `/working resolve <N>` — move item N (1-based) to Archived with `[resolved YYYY-MM-DD]` prefix (Open Threads only — observations are not resolvable; they age out)

**Observability:** 3 Langfuse spans — `living_memory_read`, `living_memory_write`, `living_memory_archive`. Metadata includes `threads_count`, `bytes_read/written`, `archived_count`, `sections_touched`, `threads_skipped_dedup`. All spans fail-open (try/except wraps every Langfuse call).

**Key design decisions:**
- Insert-only archive — Gary Tan invariant, never hard-delete
- Zero LLM in hot path — regex + date math only, <50ms per operation
- Active section caps: Open Threads 10, others 5 (oldest age to archive when exceeded)
- 3-day dedup window via subject prefix match (40 chars, case-insensitive)
- Atomic writes via `tmp + os.replace()` under `shared.file_lock()` (cross-platform, 5s timeout)
- Frontmatter `date:` field refreshed on every write
- Living Mind program COMPLETE: heartbeat observation writes SHIPPED in Act 2; episodes/ SHIPPED in Act 3; the composed first-person brief SHIPPED in Act 4 (Session Opening Brief — see below and `docs/manual/features/session-opening-brief.md`)

**Heartbeat Observations (Living Mind Act 2):** every heartbeat run derives deterministic ambient observations (counts, dates, operator-owned labels — never external free text) from the gather step's sense facts and writes them into the reserved section via `living_memory.append_heartbeat_observation` (capped 10, deduped 3d, aged 7d in-write + by dream cycle). Default groups: `calendar,email,finance,tasks,community,blockers` (all on — locked 2026-06-12); `HEARTBEAT_OBSERVATION_GROUPS=""` disables. Knobs + group/predicate table: `docs/manual/features/heartbeat-runtime.md`.

**Env vars:**
- `REGION_BUDGET_WORKING_MEMORY` (default 600) — token budget for the engine region
- `WORKING_MEMORY_AGE_DAYS` (default 7) — age threshold for dream cycle archival (threads/hypotheses/questions)
- `HEARTBEAT_OBSERVATION_*` — ambient-observation knobs (groups, per-run cap, thresholds, section cap/dedup/age) — full table in the heartbeat-runtime manual page

**Tests:** 19 tests (13 behavior + 5 Langfuse spans + 1 resolve integration)
**Commit:** `1de11c0`

### Episodes (Living Mind Act 3)

The self's autobiography. Every meaningful flush leaves a structured narrative episode in `vault/memory/episodes/{YYYY-MM-DD}-{surface}-{sid8}-{HHMMSS}.md` — written by the EXISTING `memory_flush.py` (no new pipeline, no new LLM call: the flush prompt now emits its bullets under `## Summary` / `## Key Decisions` / `## Open Threads` / `## Texture`, parsed deterministically with an all-under-Summary fallback for provider variance). Episodes contain the LLM summary, NEVER the transcript — the writer receives only `response_text` + the context FILENAME.

| File | Purpose |
|------|---------|
| `episodes.py` | Primitives — `derive_flush_meta`, `parse_flush_sections`, `write_episode_from_flush`, `list_open_episodes`, `render_episodes_digest`, `mark_episodes_consolidated`. Atomic writes under `file_lock`, lazy Langfuse spans (`episode_write`, `episode_consolidate_flip`) |
| `memory_flush.py` | Calls the writer after the daily-log append (fail-open), then best-effort single-file reindex (`_reindex_episode` → `recall_service.reindex_file`) so episodes are searchable same-day |
| `memory_dream.py` | Gather scans open episodes into the signal score + `SignalResult.episode_paths`; consolidate prompt gains `## Recent Episodes (open)`; post-consolidate flip marks them `consolidated` |
| `config.py` | `get_episode_settings()` — Rule 1 call-time resolver for all `EPISODE_*` knobs |

**Key behavior:**
- Episode key = LIFECYCLE-unique (hook-run timestamp from the context filename), NOT the channel-stable chat session key — two `/clear`s of the same channel on the same day are two files; a same-lifecycle retry appends `## Update (HH:MM)` and re-opens the episode
- Filename date = lifecycle START date (midnight crossover stays in one file); `sid8 = sha1(session_id)[:8]` groups a channel's episodes
- Frontmatter: `tags: [system, memory, living-mind]` (existing taxonomy — lint-clean), `status: open|consolidated`, `consolidated_at:` set by the dream flip
- `FLUSH_OK` never writes an episode; `EPISODE_MIN_CHARS` (80) floor; `EPISODE_MAX_PER_DAY` (20) caps NEW files per lifecycle-date (updates exempt)
- Recall reach is free: `memory_index` rglob includes `episodes/`, search/recall code unchanged; `--path-prefix episodes/` scopes searches
- Insert-only history: no archival, dream prune never touches episodes
- Knobs: `EPISODE_MIN_CHARS`, `EPISODE_MAX_PER_DAY`, `EPISODE_DREAM_MAX_FILES` (10), `EPISODE_DREAM_MAX_CHARS_PER` (600), `EPISODE_DREAM_MAX_TOTAL_CHARS` (4000)

**Operator page:** `docs/manual/features/episodes.md`
**Tests:** `tests/test_episodes.py` (55) + `tests/test_session_flush_hooks.py` (13, win32 hook fix) + episode cases in `tests/test_memory_flush_gate.py` and `tests/test_memory_dream.py`

### Session Opening Brief (Living Mind Act 4)

The 6:30am moment — the composed first-person brief. The operator's first interactive ENGINE turn after a meaningful absence (default 8h, inclusive) opens with a deterministic "while you were out" block: fresh heartbeat observations + fresh `[heartbeat]`-tagged threads + episodes written while away (status-agnostic) + applied memory amendments, with open threads as Mid-flight context. **Zero new LLM calls** — the block rides the EXISTING turn's `RuntimeRequest.prompt` as a suffix (the attachment transport: stdin on the native lane, `User task:` on CLI lanes; never the win32-capped system append, never `message.text` — persisted history shows operator text only).

| File | Purpose |
|------|---------|
| `cognition/proactive_brief.py` | `build_session_opening_brief` (gate → reads → boredom → cap-priority render), `normalize_physical_timestamp` (the ONE timestamp owner: SQLite naive / Postgres aware / clear-event ISO / amendment aware-UTC → naive local), brief-owed marker IO (`read/write/clear_brief_owed` over `STATE_DIR/session-brief-owed.json`). Existing `build_proactive_brief*` builders untouched |
| `chat/engine.py` | `resolve_last_operator_activity` (max of newest INTERACTIVE session `updated_at` + newest interactive-trigger clear event — Rule 2, physical state only), `_maybe_session_brief` (per-turn decision, whole-body fail-open, `trace_decisions["session_brief"]` every turn), `note_router_activity` (marker seam), prompt suffix at the `RuntimeRequest` call site |
| `chat/router.py` + `chat/core_handlers.py` | `note_router_activity` called at the `_persist_router_turn` pre-bump seam and before the `/clear` lifecycle — a `/status`-first morning cannot eat the brief (marker carries the pre-bump boundary; consumed exactly once by the first completed engine decision, fired OR silent) |
| `chat/session_lifecycle_hooks.py` | Clear-event rows gain additive `trigger_source` (interactive/cron/tool/hook); resolver counts only interactive; missing field = legacy interactive (2026-06-12) |
| `episodes.py` | `list_episodes_since` — strict-after lifecycle instant, date fallback, status-AGNOSTIC (overnight-consolidated episodes still surface) |
| `config.py` | `get_session_brief_settings()` — Rule 1 call-time resolver for all `SESSION_BRIEF_*` knobs |

**Key behavior:**
- Boredom instinct (the `DREAM_SILENT` pattern): `fresh_items < SESSION_BRIEF_MIN_FRESH_ITEMS` → NO injection, not even a minimal line. Open threads NEVER count (fresh manual threads included) — only change sources do, plus the single explicit `[heartbeat]`-tag exception. Silence gets its own receipt (`[SessionBrief] silent` + trace decision)
- Gate: raw `source == "interactive"` EXACT equality (fail-open `normalize_source` deliberately not trusted), non-PIV, away ≥ threshold (inclusive), fresh items ≥ min. Prefetched-context turns DO fire
- Render priority: instruction reserved → one item per fired source reserved → deep-fill What changed → Self updates → Mid-flight (context drops first); newline-boundary `[TRUNCATED]`
- Double-fire bound: in-memory `_session_brief_fired_at` fold; process restart inside a gap = at worst one extra brief (accepted)
- Fail-open at every seam: builder/resolver/marker failures → bare turn, decision `error`, marker preserved for retry
- Knobs: `SESSION_BRIEF_ENABLED` (true), `SESSION_BRIEF_AWAY_HOURS` (8), `SESSION_BRIEF_MIN_FRESH_ITEMS` (1), `SESSION_BRIEF_MAX_PER_SECTION` (5), `SESSION_BRIEF_MAX_CHARS` (2400)
- Act 2 deferred pickup closed: `living_memory_read` span metadata now carries `observations_count` in both branches

**Operator page:** `docs/manual/features/session-opening-brief.md`
**Tests:** `tests/test_session_brief.py` (51) + Act 4 cases in `tests/test_chat_runtime_engine.py` (9), `tests/test_router_transcript_persistence.py` (5), `tests/test_chat_lifecycle_hooks.py` (3), `tests/test_episodes.py` (9), `tests/test_living_memory.py` (2)

### The Living Self (Make The Self Real — Acts 1-5)

Where Living Mind gave the assistant a substrate (sense → remember → brief), the Make The Self Real program turned the mimic into an individuated self that forms, holds, and earns its own beliefs. It is woven into the EXISTING scheduled cadence — the morning reflection now runs belief extraction + the contradiction pass alongside promotion/decay; no new pipeline. Full operator/architecture runbook: `docs/the-living-self-manual.md`.

| Act | Module(s) | What it made real | Cadence |
|-----|-----------|-------------------|---------|
| 1 — self-model source | `cognition/operator_beliefs.py`, `self_model.py`, `capture.py` | Models the OPERATOR from verbatim `role==user` chat.db turns (not the bot's own replies); embedding cosine dedup (`INFERENCE_DEDUP_THRESHOLD=0.72`); `explicit`/`reflection` provenance sources wired; reversible `migrate-corpus` CLI quarantines keyword-capture poison; renderer source-filtered | Reflection (8 AM) extracts; per-turn capture stages |
| 2 — contradiction engine | `cognition/belief_conflicts.py` | A belief can be HELD AGAINST CONFLICT — nightly embedding pre-filter + LLM judge; explicit beliefs SACROSANCT (an LLM can never lower an operator-stated belief; loser is always a `reflection` by construction); once-only via the `contradicted_by` audit key (drops once, never flaps); held-under-tension render | Reflection (8 AM), after extraction |
| 3 — gated cognitive pass | `cognition/cognitive_pass.py`, `processes.py`, `proactive_actions.py` | The Homie THINKS BEFORE IT SPEAKS — gated inner monologue on substantive turns (the `*_process` functions refactored to return `(wm, thought, actions)` not BE the reply); haiku tier, `asyncio.wait_for` timeout, history-pure (never enters the transcript), win32-capped via the canonical `regions.truncate_for_win32_argv`; proactive action proposes through the default-deny integration gate | Per-turn, gated (`COGNITIVE_PASS_FIRE_PROCESSES=planning`, `MIN_CHARS=40`) |
| 4 — evolve → identity | `scripts/evolve/evolve_loop.py`, `judge.py`, `belief_regression.py`, `cognition/evidence_gate.py`, the `amendments.py` seam | Belief is EARNED not asserted — three layers: evidence-READ gate (`evidence_gate` opens+CONFINES to the vault+BOUNDS each cited path), the deterministic `belief_regression` floor (reuses the never-softenable `evolve/veto` floor; seeded with the system's own failure modes), and a circularity-guarded scheduled LLM judge (fails CLOSED). The `amendments.py` default-deny gate is ADDITIVE-unchanged (`evidence_check=None` = byte parity). Archon drives candidate search; `evolve/` is the fitness oracle | `evolve propose` scheduled (recall-safe); `propose-belief` Archon-driven (identity) |
| 5 — persona learning | `persona_learning_tick.py`, `memory_reflect.py` (persona corpus path), `session.py` (`persona_id` column), `personas/services.py` (`set_persona_learning`) | The Living Self pointed at every named persona profile — persona-attributed experience trail, scheduled learning fan-out (subprocess spawn via `-p`), reflection-only provenance (forced `source="reflection"`), injection gate (`is_injection_attempt` rejection-only). Also fixes the live bug where persona Discord turns contaminated the main corpus | Persona learning tick (scheduled, background tiers) |

**Key invariants:** the belief judge has ZERO chat-hot-path calls (scheduled/Archon only); the amendment ledger + rollback + audit are untouched (Act 4 inserts before them); the evidence gate confines reads to the vault (no `.env`/secret leakage to the judge prompt); every faculty fails open. The crux acceptance test (form → hold → persist-only-if-earned → act) passes: an empty-evidence high-confidence "I read the doc" belief is REJECTED on a real falsifiable check, SELF.md byte-unchanged.

**Config resolvers (all Rule-1 call-time):** `get_inference_extraction_settings`, `get_contradiction_settings`, `get_cognitive_pass_settings`, `get_belief_evolve_settings`, `get_persona_learning_settings` — knob tables in `docs/the-living-self-manual.md` §9.
**Tests:** `tests/test_living_self_act{1,2,3,4}.py` + the contradiction/evolve/amendment suites + `tests/test_persona_learning_*.py` + `tests/test_corpus_persona_exclusion.py` + `tests/test_persona_reflection_provenance.py`.
**Operator page:** `docs/manual/features/persona-learning-loop.md`
