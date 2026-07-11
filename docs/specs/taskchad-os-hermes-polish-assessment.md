# TaskChad OS × Hermes Agent: Polish and Self-Improvement Blueprint

**Repository assessed:** `TheSmokeDev/taskchad-os`
**Branch/commit:** `master` at `d174d8c47a5b5aaa14ad462eb7aa8a6037412772`
**Hermes reference:** local installed source plus authoritative Hermes documentation
**Method:** read-only documentation-to-code traceability review; no installs, runtime execution, or repository modifications.

---

## Executive verdict

TaskChad OS is already a serious identity-first cognitive agent framework—not a chat wrapper and not vaporware. Its strongest real implementations are:

- structured Living Self cognition and immutable working-memory assembly;
- isolated persona/profile lifecycle;
- Markdown-vault memory with hybrid retrieval and persona-scoped indexes;
- Cabinet multi-persona rooms;
- Convoy DAGs, typed mailboxes, claim/ack delivery, Team Room, and Operating Room slices;
- security-scanned, recurrence-gated skill drafting and operator promotion;
- amendment policy, audit ledger, snapshots, and reconciliation;
- runtime lanes across Claude SDK, Codex, Gemini CLI, and OpenAI-compatible providers;
- broad CLI, API, dashboard, desktop, and channel surfaces;
- unusually broad unit and contract-test coverage.

The main gap versus Hermes is **not feature count**. It is **cohesion, lifecycle completeness, and operational proof**. TaskChad has many strong subsystems, but configuration, policy, tool authority, persona execution, jobs, diagnostics, audit, and operator proof are fragmented across parallel pathways.

The right strategy is therefore:

> Keep TaskChad’s identity-first cognition, Cabinet, vault, and Convoy concepts. Port Hermes’s lifecycle invariants and operating discipline—not its accumulated global state or giant modules.

---

## 1. What is real in TaskChad OS

### 1.1 Living Self and cognition

**Source anchors**

- `.claude/chat/cognition/identity_payload.py::build_identity_payload`
- `.claude/chat/cognition/working_memory.py::Memory`, `WorkingMemory`, `with_memory`
- `.claude/chat/cognition/regions.py::build_initial_working_memory`, `prompt_regions_from_working_memory`
- `.claude/chat/cognition/steps.py::CognitiveContext`, cognitive step functions
- `.claude/chat/cognition/cognitive_pass.py::should_run_cognitive_pass`, `run_cognitive_monologue`
- `.claude/chat/cognition/amendments.py`
- `.claude/chat/engine.py::_maybe_cognitive_pass`
- `.claude/scripts/evolve/evolve_loop.py::propose_belief`
- `.claude/scripts/evolve/belief_regression.py::evaluate_belief_regression`
- `.claude/scripts/evolve/judge.py::judge_belief_candidate`
- `.claude/scripts/heartbeat.py`, `memory_reflect.py`, `memory_dream.py`, `memory_weekly.py`

**Assessment**

This is a real structured prompt/cognition pipeline. Identity, self-model, user model, durable memory, recalled memory, procedural memory, internal monologue, and recent conversation are modeled as ordered regions rather than one prompt dump. Durable identity changes remain policy- and approval-gated.

**Qualification**

“Living” currently means scheduled and per-turn cognition, not a continuously active cognitive kernel. That is a reasonable safety choice, but the product language should say so clearly.

### 1.2 Personas and profiles

**Source anchors**

- `.claude/scripts/personas/core.py`
- `.claude/scripts/personas/lifecycle.py`
- `.claude/scripts/personas/services.py`
- `.claude/scripts/personas/boot.py`
- `.claude/scripts/personas/clone.py`
- `.claude/scripts/personas/migrate.py`
- `.claude/scripts/personas/atomic.py`
- `.claude/scripts/dashboard_bot_lifecycle.py`
- `.claude/scripts/persona_learning_tick.py`
- `.claude/chat/discord_persona_runtime.py`
- `.claude/chat/web_persona_runtime.py`

**Assessment**

Profile creation, validation, inventory repair, isolation, clone/import/export/migration, process handling, port allocation, and dashboard surfaces are substantive. This is one of TaskChad’s strongest differentiators.

**Gap**

Persona execution is duplicated across main engine, Discord, web, Cabinet, scheduled cognition, and teams. A single `PersonaTurnService` should own identity loading, recall, policy, runtime selection, persistence, and observability.

### 1.3 Vault memory and recall

**Source anchors**

- `.claude/chat/recall_service.py::recall`
- `.claude/chat/cognition/recall.py::run_recall_pipeline`
- `.claude/chat/cognition/graph.py::MemoryGraph`
- `.claude/scripts/memory_index.py`
- `.claude/scripts/memory_search.py`
- `.claude/scripts/db.py::{MemoryDB,SQLiteMemoryDB,PostgresMemoryDB}`
- `.claude/scripts/memory_flush.py`, `memory_reflect.py`, `memory_dream.py`, `memory_weekly.py`
- `.claude/scripts/orchestration/team_memory.py`

**Assessment**

Markdown as canonical source plus rebuildable SQLite/vector indexes is implemented. Recall includes tiering, expansion, keyword/vector retrieval, graph boosting, optional reranking, sanitization, and prompt budgeting.

**Gaps**

- no transaction joining Markdown mutation to index update;
- shallow freshness/corruption diagnostics;
- no universal enforcement that all recall consumers use the canonical service;
- fixed vault slots rather than a declarative vault registry;
- persona authorship and Cabinet learning ingestion are incomplete.

### 1.4 Skill-from-experience

**Source anchors**

- `.claude/chat/cognition/skills.py::{SkillSpec,propose_skill,write_skill,build_skill_index,validate_skill,patch_skill}`
- `.claude/chat/cognition/skill_usage.py`
- `.claude/chat/cognition/skill_promotion.py`
- `.claude/chat/cognition/skill_guard.py`
- `.claude/chat/skill_audit.py`
- `.claude/chat/engine.py` skill proposal/write seam
- `.claude/chat/core_handlers.py::handle_skills`

**Assessment**

The lifecycle is real and appropriately conservative:

`observe recurrence → draft → quarantine by directory → scan → operator approval → promote → audit → stale archive`

Generated skills do not enter active procedural memory until promoted.

**Gaps**

- skills are primarily Markdown prompt artifacts rather than typed procedures;
- recurrence matching is coarse;
- promotion does not empirically replay/evaluate a procedure;
- audit failure can fail open;
- interrupted promotion is not fully transactional.

### 1.5 Convoy, mailbox, teams, and Operating Room

**Source anchors**

- `.claude/scripts/orchestration/db.py::OrchestrationDB`
- `.claude/scripts/orchestration/convoy_service.py::ConvoyService`
- `.claude/scripts/orchestration/mailbox_service.py::MailboxService`
- `.claude/scripts/orchestration/team_service.py::TeamService`
- `.claude/scripts/orchestration/team_loop.py`
- `.claude/scripts/orchestration/team_executor.py`
- `.claude/scripts/orchestration/team_room.py`
- `.claude/scripts/orchestration/operating_room.py`
- `.claude/scripts/orchestration/api.py`

**Assessment**

Convoy DAG validation, dependency release, CAS dispatch, attempts, typed messages, claim tokens, acknowledgement, and stale-claim recovery form a credible local orchestration baseline.

**Qualification**

It is a strong local SQLite workflow engine, not yet a distributed scheduler. Operating Room is currently a bounded product slice around Team Room and proof-packet generation, not a full durable agent-operations cockpit.

### 1.6 Amendments and rollback

**Source anchors**

- `.claude/chat/cognition/amendments.py::{AmendmentProposal,ProposalLedger,AmendmentPolicy,apply_policy_approved_amendments,evaluate_amendment_policy,collapse_autonomous_amendments,_write_rollback_snapshot}`

**Assessment**

The policy, ledger, locking, hashes, snapshots, deduplication, and reconciliation are strong.

**Critical gap**

Snapshots are created, but there is no amendment-aware restore operation that verifies hashes, restores atomically, records actor/reason, and marks the proposal rolled back. This is the clearest “looks complete but lifecycle is unfinished” gap.

---

## 2. What Hermes does differently

### 2.1 Skills as a managed knowledge supply chain

**Hermes anchors**

- `tools/skills_tool.py`
- `tools/skill_manager_tool.py`
- `tools/skills_sync.py`
- `tools/skills_hub.py`
- `tools/skills_guard.py`
- `tools/skills_ast_audit.py`
- `tools/skill_usage.py`
- `agent/curator.py`
- `agent/curator_backup.py`

Hermes adds:

- multiple skill origins and source adapters;
- provenance and content hashes;
- environment/platform readiness;
- quarantine and security scanning;
- official-vendor synchronization with local modification detection;
- usage/view/patch telemetry;
- pin/archive/restore states;
- curator backups;
- evidence-based reconciliation between what a curator claims and what actually changed.

**Port the lifecycle, not the layout.** TaskChad should build a typed `SkillRepository` and generate Markdown projections, rather than making directories the only database.

### 2.2 Narrow-waist capability architecture

**Hermes anchors**

- `tools/registry.py`
- `toolsets.py`
- `model_tools.py`
- `hermes_cli/plugins.py`
- `agent/plugin_llm.py`
- `tools/mcp_tool.py`

Hermes’s reusable invariant is a narrow core with capability growth through tools, toolsets, plugins, MCP, provider contracts, and adapters. Availability checks prevent unavailable tools from entering a session manifest.

TaskChad currently has a registry federation: chat commands, intents, skills, integrations, MCP, overlays, executors, and Cabinet allowlists. These need one typed authority.

### 2.3 Stable sessions and prompt-prefix discipline

**Hermes anchors**

- `hermes_state.py::SessionDB`
- `gateway/session.py`
- `hermes_cli/active_sessions.py`
- `agent/context_compressor.py`
- `agent/conversation_compression.py`
- `trajectory_compressor.py`

Hermes treats prompt stability as an architectural invariant: compile system context/tool schemas at session start; do not mutate them casually mid-conversation. Compression is a controlled exception.

TaskChad should go further than Hermes by using an append-only `SessionEvent` log and derived `CompactionArtifact`, rather than rewriting canonical history.

### 2.4 Structured diagnostics

**Hermes anchors**

- `hermes_cli/doctor.py`
- `tools/computer_use/doctor.py`
- `scripts/discord-voice-doctor.py`

Hermes has substantial operator checks, but TaskChad should improve on its print-driven shape. Build a registry of typed diagnostic probes and expose the same JSON result to CLI, API, dashboard, and support bundles.

### 2.5 Config lifecycle

**Hermes anchors**

- `hermes_cli/config.py::{check_config_version,validate_config_structure,migrate_config,load_config,load_config_readonly,set_config_value}`

Reusable patterns:

- schema/version awareness;
- readonly loading;
- migration;
- separation of behavioral config from secrets;
- profile-scoped configuration;
- secure writes.

TaskChad’s `config.py` is overgrown and mixes import-time environment mutation with static and lazy resolution. This should be replaced by typed subsystem settings composed into one immutable runtime snapshot.

### 2.6 Central approval and security layers

**Hermes anchors**

- `tools/approval.py`
- `tools/write_approval.py`
- `tools/tirith_security.py`
- `tools/threat_patterns.py`
- `tools/url_safety.py`
- `tools/website_policy.py`
- `gateway/authz_mixin.py`

TaskChad already has serious policy components, but they are spread across runtime, Cabinet, integrations, live-safety, route policy, kill switches, and extension controls. Consolidate them into a central policy decision service.

### 2.7 Durable coordination patterns

**Hermes anchors**

- `tools/delegate_tool.py`, `tools/async_delegation.py`
- `cron/jobs.py`, `cron/scheduler.py`, `tools/cronjob_tools.py`
- `hermes_cli/kanban_db.py`, `tools/kanban_tools.py`

The most valuable Hermes ideas for TaskChad:

- scoped capability leases for child agents;
- durable schedule/run/delivery separation;
- atomic worker claims;
- heartbeats and stale-claim release;
- DAG cycle prevention;
- distinct task attempts/runs;
- verification that model-referenced work items actually exist;
- evidence-backed completion rather than trusting prose.

---

## 3. Target architecture for “The Polished Homie”

```text
Homie Runtime Core
├── ConversationRuntime (immutable per-session manifest)
├── PersonaTurnService
├── SessionEventStore
├── CapabilityRegistry
├── PolicyEngine
├── JobControlPlane
└── VerificationService

Cognitive Services
├── IdentityService
├── MemoryService
├── RecallService
├── AmendmentService
├── SkillRepository
└── CuratorService

Extension Perimeter
├── RuntimeProvider
├── CapabilityProvider / Plugin
├── MCP Bridge
├── PlatformAdapter
├── SpeechProvider
├── MemoryBackend
└── SchedulerBackend

Operator Plane
├── Structured Doctor
├── Audit + Rollback
├── Capability Gateway
├── Job Timeline
├── Proof Manifests
└── Config Explain/Migrate
```

### Core records

#### `CapabilityDescriptor`

- stable ID and version;
- input/output JSON schema;
- invocation binding;
- permissions and side-effect class;
- authentication requirements;
- availability probe;
- timeout/retry/idempotency policy;
- audit and approval requirements;
- provider/plugin owner.

#### `PolicyDecision`

- decision: allow, deny, require approval, allow with constraints;
- subject/persona/tenant;
- capability and normalized arguments;
- resource scope;
- policy chain and version;
- operation digest;
- approval actor/source;
- decision evidence.

#### `SessionEvent`

- session ID and monotonic sequence;
- event type/role;
- structured payload;
- correlation/tool-call/job IDs;
- visibility and provenance;
- timestamp and hash.

#### `SkillRecord`

- origin, version, source URI, content hash;
- state: proposed, quarantined, active, archived, rejected;
- trust level and scan report;
- usage/view/patch counts;
- required capabilities;
- optional typed procedure manifest;
- replay fixtures and evaluation score;
- supersedes/superseded-by links.

#### `MemoryEntry`

- namespace: user, identity, durable fact, preference, procedure;
- source events and evidence;
- confidence and sensitivity;
- active/proposed/rejected/stale state;
- expiration and supersession;
- prompt projection rules.

#### `JobRun`

- definition and immutable resolved prompt/skill versions;
- owner/persona/tenant;
- lease, heartbeat, fencing token;
- attempts, retries, backoff;
- state transitions and cancellation;
- output contract and verification evidence;
- delivery state separate from execution state.

---

## 4. Prioritized implementation roadmap

## Phase 0 — Trust closure

### P0.1 Amendment-aware rollback

Add to `.claude/chat/cognition/amendments.py` or a new focused `amendment_rollback.py`:

- `list_amendment_snapshots()`;
- `rollback_amendment(proposal_id, actor, reason)`;
- verify target current hash equals recorded `after_hash`;
- refuse on conflict unless explicitly forced;
- restore atomically;
- write new before/after hashes;
- mark ledger state `rolled_back`;
- expose CLI/chat/API/dashboard controls.

**Acceptance:** a test applies an amendment, edits nothing else, rolls it back, verifies byte-identical restoration and a durable rollback audit event. A second test modifies the target after application and verifies rollback refuses.

### P0.2 Fail-closed behavioral audit

Behavior-changing operations—skill promotion, amendment application, rollback, policy override—must not activate if the audit event cannot be durably committed.

**Acceptance:** simulated audit-sink failure leaves the skill quarantined and the identity file unchanged.

### P0.3 Evidence verification by default

All autonomous amendment paths must resolve cited evidence within allowed roots, verify existence/readability, and bind evidence hashes into the proposal.

### P0.4 Public proof levels

Replace ambiguous “shipped/live-proven” claims with generated levels:

- scaffolded;
- implemented;
- unit-proven;
- integration-proven;
- externally-live;
- production-supported.

Generate documentation traceability from committed manifests.

## Phase 1 — Runtime coherence

### P1.1 Central `CapabilityRegistry`

Unify chat commands, intents, integrations, skills/procedures, MCP tools, overlays, Cabinet tools, and executors under `CapabilityDescriptor`.

### P1.2 Central `PolicyEngine`

Move Cabinet policy, route policy, live-safety, integration checks, kill switches, and extension allow/deny behind one decision API. Preserve subsystem policies as rule providers, not independent enforcement paths.

### P1.3 Immutable session manifests

At session creation, compile and hash:

- base policy;
- identity/persona projection;
- user/durable memory snapshot;
- runtime/provider selection;
- capability schemas;
- policy version.

Do not change the system/tool prefix mid-session without an explicit session transition.

### P1.4 Unified `PersonaTurnService`

Replace duplicate Discord/web/Cabinet/main-engine turn assembly with one service. Adapters should only normalize ingress and render egress.

## Phase 2 — Typed configuration and diagnostics

### P2.1 Decompose `.claude/scripts/config.py`

Create typed domain settings:

- core/profile;
- memory;
- cognition;
- providers/runtime;
- tools/capabilities;
- channels;
- voice;
- orchestration;
- dashboard/security.

Compose them into one immutable validated config snapshot. Remove import-time `load_dotenv(..., override=True)` from library imports.

### P2.2 Versioned migrations

Add schema version, pure sequential migrations, pre-migration backup, atomic write, and post-migration validation.

### P2.3 `config explain <key>`

Show effective value, source layer, profile override, environment override, and whether restart is required.

### P2.4 Active doctor probes

Replace source-string/importability checks with bounded behavior probes:

- DB integrity;
- vault/index parity and source hashes;
- embedding compatibility;
- skill audit writability;
- amendment lock/write/rollback readiness;
- mailbox claim/ack round-trip;
- provider auth;
- channel send permissions;
- stale job/lease detection.

Return one structured report used by CLI/API/dashboard.

## Phase 3 — Durable jobs and orchestration

### P3.1 Unified job model

Merge background tasks, Queue Next, Convoy subtasks, Team Tick, scheduled cognition, and cron-like work into one durable `JobRun` model.

### P3.2 Leases, heartbeats, fencing, recovery

Add worker heartbeat, stale-claim recovery, fencing tokens, idempotency keys, retries/backoff, dead-letter state, and cancellation/steering.

### P3.3 Operating Room event timeline

Persist run history, events, artifacts, approvals, decisions, costs, failures, and resume state. Stream events to the dashboard rather than only producing final proof packets.

### P3.4 Output-contract verification

Workers cannot complete jobs solely by saying they succeeded. Require artifact existence, file hash, test result, URL/ID lookup, or another task-specific verifier.

## Phase 4 — Memory and skill maturity

### P4.1 Transactional memory journal

Journal source mutation, entity extraction, embedding/index updates, completion, and repair state. Make stale/partial indexes first-class health statuses.

### P4.2 Authorship and learning provenance

Add `author_id`, `is_operator`, persona, channel, and source event IDs to messages before using them as learning corpora. Extend the canonical path to Cabinet and other channels.

### P4.3 Typed skill manifests

Keep `SKILL.md` for human/agent instructions, but add:

- input/output schema;
- required capabilities;
- side-effect class;
- version/content hash;
- entrypoint or workflow graph;
- replay fixtures;
- evaluation results;
- rollback/deprecation state.

### P4.4 Curator lifecycle

Add usage telemetry, content hashes, pin/archive/restore, consolidation proposals, deterministic apply, and pre-change backup. The curator proposes; trusted code validates and applies.

## Phase 5 — Extension and adapter polish

### P5.1 Capability-negotiated platform adapters

Every adapter declares support for threads, edits, attachments, voice, buttons, streaming, steer/cancel, commands, and delivery receipts. Generate conformance tests from the declaration.

### P5.2 Isolated plugin host

Give plugins capability-scoped interfaces and restricted model access. Do not expose raw secrets or the main model client.

### P5.3 Speech pipeline

Separate recording, STT, conversation, TTS, transcoding, and playback. Store audio as immutable artifacts with retention policy.

---

## 5. Highest-value GitHub epics

1. **Trustworthy Self-Amendment** — rollback, mandatory audit, verified evidence, Audit UI.
2. **Unified Capability and Policy Kernel** — descriptors, invocation, approvals, audit.
3. **Persona Turn Unification** — one engine across web, Discord, Cabinet, and scheduled cognition.
4. **Typed Configuration and Doctor** — schemas, migration, explain, active probes.
5. **Durable Homie Jobs** — leases, heartbeats, recovery, retries, cancellation, proof.
6. **Skill Repository and Curator** — provenance, quarantine, usage, replay, archive/restore.
7. **Transactional Vault and Recall Health** — journal, freshness, provenance, repair.
8. **Operating Room Timeline** — persistent events, artifacts, approvals, costs, resume.
9. **Adapter Capability Contracts** — declarations and conformance tests.
10. **Generated Documentation Traceability** — symbols, tests, proof levels, limitations.

---

## 6. What not to copy from Hermes

- global singleton registries and process-global context;
- multi-thousand-line config, provider dispatch, gateway, or database modules;
- filesystem path conventions as the only authoritative database;
- print-driven diagnostics as the domain API;
- dotted-path string mutation as the internal config model;
- platform-specific logic in the core runtime;
- in-process scheduling as the only durability mechanism;
- unrestricted plugins or inherited child-agent secrets;
- regex-only security decisions;
- mutable history rewriting without preserved source events;
- model prose as proof of completed work.

---

## 7. Documentation/source discrepancies found

- `docs/manual/features/memory-and-recall-system.md` references a shipped `.claude/skills/vault-ops` implementation, but no tracked `vault-ops` path exists at the assessed commit.
- `dashboard/web/src/pages/Audit.tsx` is a placeholder although audit is described as an operator destination.
- `dashboard/README.md` calls Cabinet a placeholder despite substantial current implementation.
- README test counts appear stale or use a different counting method from the current tracked source.
- “Every consumer uses one recall entrypoint” is an intention, not mechanically enforced.
- Cabinet voice is partial; browser microphone → transcription → Cabinet response remains deferred.
- Operating Room is not yet a full durable operations control plane.

---

## 8. Definition of “polished” for The Homie

The Homie should be considered polished when:

1. every capability has a typed schema, availability state, policy, and audit owner;
2. every autonomous mutation is evidence-bound, reversible, and visible;
3. every job survives process restart or clearly declares itself ephemeral;
4. every completion claim has verification evidence;
5. every persona uses the same turn engine and remains isolated by request scope;
6. every configuration value can be validated, migrated, and explained;
7. every health claim is based on an active probe or explicitly labeled structural;
8. every skill has provenance, trust state, usage history, versioning, and rollback;
9. every operator surface calls the same domain services;
10. documentation maturity labels are generated from code/test/proof manifests.

---

## Final recommendation

Do not start by adding more headline features. TaskChad OS already has enough differentiated product surface. The fastest route to a “Hermes-polished Homie” is:

1. finish amendment rollback and fail-closed audit;
2. unify capability/policy authority;
3. unify persona turns;
4. replace fragmented configuration with typed snapshots and migrations;
5. unify durable jobs and verification evidence;
6. then mature memory and skills with transactional journals, provenance, replay, and curation.

That sequence preserves what makes The Homie unique while installing the operational spine that makes Hermes feel dependable.