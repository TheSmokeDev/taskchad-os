# TaskChad OS Polish Architecture Specification

- **Status:** Normative north-star specification
- **Audience:** maintainers, subsystem owners, implementers, reviewers, and operators
- **Scope:** All supported TaskChad OS / The Homie product distributions and their runtime, cognition, orchestration, extension, and operator planes; applicability is defined in §11
- **Source assessment:** [`taskchad-os-hermes-polish-assessment.md`](taskchad-os-hermes-polish-assessment.md)
- **Operator documentation:** [`../manual/README.md`](../manual/README.md)

---

## 1. Purpose and interpretation

This specification defines the target architecture by which TaskChad OS becomes operationally coherent and provably dependable without surrendering its identity-first product differentiation. It is a long-lived contract, not an assertion that the target already exists.

The key words **SHALL**, **SHALL NOT**, **SHOULD**, **SHOULD NOT**, and **MAY** are normative:

- **SHALL / SHALL NOT**: mandatory for the stated conformance level.
- **SHOULD / SHOULD NOT**: expected unless a documented, reviewed exception explains the trade-off and compensating control.
- **MAY**: permitted but not required.

A requirement identified as `POL-*` is independently traceable. All SHALL/SHALL NOT clauses, numbered safety invariants, conformance rules, and the definition in §13 are normative; headings, diagrams, implementation order, “Current” statements, and explanatory prose are informative unless they contain a `POL-*` requirement. “Target” alone does not make prose normative. A source-path cross-reference is an implementation anchor, not a mandate to preserve that file layout.

When this specification conflicts with feature marketing or a manually maintained maturity label, generated evidence and this specification govern architecture conformance; the feature manual remains the operator guide.

## 2. Product intent

TaskChad OS SHALL be an **identity-first cognitive agent operating system**: a durable, inspectable self and a family of isolated personas that remember, reason, learn conservatively, coordinate work, and act only through governed capabilities.

Architectural polish does not mean adding the most features. It means that the differentiated concepts enabled in a claimed product scope—Living Self, persona profiles, Markdown vault, Cabinet, Convoy, and Operating Room—share one dependable lifecycle spine:

> resolve identity → assemble a stable session → recall with provenance → decide policy → invoke typed capabilities → persist events → verify effects → present operator proof.

TaskChad SHALL retain the distinction between:

1. **identity and cognition**, which explain who is acting and why;
2. **authority**, which determines what that actor may do;
3. **execution**, which performs bounded work; and
4. **proof**, which demonstrates what actually occurred.

It SHALL NOT become a generic chat wrapper, an ungoverned self-modifying agent, or a clone of Hermes internals. Hermes-inspired lifecycle discipline MAY be adopted; global mutable state, giant cross-domain modules, filesystem conventions as the only database, and prose-as-proof SHALL NOT be adopted.

## 3. Architectural principles

### POL-PR-001 — Identity precedes capability
Every interactive turn, scheduled cognition run, delegated job, and external action SHALL identify the tenant, persona, operator relationship, and applicable identity projection before capability selection. A provider or channel SHALL NOT silently become the identity authority.

### POL-PR-002 — One domain path, many surfaces
CLI, API, dashboard, desktop, mobile, channel, Cabinet, and scheduler surfaces SHALL call the same domain services for equivalent operations. Adapters SHALL normalize ingress, propagate request context, and render egress; they SHALL NOT reimplement recall, turn assembly, policy, mutation, or proof semantics.

### POL-PR-003 — Narrow waist, explicit edges
Core behavior SHALL depend on typed ports and records rather than provider-, platform-, or storage-specific details. Providers, plugins, MCP bridges, channels, speech systems, memory backends, and schedulers SHALL attach at the extension perimeter.

### POL-PR-004 — Stable context, append-only truth
Canonical session and operation history SHALL be append-only. Derived summaries and prompt projections MAY change, but SHALL retain links to source events and SHALL NOT rewrite those source events. A session’s system/tool prefix SHALL remain stable except through an explicit, recorded transition.

### POL-PR-005 — Default-deny effects, graceful cognition
Authority failures and behavior-changing durable mutations SHALL fail closed. Optional cognitive enrichment MAY fail open to an ordinary reply or no-op when doing so causes no external or durable effect. Every degraded path SHALL emit a typed observable outcome. “Fail open” in the Living Self manual SHALL NOT be interpreted as permission to bypass policy, audit, or mutation gates.

### POL-PR-006 — Proof over claims
Completion, health, maturity, and documentation claims SHALL be derived from evidence. Model prose, successful dispatch, source-string presence, or importability alone SHALL NOT prove completion or health.

### POL-PR-007 — Isolation is request-scoped
Tenant, persona, secret, memory, capability, and job authority SHALL be carried in explicit request context. Process-global active-profile state SHALL NOT be the security boundary. Subprocess isolation MAY remain a compatibility control during migration.

### POL-PR-008 — Human-readable projections, typed authority
Markdown SHALL remain a first-class, portable human/agent projection for identity, memory, and skills. Files and directory placement SHOULD NOT be the sole authoritative representation of lifecycle state, provenance, policy, or transactional progress.

### POL-PR-009 — Reversible evolution
Self-improvement SHALL be evidence-bound, policy-gated, audited, versioned, and reversible. The model MAY propose; trusted deterministic code SHALL validate and apply.

### POL-PR-010 — Honest maturity
A feature SHALL declare limitations and the strongest evidence level actually attained. “Shipped,” “live,” and “production-ready” SHALL NOT be used as interchangeable proof states.

## 4. Terminology

- **Homie Runtime Core:** the narrow orchestration waist for sessions, persona turns, capabilities, policy, jobs, verification, and events.
- **Living Self:** scheduled and per-turn cognitive faculties over durable identity and memory; it is not an always-running autonomous consciousness.
- **Persona:** an isolated identity/profile with explicit configuration, memory namespace, capabilities, and lifecycle.
- **Tenant:** the top-level isolation and policy boundary. A persona belongs to exactly one tenant context for an operation.
- **Request Context:** immutable propagation object containing tenant, persona, actor, session/job, correlation, policy, and credential references.
- **Session Manifest:** immutable, hashed snapshot of the session prefix inputs: identity projection, base policy, runtime selection, capability schemas, and prompt-critical configuration. Per-turn recall is not part of this immutable prefix.
- **Session prefix:** the system instructions and tool definitions compiled from a Session Manifest; it is immutable until a recorded manifest transition.
- **Turn Context:** append-only/derived per-turn inputs, including current recall results and provenance, messages, and transient cognition; changing it does not transition the Session Manifest.
- **Capability:** a typed, discoverable operation with declared authority, side effects, availability, and invocation semantics.
- **Policy Decision:** durable decision to allow, deny, require approval, or allow with constraints for a normalized operation.
- **Approval:** time- and scope-bounded operator authorization bound to an operation digest; it is not a reusable boolean.
- **Operation digest:** hash of a canonical serialization of descriptor ID/version, normalized arguments, target/resource scope, tenant/persona/actor, relevant state version, and constraint-bearing context; credentials and secrets are represented by stable references/digests, not plaintext.
- **Relevant state version:** the version or concurrency token for mutable facts on which authorization depends, as declared by policy/capability (for example target ACL, policy snapshot, or resource revision); unrelated telemetry is excluded.
- **Idempotency key:** caller- or service-generated stable identifier scoped to an operation class and authority domain that binds retries to one normalized operation and terminal outcome.
- **Session Event:** append-only canonical record of an occurrence in a session or turn.
- **Job Definition / Job Run / Attempt / Delivery:** respectively desired work, a resolved execution instance, one execution try, and independent output delivery state.
- **Artifact:** immutable or content-addressed output with media type, provenance, retention, and digest.
- **Proof Manifest:** machine-readable set of claims, evidence references, verifier results, and maturity level.
- **Projection:** derived representation for prompts, Markdown, UI, indexes, or reports; it is rebuildable or traceable to canonical records.
- **Behavior-changing mutation:** any change to durable identity, active skills, policy, configuration, authority, or externally visible state.
- **Structural probe:** verifies shape or presence only. **Behavioral probe:** exercises a bounded real operation.
- **Cabinet:** a multi-persona deliberation experience, not a separate execution kernel.
- **Convoy:** dependency-aware coordinated work, represented through the common job model rather than a separate proof model.
- **Supported:** enabled, documented, and included in a distribution's declared applicability manifest; code that is disabled, experimental, or unavailable is not supported merely because it exists.
- **Invokable operation:** an operation reachable by an actor or automated component through a supported surface. UI views, documentation, passive projections, and non-executable skill text are not invokable operations.
- **Publication boundary:** the atomic visibility point after which other components may rely on a mutation.
- **Canonical:** authoritative for the named concern, not necessarily the sole physical copy.

## 5. Current reality and target boundary

Current strengths include structured cognition (`.claude/chat/cognition/`), persona lifecycle (`.claude/scripts/personas/`), canonical recall (`.claude/chat/recall_service.py`), vault/index implementations (`.claude/scripts/memory_index.py`, `.claude/scripts/db.py`), skills (`.claude/chat/cognition/skills.py`), and credible local orchestration (`.claude/scripts/orchestration/`). The manual accurately presents broad operator surfaces, but some maturity labels exceed the evidence taxonomy required here.

Current fragmentation includes multiple persona turn paths (`.claude/chat/engine.py`, `discord_persona_runtime.py`, `web_persona_runtime.py`, Cabinet and scheduled paths), parallel capability/policy authorities, import-time configuration behavior in `.claude/scripts/config.py`, source/index consistency gaps, an incomplete amendment restore lifecycle in `.claude/chat/cognition/amendments.py`, and proof assembled after the fact rather than emitted by every operation.

The target is the following logical boundary; modules MAY be introduced incrementally and names MAY vary only if the contracts remain recognizable:

```text
Homie Runtime Core
  ConversationRuntime ─ PersonaTurnService ─ SessionEventStore
  CapabilityRegistry  ─ PolicyEngine       ─ ApprovalService
  JobControlPlane     ─ VerificationService

Cognitive Services
  IdentityService ─ MemoryService ─ RecallService
  AmendmentService ─ SkillRepository ─ CuratorService

Extension Perimeter
  RuntimeProvider ─ CapabilityProvider/Plugin ─ MCP Bridge
  PlatformAdapter ─ SpeechProvider ─ MemoryBackend ─ SchedulerBackend

Operator Plane
  Structured Doctor ─ Audit/Rollback ─ Capability Gateway
  Job Timeline ─ Proof Manifests ─ Config Explain/Migrate
```

## 6. Subsystem requirements

### 6.1 Conversation Runtime and Persona Turn Service

- **POL-RT-001:** `PersonaTurnService` SHALL be the only supported domain entry point for constructing and executing a persona turn across main chat, web, Discord, Cabinet, and equivalent future channels.
- **POL-RT-002:** A turn SHALL consume an immutable Request Context and Session Manifest plus a Turn Context assembled for that turn, and SHALL emit Session Events, usage/cost observations, policy decisions, tool results, and a final response reference. Recall results SHALL carry source IDs/digests, retrieval time, scope, and projection version and SHALL NOT mutate the session prefix.
- **POL-RT-003:** Conversation Runtime SHALL compile and hash the system/tool prefix at session creation. Changes to identity, policy, provider, tool schemas, or prompt-critical configuration SHALL require an explicit session transition event and a new manifest.
- **POL-RT-004:** Internal monologue MAY influence the current turn but SHALL NOT enter the user-visible transcript or learning corpus. This preserves the current invariant implemented around `.claude/chat/cognition/cognitive_pass.py` and working-memory regions.
- **POL-RT-005:** Optional recall or cognition failure SHOULD degrade to a traceable ordinary turn; inability to establish identity, isolation, policy, or canonical persistence SHALL stop any side effect.
- **POL-RT-006:** Cabinet SHALL use the same turn service per participant and SHALL record room, speaker, persona, and source-event provenance.

### 6.2 Identity and persona lifecycle

- **POL-ID-001:** IdentityService SHALL own validated identity loading, projection, versioning, and amendment boundaries. Runtime providers SHALL receive projections, not write authority.
- **POL-ID-002:** Every persisted message, memory candidate, amendment, and job SHALL carry tenant, persona, author, and actor provenance where applicable.
- **POL-ID-003:** Persona storage, recall indexes, configuration, ports/processes, and credentials SHALL be isolated and collision-checked. Existing lifecycle behavior in `.claude/scripts/personas/{core,lifecycle,services,atomic}.py` SHOULD be preserved behind the service boundary.
- **POL-ID-004:** Model-generated content SHALL NOT mint operator-authored or `explicit` provenance. Direct operator beliefs SHALL NOT be demoted solely by model judgment; conflicts between explicit beliefs SHALL be surfaced.
- **POL-ID-005:** Clone, import, export, migration, delete, and repair SHALL accept an idempotency key when retry can repeat a mutation. Reuse with the same normalized operation SHALL return the original terminal outcome (or its stable reference) without repeating effects; reuse for a different operation SHALL fail. Operations that cannot be safely retried SHALL reject automatic retry and expose their non-idempotent status. Publication SHALL be atomic and all outcomes auditable.

### 6.3 Session Event Store

- **POL-SE-001:** Canonical session history SHALL be an append-only ordered event stream with a monotonic sequence per stream and tamper-evident hashes or equivalent integrity controls.
- **POL-SE-002:** Concurrent appends SHALL use optimistic concurrency or an equivalent ordering guarantee; duplicate delivery SHALL be safe through idempotency keys.
- **POL-SE-003:** Compression SHALL create a `CompactionArtifact` linked to an inclusive source-event range, source digest, algorithm/model version, and validation result. Source events SHALL remain canonical under retention policy.
- **POL-SE-004:** Visibility classification SHALL prevent private reasoning, secrets, and restricted artifacts from leaking to user transcripts, proof bundles, or learning corpora.
- **POL-SE-005:** Event schema evolution SHALL be versioned and readers SHALL tolerate known older versions during the compatibility window.
- **POL-SE-006:** Retention SHALL be selected by tenant policy and data class and SHALL define minimum/maximum duration, legal hold, archival, and deletion authority. Redaction SHALL create a visibility-controlled replacement projection while retaining integrity metadata; authorized deletion SHALL use a tombstone containing non-sensitive identity, reason, authority, time, and prior digest where law/policy permits. A hash chain SHALL either bridge the tombstone or start an explicitly linked new segment; deletion SHALL never be represented as an intact original chain.

### 6.4 Capability Registry and invocation

- **POL-CP-001:** Every supported invokable operation—including executable commands, intents, integrations, procedures, MCP/Cabinet tools, overlays, and executors—SHALL resolve to exactly one canonical `CapabilityDescriptor`. Multiple names or surfaces MAY be aliases, but each alias SHALL map deterministically to the same descriptor ID/version and SHALL NOT carry independent policy or invocation semantics. Non-executable content and passive projections are outside this requirement.
- **POL-CP-002:** Descriptors SHALL declare stable ID/version, JSON-compatible input/output schemas, side-effect class, permissions, authentication references, owner, availability probe, timeout, retry, idempotency, approval, audit, and invocation binding.
- **POL-CP-003:** Only capabilities whose availability and policy preconditions are satisfied SHALL enter a session manifest.
- **POL-CP-004:** Capability invocation SHALL validate normalized input before policy evaluation and validate output before completion.
- **POL-CP-005:** Registry discovery SHALL be side-effect free. Registration SHALL NOT expose raw secrets or unrestricted main-model clients to plugins.
- **POL-CP-006:** At Level 2 and above, the read-only Capability Gateway, if included in the supported product scope, SHALL be a projection of CapabilityRegistry availability and policy metadata and SHALL NOT infer or own independent status. Before Level 2 it MAY remain a declared legacy projection and cannot evidence registry conformance.

### 6.5 Policy and approval

- **POL-PA-001:** PolicyEngine SHALL be the single decision API for capability, Cabinet, route, live-safety, integration, kill-switch, extension, tenant, and mutation policies. Existing policies MAY remain rule providers but SHALL NOT provide bypass paths.
- **POL-PA-002:** Decisions SHALL be one of `allow`, `deny`, `require_approval`, or `allow_with_constraints` and SHALL bind subject, tenant/persona, capability/version, normalized arguments, resource scope, policy chain/version, operation digest, and evidence.
- **POL-PA-003:** Approval SHALL bind actor, operation digest, constraints, issuance/expiry, and one-time or bounded-use semantics. Argument or target changes SHALL invalidate approval.
- **POL-PA-004:** External writes, durable identity mutation, active skill promotion, policy override, rollback, and destructive operations SHALL be default-denied absent explicit policy authorization.
- **POL-PA-005:** Child agents and plugins SHALL receive capability leases with least privilege, explicit scope, expiry, and revocation. Parent credentials SHALL NOT be inherited wholesale.
- **POL-PA-006:** Regex or model judgment MAY contribute a risk signal but SHALL NOT be the sole authorization mechanism.
- **POL-PA-007:** Immediately before a side-effecting invocation crosses its execution boundary, trusted code SHALL atomically (in one transaction/serialized decision, or with equivalent compare-and-set guarantees) recompute the operation digest from canonical descriptor/version, normalized arguments, target/resource, subject/context, and relevant state version; confirm the policy decision is current; confirm approval is unexpired, unrevoked, within use limits, and constraint-matching; consume/reserve any bounded use; and issue an invocation authorization bound to that digest. A change or revocation before this commit SHALL prevent invocation. Executors SHALL reject a missing, expired, consumed, or digest-mismatched authorization. Long-running operations SHALL define whether revocation prevents start only or also triggers cooperative cancellation.

### 6.6 Memory and recall

- **POL-ME-001:** Human-readable Markdown SHALL be authoritative for user-authored knowledge/identity/skill instruction content where the declared vault registry assigns it ownership. Typed repository records and mutation journals SHALL be authoritative for stable IDs, provenance, lifecycle, policy state, transactional progress, retention, and index status; Markdown front matter MAY project but SHALL NOT independently override those fields. Indexes are derived, SHALL be rebuildable, and SHALL report source digest, embedding compatibility, freshness, and partial/corrupt status. Conflicts SHALL be detected and resolved according to the registry's per-field owner, never by last-writer-wins across authorities.
- **POL-ME-002:** Every recall consumer SHALL use RecallService or an explicitly conforming port; direct index queries SHALL NOT silently bypass tenant/persona filtering, sanitization, budgeting, or provenance.
- **POL-ME-003:** Memory mutation SHALL use a journal that records source write intent, source commit, extraction, embedding/index updates, completion, and repair state. A crash SHALL result in either completed consistency or a detectable repairable state.
- **POL-ME-004:** `MemoryEntry` SHALL distinguish identity, user, durable fact, preference, procedure, and other registered namespaces, with evidence, confidence, sensitivity, lifecycle state, expiry, supersession, and prompt projection rules.
- **POL-ME-005:** Learning input SHALL include author ID, operator status, persona, channel, and source event IDs. Assistant output and untrusted Cabinet/channel content SHALL NOT be treated as operator evidence.
- **POL-ME-006:** A declarative vault registry SHOULD replace hard-coded slots while preserving compatible Markdown locations.

### 6.7 Amendments and self-improvement

- **POL-AM-001:** Every amendment SHALL cite readable evidence confined to allowed roots; evidence content hashes SHALL be bound into the proposal before evaluation.
- **POL-AM-002:** Durable identity mutation SHALL require deterministic validation, policy approval, independent evaluation where configured, an append-only audit commit, and an atomic target write.
- **POL-AM-003:** Audit commit failure SHALL prevent activation. Cognitive convenience SHALL never make behavior-changing mutation fail open.
- **POL-AM-004:** Before mutation, AmendmentService SHALL create a verified snapshot containing proposal, target, before hash/content reference, intended after hash, actor, policy version, and evidence bindings.
- **POL-AM-005:** Rollback SHALL verify the current target hash equals the recorded post-application hash, restore atomically, record actor/reason and new before/after hashes, and mark the proposal rolled back. On conflict it SHALL refuse unless an explicit forced path is separately authorized and preserves the displaced state.
- **POL-AM-006:** A rejected or interrupted amendment SHALL leave no active partial mutation. Recovery SHALL reconcile ledger, snapshot, and target state deterministically.
- **POL-AM-007:** Core constitutional identity SHALL NOT self-mutate autonomously. Expansion of this boundary requires a separately reviewed architecture decision and migration plan.

### 6.8 Skill Repository and curator

- **POL-SK-001:** Skills SHALL preserve the lifecycle `proposed → quarantined → scanned/evaluated → approved → active → archived/rejected` and SHALL NOT enter procedural memory merely by directory placement.
- **POL-SK-002:** A `SkillRecord` SHALL include origin, source URI, version, content hash, state, trust, scan reports, required capabilities, usage telemetry, supersession, and rollback/deprecation state.
- **POL-SK-003:** Active skills SHALL include a typed manifest defining input/output, side-effect class, required capabilities, and version. Executable procedures SHOULD include replay fixtures and evaluation results.
- **POL-SK-004:** Promotion SHALL be transactional and audit-fail-closed. A failed scan, evaluation, manifest validation, or audit write SHALL leave the skill quarantined.
- **POL-SK-005:** CuratorService MAY propose consolidation, patch, archive, restore, or promotion. Trusted code SHALL create a pre-change backup, validate content hashes, and deterministically apply the accepted change.
- **POL-SK-006:** `SKILL.md` SHALL remain a supported instruction projection; the repository record SHALL own lifecycle truth.

### 6.9 Job Control Plane and orchestration

- **POL-JB-001:** Background tasks, Queue Next, Convoy subtasks, Team Tick, scheduled cognition, webhook work, and cron-like work SHALL converge on a common Job Definition/Run/Attempt/Delivery model.
- **POL-JB-002:** A Job Run SHALL pin immutable IDs/digests or retained snapshots for resolved prompt, identity, capability, skill, policy, and configuration inputs needed to explain its authority and behavior. Secrets, volatile provider state, and nondeterministic external responses SHALL be referenced/redacted rather than copied, and reproducibility limitations SHALL be recorded; pinning does not promise bit-for-bit replay.
- **POL-JB-003:** Durable jobs SHALL provide atomic exclusive claim, bounded ownership with renewal/liveness detection, stale-owner commit prevention, deduplication, controlled retry/backoff, cancellation, stale recovery, and terminal exhausted/quarantine handling. Leases, heartbeats, fencing tokens, idempotency keys, and dead-letter queues are acceptable mechanisms, not required names or storage designs.
- **POL-JB-004:** A stale worker SHALL NOT commit after its fencing token is superseded.
- **POL-JB-005:** Execution state and delivery state SHALL be independent. Failed notification SHALL NOT relabel successful execution as failed, nor successful dispatch prove execution success.
- **POL-JB-006:** DAG creation SHALL reject cycles and unresolved dependencies. Dependency release SHALL use compare-and-set or equivalent concurrency control.
- **POL-JB-007:** Ephemeral work MAY omit restart durability only if declared before dispatch, visible to the operator, and prohibited from making durable completion guarantees.
- **POL-JB-008:** Current orchestration in `.claude/scripts/orchestration/{db,convoy_service,mailbox_service,team_service,team_loop,team_executor}.py` is a credible local baseline; conformance SHALL NOT claim distributed scheduling until multi-worker fault tests prove it.
- **POL-JB-009:** For an external side effect, a stable operation key SHALL be persisted before dispatch and propagated to a provider that supports idempotency. The same key and payload SHALL converge on one provider operation/receipt; a key/payload mismatch SHALL fail. If the provider offers no idempotency or lookup, ambiguous timeout SHALL be recorded as `outcome_unknown`, SHALL NOT be blindly retried, and SHALL require reconciliation or explicit policy-authorized compensation/retry.

### 6.10 Verification and proof

- **POL-VF-001:** A worker SHALL NOT mark a job `verified` solely by asserting success. Each output contract SHALL name a verifier or produce a typed `UnverifiableResult` with reason code, attempted checks, available evidence, residual uncertainty, and policy-authorized disposition. Unverifiable work MAY be recorded as executed/accepted-with-limitation but SHALL NOT satisfy a verified-completion claim.
- **POL-VF-002:** Verifiers MAY check artifact existence and digest, test output, schema validity, URL/API resource lookup, database state, delivery receipt, or another task-specific condition.
- **POL-VF-003:** Verifier execution and result SHALL be persisted independently from worker prose and SHALL include verifier version and evidence references.
- **POL-VF-004:** Proof manifests SHALL be machine-readable, redact secrets, preserve stable evidence IDs/digests, and link claims to tests, probes, artifacts, and source versions.
- **POL-VF-005:** Documentation maturity SHOULD be generated from proof manifests. Manual labels MAY add explanation but SHALL NOT raise the generated level.

### 6.11 Configuration

- **POL-CF-001:** Configuration SHALL be decomposed into typed settings for core/profile, memory, cognition, runtime/providers, capabilities, channels, voice, orchestration, and dashboard/security, composed into an immutable validated runtime snapshot.
- **POL-CF-002:** Library imports SHALL NOT mutate the process environment or load `.env` with override semantics. Existing behavior in `.claude/scripts/config.py` SHALL be retired behind compatibility loading.
- **POL-CF-003:** Behavioral configuration and secrets SHALL be separated. Records SHALL reference secret identifiers, not serialize secret values.
- **POL-CF-004:** Configuration SHALL carry a schema version and support deterministic sequential migrations, pre-migration backup, atomic publication, and post-migration validation. Reversible migrations SHALL provide and exercise rollback/restore guidance. An irreversible migration SHALL require preflight declaration, operator authorization, a restorable pre-cutover backup/export, and a tested forward-recovery or restore-to-old-version procedure; it SHALL NOT claim in-place rollback.
- **POL-CF-005:** `config explain <key>` or an equivalent shared domain operation SHALL report effective value (redacted when secret), source layer, profile and environment overrides, validation, and restart requirement.
- **POL-CF-006:** Read-only load and validation SHALL NOT rewrite files. Unknown keys SHALL produce actionable diagnostics and SHALL NOT be silently discarded.

### 6.12 Diagnostics and operator plane

- **POL-OP-001:** Doctor SHALL execute a registry of typed, bounded probes. Every supported surface that exposes Doctor results (CLI, API, dashboard, desktop, or support bundle) SHALL render the same underlying structured report; a distribution need not implement every listed surface.
- **POL-OP-002:** Probe results SHALL include ID/version, scope, structural or behavioral kind, status, observed time, duration, evidence, redacted detail, remediation, and skipped/not-applicable reason.
- **POL-OP-003:** The probe registry SHALL cover each of the following when its subject subsystem is supported and safely testable: database integrity, vault/index parity, embedding compatibility, audit writability, amendment lock/write/rollback readiness, mailbox claim/ack, provider authentication, channel permissions, and stale ownership/jobs. An unsupported or unsafe subject SHALL return typed `not_applicable` or `skipped` with reason rather than a pass.
- **POL-OP-004:** A structural probe SHALL NOT produce a behavioral health claim. Destructive probes SHALL require an isolated fixture or explicit operator authorization.
- **POL-OP-005:** Operating Room SHALL project a persistent event timeline containing runs, attempts, artifacts, approvals, decisions, costs, failures, recovery, delivery, and proof. It SHALL NOT be a second source of orchestration truth.
- **POL-OP-006:** Operator mutation surfaces SHALL provide preview, impact/scope, policy result, approval, execution status, verification, audit ID, and rollback availability.

### 6.13 Extension perimeter and adapters

- **POL-EX-001:** Platform adapters SHALL declare support for threads, edits, attachments, voice, buttons, streaming, steer/cancel, commands, and delivery receipts. Conformance tests SHOULD be generated from declarations.
- **POL-EX-002:** Unsupported adapter capabilities SHALL fail explicitly or degrade through a declared fallback; they SHALL NOT be silently presented as successful.
- **POL-EX-003:** Plugins SHALL run through capability-scoped interfaces and SHOULD be process-isolated where they execute untrusted code. Raw secrets and unrestricted database/model clients SHALL NOT be exposed.
- **POL-EX-004:** Speech SHOULD separate recording, STT, conversation, TTS, transcoding, and playback. Audio artifacts SHALL have immutable IDs, provenance, sensitivity, and retention policy.
- **POL-EX-005:** Provider/platform-specific logic SHALL remain outside the Runtime Core. Provider selection SHALL NOT alter policy or identity semantics.

## 7. Core domain records

Every persisted or exchanged core domain record defined in this section SHALL have a schema version, stable identifier, creation time, and provenance appropriate to its domain. Ephemeral internal value objects and provider-native payloads need not duplicate these fields if wrapped by a conforming record. Timestamps alone SHALL NOT provide ordering where monotonic sequence or stale-owner prevention is required.

### 7.1 `RequestContext`
`tenant_id`, `persona_id`, `actor_id/type`, `operator_relationship`, `session_id` or `job_run_id`, correlation/causation IDs, policy snapshot ID, credential-reference set, locale/channel, trace context.

### 7.2 `SessionManifest`
Session and persona IDs; identity projection/version/digest; provider/model selection; ordered capability descriptor IDs/versions/schema digests; base policy version; prompt-critical config snapshot ID; compiled-prefix digest; creation reason; predecessor manifest and transition event. Per-turn recall boundaries/digests belong to Turn Context/Session Events, not this manifest.

### 7.3 `SessionEvent`
Stream ID and monotonic sequence; event ID/type/role; structured payload or artifact reference; tenant/persona/actor; correlation, causation, tool-call, and job IDs; visibility/sensitivity; timestamp; idempotency key; previous/current integrity hashes.

### 7.4 `CompactionArtifact`
Source stream/range and digest; summary content/artifact; algorithm/model/prompt versions; token accounting; validation; superseded compaction links. It SHALL be derived, never canonical history.

### 7.5 `CapabilityDescriptor`
Stable ID/version; owner/provider; input/output schemas; invocation binding; side-effect and resource classes; permission and authentication requirements; availability probe; timeout/retry/idempotency; approval/audit requirements; deprecation/supersession.

### 7.6 `PolicyDecision` and `ApprovalGrant`
Decision, subject and context, descriptor/version, normalized arguments and operation digest, scope/constraints, policy chain/version, evidence, expiry, approval reference. Grant adds approving actor/source, issue/expiry, allowed uses, revocation, and consumed state.

### 7.7 `MemoryEntry` and `MemoryMutation`
Entry namespace/content or content reference; tenant/persona; author/operator flags; source events/evidence; confidence/sensitivity; lifecycle, expiry, supersession; projection rules. Mutation adds journal state, source/index digests, extraction/embedding versions, and repair outcome.

### 7.8 `AmendmentProposal` and `RollbackRecord`
Target identity/version/path; operation; before/intended-after hashes; bounded hashed evidence; proposer/actor; tests/judgments; policy/audit references; snapshot; lifecycle state. Rollback adds expected current hash, restored hash, actor/reason, force authorization if any, displaced-state snapshot, and audit reference.

### 7.9 `SkillRecord`
Origin/source URI; version/content digest; lifecycle/trust; instruction projection; typed procedure manifest; capabilities and side effects; scan/evaluation reports; fixtures; usage/view/patch telemetry; pin/archive/restore state; supersession and backup references.

### 7.10 `JobDefinition`, `JobRun`, `JobAttempt`, `DeliveryRecord`
Definition specifies input/output contract, dependencies, policy, schedule/trigger, durability, retry and verifier. Run pins resolved versions and owner/persona/tenant. Attempt holds worker, claim, lease, heartbeat, fencing, state transitions, cost, outputs, and errors. Delivery independently holds destination, attempts, receipt, and state.

### 7.11 `ArtifactRecord` and `ProofManifest`
Artifact includes digest, size/media type, creator/source event, sensitivity, retention, and storage reference. Proof includes claim IDs, conformance level, subject/source revision, verifier results, artifact/test/probe references, environment scope, limitations, generation time, and signature/integrity metadata where required.

### 7.12 `DiagnosticResult` and `ConfigSnapshot`
Diagnostic fields are defined in POL-OP-002. Config Snapshot includes schema version, redacted typed values, per-key source metadata, digest, validation result, migration lineage, and restart domains.

## 8. Immutable safety invariants

These invariants apply at every conformance level and SHALL NOT be relaxed by feature flags:

1. **No identity ambiguity:** an operation with unresolved tenant/persona/actor SHALL NOT read private memory or invoke side-effecting capabilities.
2. **No cross-persona leakage:** memory, prompt projections, indexes, artifacts, and credentials SHALL be scoped before retrieval, not filtered only after retrieval.
3. **No authority by narration:** model output SHALL NOT grant approval, elevate provenance, waive policy, or prove completion.
4. **No unapproved durable mutation:** identity, active skill, policy, authority, configuration, and external state changes SHALL pass centralized policy and required approval.
5. **No activation without audit:** if the durable audit cannot commit, behavior-changing mutation SHALL NOT become active.
6. **No unverifiable silent mutation:** every durable mutation SHALL have before/after identity, actor, reason, operation digest, and recoverable or explicitly irreversible status.
7. **No blind rollback:** rollback SHALL compare expected and actual state and preserve displaced content on an authorized force path.
8. **No evidence escape:** autonomous amendment evidence reads SHALL be path-confined, symlink-safe, bounded, readable, and hashed.
9. **No transcript contamination:** private monologue and secrets SHALL NOT enter canonical user-visible history or learning corpora.
10. **No stale-worker commit:** fencing SHALL reject superseded attempts.
11. **No fake success:** dispatch, model assertion, or UI optimism SHALL NOT mark verified completion.
12. **No mutable session prefix without transition:** prompt-critical change SHALL create a manifest transition.
13. **No canonical-history rewrite:** compaction and redaction SHALL preserve governed source lineage; legally required deletion SHALL leave a tombstone/audit record where policy permits.
14. **No secret propagation by default:** plugins, child agents, artifacts, diagnostics, and proof bundles SHALL receive only scoped references or redacted values.
15. **No maturity inflation:** public status SHALL NOT exceed available proof.
16. **No authorization TOCTOU:** a side effect SHALL NOT start unless operation digest, current policy, approval freshness/revocation/use, constraints, and relevant state version are validated and committed as one authorization step immediately before execution; executors SHALL bind to that authorization.

## 9. Compatibility and migration constraints

- **POL-MG-001:** Migration SHALL be incremental and strangler-style. Existing CLI commands, routes, Markdown vaults, persona directories, SQLite data, and channel contracts SHALL remain usable through adapters during a documented compatibility window.
- **POL-MG-002:** Every persistent schema migration SHALL be versioned, restart-safe, backed up, and validated. It SHALL be either reversible with tested rollback or declared irreversible before execution with operator authorization and a tested restore-to-old-version or forward-recovery plan.
- **POL-MG-003:** Dual-write periods SHALL define authoritative ownership, comparison telemetry, divergence repair, and a bounded exit criterion. Indefinite dual authority is prohibited.
- **POL-MG-004:** Legacy readers MAY consume generated projections. New domain logic SHALL NOT add writes to a legacy store once authoritative ownership moves.
- **POL-MG-005:** Stable public IDs SHALL be preserved or mapped durably. Provider-specific IDs SHALL NOT become canonical IDs.
- **POL-MG-006:** Existing Markdown content SHALL migrate without lossy reformatting unless the operator explicitly accepts a previewed diff. Content hashes and backups SHALL prove preservation.
- **POL-MG-007:** Persona migration SHALL prove no cross-profile memory, token, process, port, or index collision before cutover.
- **POL-MG-008:** Policy centralization SHALL begin in observe/shadow mode, compare old and new outcomes, then enforce after divergence is resolved. Security-deny outcomes SHALL not be weakened during shadowing.
- **POL-MG-009:** Session Event Store adoption SHALL import or reference legacy transcript IDs without inventing unavailable provenance. Unknown provenance SHALL be labeled unknown.
- **POL-MG-010:** Job unification SHALL map existing Convoy/mailbox/team IDs and states. In-flight work SHALL be drained, adopted with fencing, or explicitly cancelled—never silently duplicated.
- **POL-MG-011:** Deprecations SHALL publish replacement, detection, warning period, telemetry, and removal criteria.
- **POL-MG-012:** A migration SHALL NOT claim cutover completion until old write paths are mechanically prevented and the applicable recovery path has been exercised: rollback for reversible migrations, or backup restore/forward recovery for irreversible migrations.

## 10. Operator and proof model

The operator is the ultimate authority for consequential action, not a passive log reader. For every consequential operation it exposes, an operator mutation/detail surface SHALL answer or link by stable correlation ID to:

1. **Who** acted, under which persona and tenant?
2. **What** normalized operation was requested?
3. **Why** was it allowed, denied, constrained, or held for approval?
4. **Which versions** of identity, policy, capabilities, skills, config, and provider were used?
5. **What happened** across attempts, recovery, and delivery?
6. **What evidence** verifies the result?
7. **Can it be reversed**, and what conflict would block reversal?

Audit records SHALL be append-only and queryable by correlation, actor, persona, capability, job, target, and time. Sensitive fields SHALL be redacted in projections without destroying integrity links. Support bundles SHALL be generated from the same typed records, use allowlisted inclusion, and record redaction policy.

Proof is layered:

- source and schema establish structure;
- tests establish repeatable bounded behavior;
- integration runs establish subsystem cooperation;
- external live evidence establishes real-provider/platform operation;
- production support additionally requires monitoring, runbooks, recovery exercises, ownership, and support window.

The dashboard Audit placeholder and bounded current Operating Room slice SHALL NOT be represented as a complete control plane until they consume these records and expose this loop.

## 11. Applicability, architecture conformance, and evidence maturity

### 11.1 Applicability

Each released distribution SHALL publish a machine-readable applicability manifest naming supported features/surfaces, architecture level claimed, excluded optional subsystems with rationale, and evidence references. Requirements apply as follows:

| Requirement domain | Applicability |
|---|---|
| Principles, identity/isolation, policy/approval, capability invocation, configuration, audit, migration, retention, and immutable safety invariants | Mandatory product-wide whenever the product performs the governed activity; cannot be excluded by disabling a UI |
| Conversation Runtime and Session Event Store | Mandatory for every supported interactive persona surface |
| Memory/recall | Mandatory when durable memory or recall is supported |
| Amendments/self-improvement | Mandatory when amendment or autonomous durable mutation is supported; otherwise such mutation SHALL be unreachable |
| Skills/curator | Mandatory for executable or promotable skills; passive instruction files remain subject to content ownership and capability rules |
| Job Control Plane | Mandatory for supported asynchronous, scheduled, or coordinated work; purely synchronous distributions MAY mark it not applicable |
| Verification/proof | Mandatory for every completion, health, architecture-level, or maturity claim, with typed unverifiable outcomes allowed only as specified |
| Doctor, Operating Room, and other operator views | Their underlying records and controls are mandatory for governed operations; each named view is conditional on being a supported surface |
| Extension adapters, speech, plugins, and external providers | Conditional on each declared supported integration |

An excluded feature SHALL NOT be advertised, enabled, or reachable through an undocumented path. `Not applicable` requires a test or inventory showing absence/unreachability and is not a pass. Product-wide architecture conformance is the lowest level attained by every mandatory and declared-supported applicable subsystem; scoped subsystem claims SHALL identify their scope and SHALL NOT be presented as product-wide.

### 11.2 Architecture conformance levels

Architecture levels describe implemented contracts and controls. Evidence maturity describes how a claim about those controls was established. These axes are distinct: controls MAY exist without enough evidence to admit a public architecture claim, and evidence above the admissibility floor neither adds a control nor raises the architecture level. Levels are cumulative.

For a named scope, calculate the architecture level deterministically as follows:

1. Expand the mapping below into individual requirement IDs. A range is inclusive, uses three-digit numeric ordering, and is valid only within its stated prefix.
2. Apply §11.1 and retain every mapped requirement applicable to the named scope. An exclusion affects applicability, never the requirement's mapped minimum level.
3. A candidate level `N` satisfies the control axis only when (a) every retained requirement whose `minimum_level <= N` is implemented and (b) every control bullet for Levels 0 through `N` below is implemented for the named scope. A failed or unknown applicable requirement or control bullet makes `N` and every higher candidate fail.
4. The **implemented architecture level** is the greatest candidate satisfying step 3. The **claimable architecture level** is the greatest such candidate whose claim also meets the evidence floor below. Report both when they differ; do not describe an unadmitted implementation result as a conformance claim.
5. For a product-wide result, calculate each mandatory and declared-supported scope and take the minimum. Record the applicability-manifest digest, expanded requirement set, per-requirement result, proof references, and scoring-rule version `polish-architecture-v1` in the Proof Manifest.

The following block is the normative, machine-checkable assignment of every `POL-*` requirement to exactly one minimum architecture level. Adding, removing, or renumbering a `POL-*` requirement SHALL update this block in the same change; duplicate, missing, overlapping, malformed, or nonexistent IDs SHALL make scoring invalid rather than defaulting a level.

```yaml
architecture_requirement_map:
  scoring_rule: polish-architecture-v1
  ranges:
    - { ids: "POL-PR-001..POL-PR-010", minimum_level: 1 }
    - { ids: "POL-RT-001..POL-RT-006", minimum_level: 2 }
    - { ids: "POL-ID-001..POL-ID-005", minimum_level: 1 }
    - { ids: "POL-SE-001..POL-SE-006", minimum_level: 2 }
    - { ids: "POL-CP-001..POL-CP-006", minimum_level: 2 }
    - { ids: "POL-PA-001..POL-PA-007", minimum_level: 1 }
    - { ids: "POL-ME-001..POL-ME-006", minimum_level: 4 }
    - { ids: "POL-AM-001..POL-AM-007", minimum_level: 1 }
    - { ids: "POL-SK-001..POL-SK-006", minimum_level: 5 }
    - { ids: "POL-JB-001..POL-JB-009", minimum_level: 4 }
    - { ids: "POL-VF-001..POL-VF-005", minimum_level: 1 }
    - { ids: "POL-CF-001..POL-CF-006", minimum_level: 3 }
    - { ids: "POL-OP-001..POL-OP-006", minimum_level: 3 }
    - { ids: "POL-EX-001..POL-EX-005", minimum_level: 5 }
    - { ids: "POL-MG-001..POL-MG-012", minimum_level: 3 }
```

### Level 0 — Scaffolded

Control requirements:

- Target ports, record schemas, owners, and architecture decisions exist.
- Legacy paths and migration mappings are inventoried.
- No runtime behavior is implied.

### Level 1 — Implemented / Trust-closed

Control requirements:

- Amendment-aware rollback refuses conflicts.
- Behavior-changing audit is fail-closed.
- Autonomous amendment evidence is confined, read, and hash-bound.
- Capability, policy, event, job, and proof records have runtime-enforced schemas.
- Immutable safety invariants are enforced by trusted code.

### Level 2 — Runtime-coherent

Control requirements:

- Central CapabilityRegistry and PolicyEngine govern supported effects.
- Session manifests are immutable and transitioned explicitly.
- Main web/Discord/Cabinet turn paths use PersonaTurnService when those surfaces are supported.
- Canonical Session Events and request-scoped identity/isolation are enforced.
- Equivalent operations across supported surfaces share policy and turn behavior through the same domain path.

### Level 3 — Operable

Control requirements:

- Typed config snapshots, migration, read-only load, and explain operations are active.
- Structured Doctor surfaces share typed probes.
- Audit, approvals, rollback, capability status, and event timeline are available to operators.
- Support bundles are redacted and evidence-linked.
- Backup/restore and audit-failure recovery controls are implemented.

### Level 4 — Durable

Control requirements:

- Common Job Control Plane covers supported scheduled, Convoy, Team, and background work.
- Restart recovery, bounded ownership/liveness, stale-owner exclusion, retries, cancellation, and exhausted-work handling are implemented.
- Output contracts and persisted verifier results gate completion.
- Memory journals make interrupted index updates detectable and repairable.

### Level 5 — Ecosystem-polished

Control requirements:

- Typed Skill Repository and curator lifecycle, including replay and restore controls, are active.
- Plugins and child agents are capability-scoped; adapters declare conformance contracts.
- Declared provider/channel flows implement their adapter and proof contracts.
- Generated documentation traceability is active.

### Level 6 — Production-supported

Control requirements:

- SLOs, monitoring, alerts, ownership, incident/runbook coverage, retention, upgrade/rollback, and disaster-recovery controls are defined and operational.
- Multi-worker claim and failure controls exist for the supported deployment topology.
- Security review and dependency/secret-handling governance are current.
- Proof manifests identify the supported OS/provider/channel matrix and limitations.

### 11.3 Evidence maturity (independent axis)

Every conformance or feature claim SHALL separately report one of these evidence states and its observation time/environment: **declared** (schema/design only), **structurally-probed**, **unit-proven**, **integration-proven**, **externally-live** (real named provider/environment), or **production-exercised** (current monitoring plus recovery/incident exercise). Evidence states are ordered only by the kinds of claims they can support. Architecture implementation does not imply an evidence state, and strong evidence for a legacy path does not imply architecture conformance.

Architecture controls MAY be implemented independently of evidence maturity, but a public architecture-level claim is admissible only at or above this minimum evidence floor:

| Architecture claim | Minimum admissibility evidence |
|---|---|
| Level 0 | declared |
| Level 1 | unit-proven |
| Level 2 | integration-proven |
| Level 3 | integration-proven |
| Level 4 | integration-proven |
| Level 5 | integration-proven |
| Level 6 | production-exercised |

The floor is a claim gate, not an architecture requirement. Evidence above the floor—including `externally-live` or `production-exercised` evidence for a lower-level architecture—SHALL NOT raise the architecture level. Conversely, implemented controls without the floor SHALL be reported as an unadmitted implementation result with their actual evidence state, not as the level claim. Evidence freshness policy SHALL define expiry by evidence type; stale evidence downgrades evidence state and may make a claim inadmissible, but does not erase implemented controls. Public labels SHALL show both axes, for example `Architecture L4 / integration-proven`, plus scope and limitations.

## 12. Non-goals

This specification does not require:

- continuous unconstrained cognition; scheduled and per-turn Living Self operation remains valid;
- autonomous mutation of constitutional identity or core source code;
- autonomous external action without explicit policy and approval;
- a belief judge or expensive cognition on every chat turn;
- replacement of Markdown as the user-owned memory and skill medium;
- replacement of SQLite where a local transactional backend satisfies the contract;
- a distributed scheduler before product demand and fault evidence justify one;
- identical feature support across channels; explicit capability negotiation is preferred;
- one physical monolith or one deployment process; “single service” means one domain authority;
- copying Hermes module layout, singleton registries, or process-global context;
- storing chain-of-thought; private reasoning remains ephemeral and excluded from transcripts;
- treating all historical data as if provenance exists when it does not;
- preserving undocumented bypass behavior during migration;
- more headline features before trust, coherence, durability, and proof are closed.

## 13. Definition of polished

For a named released distribution and declared support matrix, TaskChad OS is **architecturally polished** only when all applicable statements below are true and machine-traceable. Evaluation SHALL use the exact applicability and scoring procedure in §11: expand `polish-architecture-v1`, bind the applicability-manifest digest, evaluate every retained `POL-*` ID and cumulative level-control bullet, calculate the greatest passing level, and apply the evidence-admissibility floor. The Proof Manifest SHALL contain those inputs and per-item results so another evaluator given the same source revision, applicability manifest, and evidence references obtains the same label. An absent or `unknown` result is not a pass. “Polished” SHALL NOT be asserted for the entire repository, every optional integration, or an unlisted environment based on one distribution's result. UX quality or subjective aesthetics may be described separately and are not certified by this definition.

1. Every supported invokable capability has a typed schema, current availability, explicit side-effect class, policy and audit owner, and shared invocation path.
2. Every supported turn and job identifies tenant, persona, actor, session/manifest, and applicable authority without relying on ambient global profile state.
3. Every supported persona surface uses one turn service and demonstrates isolation with negative tests.
4. Every enabled autonomous durable mutation is evidence-bound, default-denied, atomic, audit-fail-closed, visible, and conflict-safe to reverse or explicitly irreversible with the required recovery path.
5. Every supported durable job survives restart with safe recovery; work declared ephemeral before execution makes no durability claim.
6. Every verified-completion claim is backed by persisted verifier evidence; typed unverifiable outcomes, execution, and delivery remain distinct.
7. Every supported canonical session history is append-only subject to governed retention/deletion; compaction is a traceable derived artifact.
8. Every enabled memory/index mutation is complete or observably repairable, and every supported recall path enforces provenance, sensitivity, and persona scope.
9. Every supported active executable skill has provenance, trust, version, required capabilities, usage history, audit, and rollback/deprecation state.
10. Every effective behavioral configuration value can be validated, migrated, explained, and safely backed up without exposing secrets.
11. Every published health claim identifies whether it is structural or behavioral and is rendered from one typed probe result on each surface that exposes it.
12. Every supported operator mutation can be previewed, authorized, observed, verified or marked unverifiable, audited, and reversed where promised.
13. Every supported adapter truthfully declares feature support and passes tests for what it declares.
14. Every public architecture/evidence label is generated or bounded by a current Proof Manifest, with scope and limitations visible.
15. Identity-first behavior remains perceptible: the Homie can explain which durable identity and evidence informed an action, while authority and proof remain independently inspectable.
16. The deterministic §11 calculation returns at least **Architecture Level 5**, and its claim is admissible with at least **integration-proven** evidence for the support matrix. A deployment marketed as production-supported returns **Architecture Level 6**, and that claim is admissible with current **production-exercised** evidence. Higher evidence maturity does not compensate for a failed architecture control or increase either result.

“Polished” therefore means not merely coherent in a demo, but trustworthy across interruption, disagreement, partial failure, migration, multiple personas, multiple surfaces, and operator scrutiny.

## 14. Required traceability and governance

Each implementation epic SHALL map changes and tests to `POL-*` IDs. A requirement exception SHALL record scope, rationale, risk, compensating control, owner, and expiry. Architecture decisions MAY refine physical module names but SHALL NOT silently weaken immutable invariants.

The following current paths are primary migration anchors:

| Target concern | Current anchors |
|---|---|
| Identity/working-memory assembly | `.claude/chat/cognition/identity_payload.py`, `working_memory.py`, `regions.py`, `steps.py` |
| Persona turns/lifecycle | `.claude/chat/engine.py`, `discord_persona_runtime.py`, `web_persona_runtime.py`, `.claude/scripts/personas/` |
| Recall and memory | `.claude/chat/recall_service.py`, `.claude/chat/cognition/recall.py`, `.claude/scripts/memory_index.py`, `.claude/scripts/db.py` |
| Amendments/evolution | `.claude/chat/cognition/amendments.py`, `.claude/scripts/evolve/` |
| Skills | `.claude/chat/cognition/skills.py`, `skill_promotion.py`, `skill_guard.py`, `.claude/chat/skill_audit.py` |
| Orchestration | `.claude/scripts/orchestration/db.py`, `convoy_service.py`, `mailbox_service.py`, `team_service.py`, `operating_room.py` |
| Configuration | `.claude/scripts/config.py` |
| Operator documentation | `docs/manual/README.md`, `docs/the-living-self-manual.md`, `docs/manual/features/` |

Conformance evidence SHALL include source revision, environment, command/test identity, result, and artifact digest. Reviews SHOULD reject changes that create a second domain authority even when they locally pass tests.

---

## 15. Initial implementation order

The normative destination does not require a flag-day rewrite. The preferred dependency order is:

1. close amendment rollback, evidence binding, and fail-closed audit;
2. establish records, Request Context, and generated proof levels;
3. centralize capability and policy authority;
4. introduce immutable manifests, Session Events, and PersonaTurnService;
5. migrate typed configuration and structured diagnostics;
6. unify durable jobs, Operating Room timeline, and verification;
7. journal memory/index updates and complete provenance;
8. move skills to repository/curator lifecycle;
9. enforce adapter/plugin contracts and attain production support evidence.

New features SHOULD enter through the target contracts even while legacy features are being migrated. Work that increases surface area without reducing fragmented authority SHOULD be deferred unless needed to close a safety or compatibility gap.
