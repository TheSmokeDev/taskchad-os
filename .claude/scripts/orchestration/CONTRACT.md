# Orchestration Contract — Frozen 2026-04-03

## Ownership

The `thehomie` framework owns all orchestration primitives:
convoys, subtasks, dependency edges, attempts, mailbox messages, and deliveries.

**Mission Control** is a downstream GUI/operator surface — it reads and
triggers actions through framework APIs but does not own orchestration state.

**Paperclip** is an optional executor adapter — it can execute dispatched
subtasks and report results, but is never the source of truth.

## Frozen Models

| Entity | Table | Key Fields |
|--------|-------|------------|
| Convoy | `convoys` | id, title, status, created_by, base_branch, merge_strategy |
| Subtask | `subtasks` | id, convoy_id, title, status, remaining_dependencies, seq |
| DependencyEdge | `dependency_edges` | from_subtask_id, to_subtask_id (UNIQUE pair) |
| Attempt | `attempts` | attempt_key (UNIQUE), action, status |
| AgentMessage | `agent_messages` | from_agent, message_type, body, dedupe_key |
| AgentDelivery | `agent_deliveries` | message_id, recipient_agent, status, claim_token |

See `models.py` for complete field definitions.

## Frozen DTO Rules

- `CreateSubtaskInput.depends_on_subtask_indexes` is only for subtask references
  inside the same `create_convoy()` request.
- `AddSubtaskInput.depends_on_subtask_ids` is only for references to already
  persisted subtasks in an existing convoy.
- Delivery acknowledgement requires claim ownership:
  - delivery must already be `claimed`
  - recipient identity must match
  - claim token must match

## Status Enums

- **ConvoyStatus**: draft | active | paused | completed | failed | cancelled
- **SubtaskStatus**: pending | ready | dispatched | running | completed | failed | cancelled | stalled
- **MessageType**: command | approval_request | clarification | exception | handoff | interrupt | cancel | result | status | message
- **DeliveryStatus**: pending | seen | claimed | acked | nacked | expired | dead_lettered

## Valid Convoy Transitions

```
draft   → active, cancelled
active  → paused, cancelled
paused  → active, cancelled
```

Terminal states (completed, failed, cancelled) have no outbound transitions.

## Executor Interface

```python
class ExecutorAdapter(ABC):
    def dispatch(subtask) -> str | None    # returns external ref ID
    def cancel(subtask) -> bool            # returns acceptance
    def check_status(subtask) -> str | None
```

Default: `LocalExecutor` (noop — operator drives via CLI).
Optional: Paperclip adapter (Phase 4).

## No-Dual-Write Rule

During migration from MC ownership to framework ownership, there must be
**zero period** where both systems write orchestration state. The cutover
is atomic per entity type: either MC owns writes or the framework does.

Enforcement:
1. Framework service layer is built and tested (Phase 1)
2. CLI is wired to framework services (Phase 2)
3. Local API exposes framework services (Phase 3)
4. MC routes are repointed to framework API (Phase 5) — atomic cutover
5. MC local tables are retired after cutover verification

## Local API Trust Boundary

- Binds to `127.0.0.1` only (loopback) by default
- No authentication required for local-only callers in early phases
- Remote access requires explicit opt-in configuration
- CLI calls Python services directly — no HTTP loopback

## 8. Team Coordinator Runtime Doctrine

**Architecture assignments (frozen):**

| Concern | Owner | Notes |
|---------|-------|-------|
| Work ownership and dependency graph | **Convoy** | Task creation, dispatch, completion, DAG edges |
| Agent-to-agent communication | **Mailbox** | All inter-agent messages, control messages |
| Leader/worker behavioral rules | **Coordinator contract** | Runtime-injected prompt doctrine |
| Operator visibility and control | **Mission Control** | Read-only state consumer, not state owner |
| Shared team memory | **Framework memory APIs** | Behind canonical memory, not vendor-specific sync |

**Explicitly rejected (from the competitor):**
- File-backed team task directories (`~/.thehomie/teams/...`)
- File-backed teammate inboxes
- Anthropic-specific team-memory sync endpoints
- Making Mission Control the source of truth for team lifecycle

**Team coordinator rules:**
1. The coordinator leads — workers do not see the parent conversation
2. Workers produce bounded outputs (research, implementation, verification) — they do not coordinate other workers
3. Findings are synthesized at the leader — never "based on your findings, do X"
4. Parallelism is the primary advantage of team mode — use it
5. Continue a worker when their context still helps; spawn fresh when a clean perspective is better
6. Mailbox is for coordination/control messages — it is NOT the work graph
7. Convoy subtasks define owned work — a worker claiming a subtask means they own it
