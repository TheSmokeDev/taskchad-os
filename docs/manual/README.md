# The Homie Manual

This is the canonical feature manual for The Homie.

Use this when you need to understand what has shipped, where the source of
truth lives, how to operate a feature, how to test it, and which proof/handoff
documents back the current state.

`docs/manual/` is public-framework documentation. Keep it portable and
sanitizer-safe; private handoffs, PRPs, vault notes, account details, and
machine-specific proof artifacts stay outside the public manual.

## Open Source Reader Path

1. Start with the root [README](../../README.md) for Quick Install, Getting
   Started commands, the documentation map, and proof boundaries.
2. Use [INSTALL](../../INSTALL.md) for setup and channel credentials.
3. Read [Operating Room](features/operating-room.md),
   [Capability Gateway](features/capability-gateway.md), and
   [Desktop v0](features/desktop-v0.md) to understand the current dashboard
   and desktop operator loop.
4. Read [Multi-Channel Adapters](features/multi-channel-adapters.md) for
   Telegram attachments, attachment groups, quick-turn batching, and
   Queue/Steer follow-up controls.
5. Read [Runtime Status And Model Control](features/runtime-status-model-control.md)
   before changing provider, lane, or quiet JSON behavior.
6. Read [Heartbeat Runtime](features/heartbeat-runtime.md) before changing
   proactive background reasoning, heartbeat model overrides, or scheduler
   behavior.

## Ecosystem Positioning

The Homie sits in the same public agent ecosystem as OpenClaw, Hermes Agent,
OpenSouls, and ClaudeClaw. OpenClaw proved broad agent/channel access; Hermes
pushed self-improving loops and desktop/operator ergonomics; OpenSouls
influenced the mental-model vocabulary around AI souls, working memory, and
mental processes; ClaudeClaw inspired dashboard/operator experience. The Homie
is independent and identity-first: durable memory, judgment, Operating Room
orchestration, and thin channel/desktop surfaces over one runtime. Use
[NOTICE](../../NOTICE.md) and [AUTHORS](../../AUTHORS.md) for attribution; use
[CONTRIBUTING](../../CONTRIBUTING.md) for community participation.

## Table Of Contents

### Start Here

- [Open Source Reader Path](#open-source-reader-path)
- [Ecosystem Positioning](#ecosystem-positioning)
- [Feature Page Template](feature-template.md)
- [Manual Maintenance Rules](#manual-maintenance-rules)
- [Feature Coverage Map](#feature-coverage-map)

### Active Feature Manuals

| Feature | Status | Manual Page | Primary Operator Surface |
|---|---|---|---|
| Homie Dashboard Framework | Canonical operator shell | [homie-dashboard-framework](features/homie-dashboard-framework.md) | `dashboard/`, `/mission`, `/teams`, `/browser`, `/mobile` |
| Operating Room | Product slice implemented | [operating-room](features/operating-room.md) | `/teams`, `/api/team/operating-room/run` |
| Capability Gateway | Read-only v1 implemented | [capability-gateway](features/capability-gateway.md) | `/capabilities`, `/api/capabilities/status` |
| Desktop v0 | Dashboard-first Electron app + unpacked and portable artifacts | [desktop-v0](features/desktop-v0.md) | `thehomie desktop --shell`, `dashboard/desktop` |
| Desktop Dev Launcher | Windows-first dev launcher | [desktop-dev-launcher](features/desktop-dev-launcher.md) | `thehomie desktop` |
| Runtime Status And Model Control | Active baseline | [runtime-status-model-control](features/runtime-status-model-control.md) | `/provider`, `/model`, status/doctor |
| Bot Self-Restart | Active baseline, live-proven | [bot-self-restart](features/bot-self-restart.md) | `/restart` |
| Live Lane Safety Contract | Active baseline | [live-lane-safety](features/live-lane-safety.md) | `live-safety proof`, status/doctor, orchestration live APIs |
| Persona Lifecycle And Files | Active baseline | [persona-lifecycle-files](features/persona-lifecycle-files.md) | `/agents`, `/agents/:id/files` |
| Convoy, Work Queue, And Mailbox | Active baseline | [convoy-work-mailbox](features/convoy-work-mailbox.md) | `/convoy`, `/work`, mailbox APIs |
| Team Operations And Executor | Active baseline | [team-operations-executor](features/team-operations-executor.md) | `/teams`, team APIs |
| Tenant Isolation v0 | Phase A+B shipped, enforcement default-OFF | [tenant-isolation-v0](features/tenant-isolation-v0.md) | `thehomie tenant`, `HOMIE_TENANT_ENFORCEMENT`, orchestration/dashboard API |
| Archon Repo Dispatch | Public-safe pattern and templates | [archon-repo-dispatch](features/archon-repo-dispatch.md) | `thehomie profile init-archon`, `thehomie archon ...`, `templates/repository-dispatch/` |
| Dashboard Mobile Access | Shipped, live-proven | [dashboard-mobile-access](features/dashboard-mobile-access.md) | `/mobile` |
| Homie Mobile App | Shipped (v1) | [homie-mobile-app](features/homie-mobile-app.md) | Capacitor iOS/Android over the dashboard |
| Team Room | V3 shipped, live-proven | [team-room](features/team-room.md) | `/teamroom`, `/teams` |
| Autonomous Team Scheduler | Shipped, Telegram-proven | [autonomous-team-scheduler](features/autonomous-team-scheduler.md) | `/teamtick`, `/teams` |
| BrowserOps + Browser Viewer | Shipped, live-proven | [browserops-browser-viewer](features/browserops-browser-viewer.md) | `/browserops`, `/browser` |
| Social-Write Executor | Shipped, default-denied, operator-gated per action | [social-write-executor](features/social-write-executor.md) | `/linkedin_post`, `/linkedin_connect`, `/reddit comment\|post` |
| Video Generation | Shipped, native command, model-agnostic | [video-generation](features/video-generation.md) | `/video`, `video_pipeline.py`, `video_styles.py` |
| Document Uploads And Ingest | Shipped, all 3 phases (truthfulness, full reads, /vault-ingest) | [document-uploads-and-ingest](features/document-uploads-and-ingest.md) | `attachment_context.py`, `router.py` `/vault-ingest` caption, Telegram + Discord |
| Telegram Command Menu | Curated native menu | [telegram-command-menu](features/telegram-command-menu.md) | `/commands`, Telegram slash menu |
| Multi-Channel Adapters | Active baseline, Telegram docs + turn controls proven | [multi-channel-adapters](features/multi-channel-adapters.md) | Telegram, Slack, Discord, WhatsApp, web, CLI |
| Cabinet Rooms | Shipped baseline, manual exists | [cabinet-rooms](features/cabinet-rooms.md) | `/cabinet`, `/standup`, `/discuss`, `/cabinet` dashboard |
| Cabinet Voice | Single-session lifecycle controls shipped | [cabinet-voice](features/cabinet-voice.md) | `/cabinet voice`, `/voices`, `/api/cabinet/voice/*` |
| Cognitive Loop | Shipped/live-runtime proven; dashboard route hidden from public nav | [jarvis-cognitive-loop](features/jarvis-cognitive-loop.md) | status/doctor, scheduled loops |
| Heartbeat Runtime | Active baseline, runtime contract corrected | [heartbeat-runtime](features/heartbeat-runtime.md) | `heartbeat.py`, `HEARTBEAT.md`, scheduled loop |
| Direct Integration Capability Contract | Shipped, policy-enforced | [direct-integration-capability-contract](features/direct-integration-capability-contract.md) | direct integration wrapper, `/send`, status/doctor |
| Memory, Knowledge Graph, And Dashboard Chat | Active baseline, dashboard chat reliability proven | [memory-hive-chat-observer](features/memory-hive-chat-observer.md) | `/memories`, `/hive`, `/chat` |
| Scheduled Jobs, Settings, And Audit | Active baseline | [scheduled-settings-audit](features/scheduled-settings-audit.md) | `/scheduled`, `/settings`, `/audit` |
| Native Design (`/design`) | Phase 1 + B1 shipped, live-proven | [design-capability](features/design-capability.md) | `/design`, `/design system <slug>` |
| Business Signal Engine | Shipped (#79), merged | [business-signal-engine](features/business-signal-engine.md) | `/signal`, `/signal refresh`, daily/weekly cadence |
| Social Post Pipeline | Shipped (#80), default-denied, operator-gated | [social-post-pipeline](features/social-post-pipeline.md) | `/social` (draft/approve/post/schedule/cadence) |
| Skill-From-Experience Loop | Shipped, default-denied, operator-gated | [skill-from-experience-loop](features/skill-from-experience-loop.md) | `/skills` (review/promote/reject) |
| Social Cadence Draft Delivery | Shipped, default-denied, operator-gated per tap | [social-cadence-draft-delivery](features/social-cadence-draft-delivery.md) | Telegram draft cards (Approve/Edit/Reject), `/social` |
| Intent-PRD and Clutch Review | Shipped (#78), merged | [intent-prd-and-clutch](features/intent-prd-and-clutch.md) | `create-prd`, `archon workflow run archon-clutch` |
| Context-Economy DX | Shipped (#66), merged | [context-economy-dx](features/context-economy-dx.md) | `/prime-*`, `brownfield-day-1`, `vertical-slice-audit` |
| Repositories System | Shipped (#63), merged | [repositories-system](features/repositories-system.md) | `thehomie repositories status\|validate` |
| Archon Workflows | Active baseline, autonomous pipeline live-proven | [archon-workflows](features/archon-workflows.md) | `archon workflow list\|run\|status` |

### Existing Deep Public Manuals

| Document | Use |
|---|---|
| [The Living Self Manual](../the-living-self-manual.md) | The cognitive system end to end — sense, form beliefs, hold against conflict, think before speaking, earn convictions. Operator runbook + architecture + knobs + verification. Ties together Heartbeat Runtime, Episodes, and Session Opening Brief. |
| [BrowserOps Agent Browser Manual](../browserops-agent-browser-manual.md) | Deep BrowserOps operating contract, safety policy, validation, and failure modes. |
| [Social-Write Executor Manual](../social-write-executor-manual.md) | Deep operating contract for operator-approved LinkedIn and Reddit writes: the isolated-approval gate, the executor/driver split, audit policy, platform notes, and validation. |
| [Cabinet Room Manual](../cabinet-room-manual.md) | Deep Cabinet room-state, dashboard, and participant-control context. |

Private proof handoffs, tracker entries, and migration notes stay out of the
public framework export. Feature pages summarize the latest proof without
requiring those private documents.

## Public Proof Boundaries

- Desktop v0 proves the dashboard-first Electron app plus unpacked and
  portable no-admin Windows artifacts. A signed installer is not claimed yet.
- Fresh public Windows install smoke has proven install, setup check, real CLI
  chat, Desktop launch, route checks, and clean shutdown from a clean clone.
- Cabinet Voice has lifecycle controls and a partial LiveKit spike. The
  browser mic -> transcript -> Cabinet reply path remains deferred.
- Local proof reports, private handoffs, account-specific setup, and generated
  runtime artifacts stay private even when the feature manual is public.

## Manual Maintenance Rules

When a feature ships or materially changes:

1. Update or create the matching page under `docs/manual/features/`.
2. Add it to the Active Feature Manuals table if it is new.
3. Keep private agent instructions slim; add only pointers or invariants that
   all agents need.
4. Keep the internal tracker focused on current state and next work, not a
   full feature manual.
5. Link proof handoffs from the feature page instead of copying whole handoffs.
6. Do not paste secrets, account IDs, token values, cookies, raw browser state,
   private phone/email details, or unredacted local env values.
7. If a feature is public-framework safe, say whether it has been exported.
   Public export still goes through `scripts/sanitize.py`; never manually copy.

## Feature Coverage Map

This first manual pass intentionally seeds the highest-churn Homie features.
Remaining features should be folded in during follow-up passes:

- Mission Control relay surfaces

Use the template, tracker, handoffs, recent commits, and targeted vault context
when adding those pages.
