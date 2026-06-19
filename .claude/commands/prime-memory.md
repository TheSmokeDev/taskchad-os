---
description: Load the memory pipelines slice — search, indexing, reflection, weekly synthesis, dream consolidation, working memory, episodes, entity compilation
---

# Prime: Memory Pipelines Slice

## Objective

Build understanding of the 5 automated memory pipelines that keep The Homie's memory current: heartbeat, daily reflection, weekly synthesis, dream consolidation, and session flush. Plus the search/indexing layer, working memory, episodes, and entity compilation engine.

## Key Files to Read

Read these files in order. All pipeline files live in `.claude/scripts/`.

### Architecture doc (read first)
@.claude/sections/03_memory_pipelines.md

### Config and shared state
@.claude/scripts/config.py
@.claude/scripts/shared.py

### Search and indexing
@.claude/scripts/memory_search.py
@.claude/scripts/memory_index.py
@.claude/scripts/db.py
@.claude/scripts/embeddings.py

### Daily reflection (8 AM)
@.claude/scripts/memory_reflect.py

### Weekly synthesis (Sunday 8 PM)
@.claude/scripts/memory_weekly.py

### Dream consolidation (post-weekly + standalone)
@.claude/scripts/memory_dream.py

### Working memory (Living Mind Phase 1)
@.claude/scripts/living_memory.py

### Entity compilation engine (Karpathy LLM Wiki port)
@.claude/scripts/entity_extractor.py

## Slice Boundaries

- **Owns**: heartbeat gather+reason, daily reflection, weekly synthesis, dream consolidation (4 phases), memory search (keyword/semantic/hybrid), markdown indexing, working memory CRUD, episode write/consolidate, entity extraction+compilation
- **Does NOT own**: recall pipeline (`.claude/chat/cognition/recall.py` — see `/prime-signal`), chat routing (`.claude/chat/`), runtime provider selection (`.claude/scripts/runtime/`), orchestration (`.claude/scripts/orchestration/`)
- **Cross-slice touchpoints**: all pipelines use `run_with_fallback()` from runtime for LLM calls; reflection/synthesis trigger entity compilation post-step; dream reads episodes; session flush writes episodes

## Key Invariants

1. Background model tiers: heartbeat = `fast` (haiku), reflection/synthesis/dream = `quality` (sonnet) — NEVER inherit the operator's interactive flagship model
2. Working memory is insert-only archive (Gary Tan invariant) — never hard-delete
3. Episodes contain the LLM summary, NEVER the transcript
4. Entity compilation hooks are non-blocking (try/except) — compilation failure never breaks reflection/synthesis
5. Dream phases 1-2 are zero-cost (pure Python grep) — LLM only invoked when signal found (threshold=4)

## Output

After reading, provide:

### Memory Overview
- Pipeline schedule and trigger map
- Background model tier assignments (fast vs quality)
- State file locations

### Key Patterns
- Dream 4-phase pipeline (orient -> gather -> consolidate -> prune)
- Working memory lifecycle (add -> age -> archive)
- Entity compilation cascade (8 entry points)
- Search modes (keyword, semantic, hybrid)

### Current State
- State file paths (heartbeat, reflection, weekly, dream)
- Test coverage per pipeline
- Env var knobs for each pipeline
