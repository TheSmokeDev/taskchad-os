# Changelog

All notable changes to The Homie are documented here.

Format inspired by [Hermes Agent](https://github.com/NousResearch/hermes-agent). Each release gets:
- A SemVer name (`v1.1.0`) + CalVer tag (`v2026.4.5`)
- A one-line tagline describing the release character
- Highlights for the user-visible wins
- Grouped sections by area

---

## The Homie v1.1.0 (v2026.4.5)

**Release Date:** April 5, 2026

> The team runtime release — multi-agent team sessions, typed mailbox, backend fallback selection, framework-native shared memory, and convoy/mailbox phase 6 hardening. 1101 tests. All orchestration phases complete.

---

### Highlights

- **Multi-Agent Team Sessions** — Agents now coordinate in DB-backed team sessions with typed roles (leader/worker), full lifecycle (`active → idle → shutdown_requested → closed`), and per-session convoy binding. Teams are persistent across restarts and queryable via API and CLI.

- **Typed Mailbox** — The agent mailbox now carries a `msg_type` column with typed control semantics: `task_assignment`, `work_handoff`, `blocked_request`, `idle_ready`, `shutdown_request`, `shutdown_ack`, `verifier_feedback`, `progress_update`, `direct`. Inbox filtering by type is supported.

- **Backend Fallback Chain** — Team dispatch now uses a `BackendSelector` with an `auto→paperclip→workflow→local` fallback chain. If the preferred backend is unavailable, dispatch degrades gracefully instead of failing hard. The actual backend used is returned in the receipt.

- **Framework-Native Team Memory** — Shared team memory at `vault/teams/team-{id}/` with per-team-id isolation (not name — prevents session collision), secret guardrails (8 credential patterns blocked before write), path traversal rejection, and full CRUD API at `/api/team/{id}/memory`.

- **Convoy Phase 6 Hardening** — Auth token middleware, executor callback ingress with idempotency, CAS on dispatch (no double-dispatch races), terminal status guards, and thin-adapter parity proofs (no `sqlite3` in `api.py`). 70 tests.

- **Security: NULL Convoy Guard** — Teams without `convoy_id` set are denied dispatch authority entirely. Previously, a NULL convoy_id would bypass the ownership check and grant wildcard subtask access.

- **Security: IntegrityError Surface** — `add_member()` and `create_team_session()` now catch all `sqlite3.IntegrityError` (CHECK constraint, FK violation, UNIQUE) at the service layer and surface as `ValueError` → 4xx. Non-UNIQUE constraint errors previously propagated as 500.

---

### Orchestration

#### Team Runtime (Phases 0-7)
- Phase 0: doctrine freeze — `CONTRACT.md` + `team-orchestration.md` merged model
- Phase 1: coordinator runtime contract — `team-coordinator-contract.md` + `engine.py` injection
- Phase 2: canonical team state — `team_sessions`/`team_members` tables + `TeamService` + `/api/team`
- Phase 3: typed mailbox — `msg_type` column + typed send helpers + inbox filter
- Phase 4: CLI surface — `thehomie team list/status/members/shutdown/ping/close`
- Phase 5: MC team view — `TeamOperationsPanel` + BFF routes
- Phase 6: backend selection + fallback — `BackendSelector` with `auto→paperclip→workflow→local`
- Phase 7: framework-native team memory — `team_memory.py` + secret guardrails + `/api/team/{id}/memory`

#### Convoy/Mailbox (Phase 6)
- Auth token middleware — `ORCHESTRATION_API_TOKEN` bearer validation, non-loopback enforcement
- Executor callback ingress — `POST /api/executor/callback` + `callback_receipts` idempotency table
- CAS dispatch — compare-and-swap guards prevent double-dispatch races
- Terminal status guards — no transitions out of `completed`/`failed`/`cancelled`
- Thin-adapter parity — `sqlite3` removed from `api.py`; all DB errors surface via service layer

### CLI

- `thehomie team list` — list active team sessions
- `thehomie team status <id>` — team detail, members, mailbox backlog (scoped to convoy)
- `thehomie team members <id>` — member list with roles
- `thehomie team shutdown <id>` — request graceful shutdown
- `thehomie team ping <id>` — bump activity timestamp
- `thehomie team close <id>` — force-close team session

### API

New endpoints:
- `POST /api/team` — create team session
- `GET /api/team/{id}` — get team + members
- `GET /api/team` — list teams (optional status filter)
- `POST /api/team/{id}/shutdown` — request shutdown
- `POST /api/team/{id}/close` — force close
- `POST /api/team/{id}/ping` — ping activity
- `POST /api/team/{id}/member` — add member
- `PATCH /api/team/{id}/member/{agent_id}` — update member status
- `POST /api/team/{id}/dispatch/{subtask_id}` — dispatch subtask via team backend
- `GET /api/team/{id}/memory` — list memory files
- `GET /api/team/{id}/memory/{filename}` — read memory file
- `POST /api/team/{id}/memory/{filename}` — write memory file (with secret scan)
- `DELETE /api/team/{id}/memory/{filename}` — delete memory file

### Security

- `dispatch_to_executor()` — teams with `convoy_id = NULL` denied dispatch authority
- `add_member()` — all `IntegrityError` subtypes (CHECK, FK, UNIQUE) surface as `ValueError` / 4xx
- `create_team_session()` — FK violations (bad `convoy_id`) surface as `ValueError`
- `team_memory.py` — 8 credential patterns blocked: generic API keys, OpenAI (`sk-`), Langfuse (`pk-lf-`, `sk-lf-`), Paperclip (`pcp_`), GitHub PAT (`ghp_`), JWT, bearer tokens

### Tests

1101 passing (was 780 in v1.0.0). 321 tests added across team orchestration phases:
- `test_team_cli.py` — 50 tests (CLI surface)
- `test_typed_mailbox.py` — 75 tests (mailbox types + filtering)
- `test_backend_fallback.py` — 80 tests (BackendSelector + dispatch)
- `test_team_memory.py` — 40 tests (shared memory + secret guardrails + API)
- `tests/test_orchestration_api.py` — extended with team session API coverage

---

## The Homie v1.0.0 (v2026.4.2)

**Release Date:** April 2, 2026

> The hardening release — framework invariants locked, convoy/mailbox phases 0-5 complete, Langfuse full-depth observability, 9-layer cognitive architecture fully shipped, and Hermes competitive analysis complete.

### Highlights

- **9-Layer Cognitive Architecture complete** — All layers shipped: identity (SOUL), self-awareness (SELF), understanding (USER + ToM), memory (vault + hybrid search), recall (3-tier gate + dual search + graph), learning (auto-capture → staging → promotion + skills), thinking (mental process state machine), continuity (session persistence + compaction), and self-evolution (planned)
- **Convoy/Mailbox Orchestration Phases 0-5** — contract freeze, service layer, CLI, local API (port 4322), executor adapters (Local/Paperclip/WorkflowRunner), subtask transitions + field updates
- **Langfuse Full-Depth Observability** — single nested trace per message: 9 spans (session_lookup, process_detection, recall, classify_tier, recall_pipeline, region_assembly, run_with_fallback, invoke_agent, post_response)
- **Framework Hardening Phases 1-6** — canonical ingress, durable session identity, one recall service, runtime contract, packaging, Hermes port analysis
- **Quiet CLI JSON machine contract** — `thehomie chat -q "..." -Q` preserves `provider`, `model`, `cost_usd`, `tool_calls`
- **780 tests passing**
