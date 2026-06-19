---
name: homie-self-map
description: Framework architecture map for The Homie. Use when an agent needs to understand the framework's vertical slice structure, locate key files, or extend the framework (add a skill, slash command, integration, or lane). Load this skill BEFORE attempting to modify or extend the framework.
---

# The Homie — Framework Self-Map

A curated architecture map. Read this to understand where things live and how to extend them.

## Vertical Slices

### 1. Chat — Operator Interfaces & Routing

- **Directory**: `.claude/chat/`
- **Key files**: `router.py` (slash commands + NL routing), `engine.py` (runtime-backed conversations), `core_handlers.py` (command handlers), `extension_manager.py` (intent registry), `models.py` (IncomingMessage)
- **Entry points**: `run_chat.sh` (background), `main.py` (direct)
- **What it does**: Routes Telegram/CLI/web messages through slash commands or the reasoning engine

### 2. Runtime — Reasoning Execution Boundary

- **Directory**: `.claude/scripts/runtime/`
- **Key files**: `lane_router.py` (lane-first dispatch), `bootstrap.py` (session start context), `profiles.py` (provider registry), `selection.py` (runtime selection), `base.py` (RuntimeRequest/RuntimeResult)
- **Entry points**: `lane_router.py:run_with_runtime_lanes()`, `bootstrap.py:build_session_start_context()`
- **What it does**: Routes reasoning requests through claude_native or generic_runtime lanes with provider fallback

### 3. Cognition — Memory & Self-Model

- **Directory**: `.claude/chat/cognition/`
- **Key files**: `recall.py` (tier classification + dual search), `operator_beliefs.py` (operator model from chat turns), `belief_conflicts.py` (contradiction engine), `cognitive_pass.py` (gated inner monologue), `proactive_brief.py` (session opening brief)
- **Entry points**: `recall.py:run_recall_pipeline()`, `cognitive_pass.py:run_cognitive_pass()`
- **What it does**: Recall, self-model, belief formation, proactive briefing — the "thinking" layer

### 4. Orchestration — Multi-Agent Task Coordination

- **Directory**: `.claude/scripts/orchestration/`
- **Key files**: `convoy_service.py` (convoy business logic), `mailbox_service.py` (inter-agent messages), `executor.py` (dispatch adapters), `api.py` (FastAPI on port 4322), `contract.py` (frozen enums + transitions)
- **Entry points**: `run_api.py` (uvicorn), CLI via `thehomie convoy`/`thehomie mailbox`
- **What it does**: Convoy/mailbox DAG coordination with dependency tracking and executor dispatch

### 5. Integrations — External API Connections

- **Directory**: `.claude/scripts/integrations/`
- **Key files**: `registry.py` (service availability), `capabilities.py` (default-deny action gates), `gmail_api.py`, `calendar_api.py`, `asana_api.py`, `slack_api.py`
- **Entry points**: `capabilities.py:require_integration_action()`, `registry.py:get_integration_status()`
- **What it does**: Direct API access to Gmail, Calendar, Asana, Slack, etc. — all behind capability gates

### 6. Dashboard — Web Control Plane

- **Directory**: `.claude/scripts/dashboard_*.py` + `dashboard/server/` + `dashboard/web/`
- **Key files**: `dashboard_api.py` (Python HTTP API), `dashboard/server/index.ts` (Hono thin proxy on port 3141), `dashboard/web/` (Vite+Preact SPA)
- **Entry points**: Port 4322 (Python API), port 3141 (Hono proxy)
- **What it does**: Web GUI for bot management, cabinet rooms, browser viewer — reads from Python-owned APIs

### 7. Memory — Canonical Substrate

- **Directory**: `vault/memory/`
- **Key files**: `SOUL.md` (identity), `SELF.md` (self-model), `USER.md` (operator profile), `MEMORY.md` (decisions + lessons), `WORKING.md` (cross-session scratchpad)
- **Entry points**: `bootstrap.py:build_session_start_context()` injects at session start
- **What it does**: Obsidian vault as source of truth — all derived state (memory.db, chat.db) is cache

## Lane System

Two runtime lanes, resolved at `lane_router.py:48-58`:

| Lane | Provider | Auth | When |
|------|----------|------|------|
| `claude_native` | Claude Agent SDK | Max subscription (`~/.claude/.credentials.json`) | Default for resume, explicit `SECOND_BRAIN_RUNTIME_LANE=claude_native` |
| `generic_runtime` | Codex, Gemini, OpenAI-compatible | API keys / env vars | Default lane; fallback chain in `profiles.py` |

Adapter dispatch at `lane_router.py:61-76` maps `profile.provider` → adapter class. Business behavior is lane-agnostic — the assembled prompt survives any provider fallback.

## Hook Lifecycle

| Hook | File | When |
|------|------|------|
| SessionStart | `.claude/hooks/session-start-context.py` | Injects SOUL/SELF/USER/MEMORY/GOALS + repo briefing |
| PreCompact | `.claude/hooks/pre-compact-flush.py` | Saves context before auto-compaction |
| SessionEnd | `.claude/hooks/session-end-flush.py` | Flushes context to daily log + writes episode |
| UserPromptSubmit | `.claude/hooks/check_live_chat.py` | Checks live-chat for unread messages |

## Memory Pipelines

| Pipeline | File | Schedule | What It Does |
|----------|------|----------|-------------|
| Heartbeat | `heartbeat.py` | Every 30 min | Gathers API data, reasons, notifies |
| Reflection | `memory_reflect.py` | Daily 8 AM | Reviews daily log, promotes to MEMORY.md |
| Weekly | `memory_weekly.py` | Sunday 8 PM | Creates weekly summary, updates GOALS.md |
| Dream | `memory_dream.py` | Post-weekly + manual | Deep consolidation, prune, normalize |
| Recall | `recall_service.py` + `cognition/recall.py` | Per-message | FTS5 keyword + semantic search |

## Default-Deny Mutation Policy

Any surface that mutates the outside world ships default-denied with an audit trail. Three gate implementations:

1. **Integration actions** — `capabilities.py:require_integration_action()` gates all mutating integration calls
2. **Capability gateway** — `orchestration/capability_gateway.py` gates operating-room surface actions
3. **Kill switches** — `security/kill_switches.py:requireEnabled()` provides operator-toggleable refusal counters

Pattern: default-deny → explicit capability gate → audit row.

## Extension Guides

Step-by-step checklists for extending the framework:

- **Add a skill** → [references/add-skill.md](references/add-skill.md)
- **Add a slash command** → [references/add-command.md](references/add-command.md)
- **Add an integration** → [references/add-integration.md](references/add-integration.md)

Complementary: the `skill-creator` skill teaches HOW to write a good skill; these guides teach WHERE it fits in the framework.
