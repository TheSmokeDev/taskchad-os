---
description: Load the memory pipelines slice — reflection, weekly synthesis, dream consolidation, flush, search, indexing, working memory, episodes
---

# Prime: Memory Pipelines Slice

## Objective

Build understanding of the 5 automated memory pipelines that keep The Homie's memory current: heartbeat, daily reflection, weekly synthesis, dream consolidation, and session flush. Plus the search/indexing layer, working memory (Living Mind), and episodes (autobiography).

## Key Files to Read

Read these files in order. Pipelines live in `.claude/scripts/`; search and support modules are alongside them.

### Architecture doc (read first)
@.claude/sections/03_memory_pipelines.md

### Config and shared state
@.claude/scripts/config.py
@.claude/scripts/shared.py

### Heartbeat (every 30 min)
@.claude/scripts/heartbeat.py

### Daily reflection (8 AM)
@.claude/scripts/memory_reflect.py

### Weekly synthesis (Sunday 8 PM)
@.claude/scripts/memory_weekly.py

### Dream consolidation (post-weekly + standalone)
@.claude/scripts/memory_dream.py

### Session flush (on session end / pre-compact)
@.claude/scripts/memory_flush.py

### Search and indexing
@.claude/scripts/memory_search.py
@.claude/scripts/memory_index.py
@.claude/scripts/db.py

### Identity payload reader (shared shim)
@.claude/chat/cognition/identity_payload.py

### Working memory (Living Mind Phase 1)
@.claude/scripts/living_memory.py

### Episodes (Living Mind Act 3)
@.claude/scripts/episodes.py

### Entity compilation engine
@.claude/scripts/entity_extractor.py

## Slice Boundaries

- **Owns**: heartbeat gather+reason, daily reflection, weekly synthesis, dream consolidation (4 phases), session flush, memory search (keyword/semantic/hybrid), markdown indexing, working memory CRUD, episode write/consolidate, entity extraction+compilation
- **Does NOT own**: recall pipeline (`.claude/chat/cognition/recall.py` — see `/prime-signal`), chat routing, runtime provider selection, vault structure (canonical memory lives in `vault/memory/`)
- **Cross-slice touchpoints**: all pipelines use `run_with_fallback()` from runtime for LLM calls; reflection/synthesis call entity compilation post-step; dream reads episodes; flush writes episodes; heartbeat writes working-memory observations

## Key Invariants

1. Background model tiers: heartbeat=`fast` (haiku), reflection/synthesis/dream=`quality` (sonnet) — NEVER inherit the operator's interactive flagship model
2. Identity payload reader is the single shim for all identity-file reads (SOUL/SELF/USER/MEMORY/GOALS/WORKING) — no duplicate `read_file_safe()` calls
3. Working memory is insert-only archive (Gary Tan invariant) — never hard-delete
4. Episodes contain the LLM summary, NEVER the transcript
5. Entity compilation hooks are non-blocking (try/except) — compilation failure never breaks reflection/synthesis
6. Dream phases 1-2 are zero-cost (pure Python grep) — LLM only invoked when signal found (threshold=4)

## Output

After reading, provide:

### Memory Overview
- Pipeline schedule and trigger map
- Background model tier assignments
- State file locations

### Key Patterns
- Dream 4-phase pipeline (orient → gather → consolidate → prune)
- Working memory lifecycle (add → age → archive)
- Episode lifecycle (write → search → dream-consolidate → flip status)
- Entity compilation cascade (8 entry points)

### Current State
- State file paths (heartbeat, reflection, weekly, dream)
- Test coverage per pipeline
- Env var knobs for each pipeline
