---
description: Load the cognition + Living Self slice — recall, belief formation, contradiction engine, cognitive pass, evidence gate, self-evolution
---

# Prime: Signal / Cognition Slice

## Objective

Build understanding of The Homie's cognitive system — how it recalls, thinks before speaking, forms beliefs about the operator, detects contradictions, and evolves its own identity files through evidence-gated amendments. This is the "Make The Self Real" program: Living Mind (sense/remember/brief) + Living Self (form/hold/earn beliefs).

## Key Files to Read

Read these files in order. The cognition modules live in `.claude/chat/cognition/`; the evolution engine lives in `.claude/scripts/evolve/`.

### Architecture doc (read first — Living Self section)
@.claude/sections/03_memory_pipelines.md

### Core recall pipeline
@.claude/chat/recall_service.py
@.claude/chat/cognition/recall.py

### Region assembly (how context reaches the prompt)
@.claude/chat/cognition/regions.py
@.claude/chat/cognition/identity_payload.py

### Cognitive pass (think before speaking)
@.claude/chat/cognition/cognitive_pass.py
@.claude/chat/cognition/processes.py
@.claude/chat/cognition/proactive_actions.py

### Operator modeling (Act 1 — model the operator from verbatim turns)
@.claude/chat/cognition/operator_beliefs.py
@.claude/chat/cognition/self_model.py
@.claude/chat/cognition/capture.py

### Contradiction engine (Act 2)
@.claude/chat/cognition/belief_conflicts.py

### Evidence gate + amendments (Act 4)
@.claude/chat/cognition/evidence_gate.py
@.claude/chat/cognition/amendments.py

### Self-evolution loop (Archon-driven)
@.claude/scripts/evolve/evolve_loop.py
@.claude/scripts/evolve/judge.py
@.claude/scripts/evolve/belief_regression.py

### Session opening brief (Living Mind Act 4)
@.claude/chat/cognition/proactive_brief.py

## Architecture Manual
@docs/the-living-self-manual.md

## Slice Boundaries

- **Owns**: recall pipeline (tier classification, dual search, graph traversal), region assembly, cognitive pass, operator belief extraction, contradiction detection, evidence-gated amendments, belief evolution loop, session opening brief
- **Does NOT own**: memory persistence pipelines (`.claude/scripts/memory_*.py` — see `/prime-memory`), chat routing (`.claude/chat/router.py`), runtime provider selection
- **Cross-slice touchpoints**: `engine.py` calls cognition modules for region assembly, recall, and cognitive pass; memory pipelines call recall for search; evolve loop reads/writes SOUL.md and SELF.md

## Key Invariants

1. Belief judge has ZERO chat-hot-path calls — scheduled/Archon only
2. Amendment ledger + rollback + audit are untouched by new evolution code
3. Evidence gate confines reads to the vault (no `.env`/secret leakage to the judge prompt)
4. Every faculty fails open — cognition-unavailable = raw text append, no crash
5. Explicit beliefs are SACROSANCT — an LLM can never lower an operator-stated belief
6. Cognitive pass is history-pure — thoughts never enter the transcript

## Output

After reading, provide:

### Cognition Overview
- 4-Act structure status (Living Self)
- Recall tiers and search modes
- Region budget allocations

### Key Patterns
- Capture → belief extraction → contradiction → evidence gate → amendment flow
- Cognitive pass gating criteria (`COGNITIVE_PASS_FIRE_PROCESSES`, `MIN_CHARS`)
- Session brief away-threshold and boredom instinct

### Current State
- Test files and counts per act
- Config resolvers and their env var knobs
