---
description: Load the orchestration slice — convoy/mailbox, team coordination, executor adapters, local API (port 4322)
---

# Prime: Orchestration Slice

## Objective

Build understanding of the convoy/mailbox orchestration system, team coordination, and the local API surface. This slice owns multi-agent task coordination — subtask DAGs with dependency tracking, inter-agent messaging, and pluggable executor adapters.

## Key Files to Read

Read these files in order. Together they are the complete orchestration vertical slice.

### Architecture doc (read first)
@.claude/sections/07_orchestration.md

### Contract (frozen — read before any service code)
@.claude/scripts/orchestration/CONTRACT.md
@.claude/scripts/orchestration/contract.py

### Data model
@.claude/scripts/orchestration/models.py

### Persistence layer
@.claude/scripts/orchestration/db.py

### Core services
@.claude/scripts/orchestration/convoy_service.py
@.claude/scripts/orchestration/mailbox_service.py

### Executor boundary
@.claude/scripts/orchestration/executor.py

### API surface (thin adapter — zero business logic)
@.claude/scripts/orchestration/api.py

## Slice Boundaries

- **Owns**: convoy state machine, subtask transitions, dependency release, mailbox delivery, team sessions/members, executor dispatch+callback, the local API on port 4322
- **Does NOT own**: chat routing (`.claude/chat/`), dashboard rendering (`dashboard/`), runtime provider selection (`.claude/scripts/runtime/`), memory pipelines (`.claude/scripts/memory_*.py`)
- **Cross-slice touchpoints**: Cabinet handlers in `core_handlers.py` HTTP-route to this API; dashboard reads convoy/team state via the API; orchestration never imports from chat or dashboard

## Invariants to Preserve

1. Contract is FROZEN — transition maps, terminal sets, field allowlists do not change without a PRP
2. 3-layer idempotency: CAS dispatch + attempts table + callback receipts
3. Executor boundary: adapters NEVER write DB — they return `ExecutorReceipt`, the service layer persists
4. API is a thin adapter: routes to service layer, Pydantic validation, zero business logic in HTTP handlers
5. Non-loopback requires `ORCHESTRATION_API_ALLOW_NON_LOOPBACK=true` + bearer token

## Output

After reading, provide:

### Key Files
| File | Purpose |
|------|---------|
| `contract.py` | Frozen enums, transition maps, terminal sets, field allowlists |
| `convoy_service.py` | All convoy business logic |
| `mailbox_service.py` | Message send, inbox, claim, acknowledge |
| `executor.py` | ExecutorAdapter base + LocalExecutor, PaperclipExecutor, WorkflowRunnerExecutor |
| `api.py` | FastAPI thin adapter on port 4322 |
| `models.py` | Dataclasses: Convoy, Subtask, DependencyEdge, ExecutorReceipt, etc. |
| `db.py` | SQLite persistence |

### Architecture Notes
- Convoy/subtask state machine transitions and terminal sets
- Dependency release mechanics (completion decrements `remaining_dependencies`)
- Executor dispatch pattern (CAS + attempts + callback receipts)

### Current State
- Phase status for convoy/mailbox (0-6) and team orchestration (0-7)
- Test count and coverage
- Any open follow-ups noted in section 07
