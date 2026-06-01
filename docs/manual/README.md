# The Homie Manual

This is the canonical feature manual for The Homie.

Use this when you need to understand what has shipped, where the source of
truth lives, how to operate a feature, how to test it, and which proof/handoff
documents back the current state.

`docs/manual/` is public-framework documentation. Keep it portable and
sanitizer-safe; private handoffs, PRPs, vault notes, account details, and
machine-specific proof artifacts stay outside the public manual.

## Table Of Contents

### Start Here

- [Feature Page Template](feature-template.md)
- [Manual Maintenance Rules](#manual-maintenance-rules)
- [Feature Coverage Map](#feature-coverage-map)

### Active Feature Manuals

| Feature | Status | Manual Page | Primary Operator Surface |
|---|---|---|---|
| Homie Dashboard Framework | Canonical operator shell | [homie-dashboard-framework](features/homie-dashboard-framework.md) | `dashboard/`, `/mission`, `/teams`, `/browser`, `/mobile` |
| Runtime Status And Model Control | Active baseline | [runtime-status-model-control](features/runtime-status-model-control.md) | `/provider`, `/model`, status/doctor |
| Persona Lifecycle And Files | Active baseline | [persona-lifecycle-files](features/persona-lifecycle-files.md) | `/agents`, `/agents/:id/files` |
| Convoy, Work Queue, And Mailbox | Active baseline | [convoy-work-mailbox](features/convoy-work-mailbox.md) | `/convoy`, `/work`, mailbox APIs |
| Team Operations And Executor | Active baseline | [team-operations-executor](features/team-operations-executor.md) | `/teams`, team APIs |
| Dashboard Mobile Access | Shipped, live-proven | [dashboard-mobile-access](features/dashboard-mobile-access.md) | `/mobile` |
| Team Room | V3 shipped, live-proven | [team-room](features/team-room.md) | `/teamroom`, `/teams` |
| TaskChad Team Drill | Runtime mode shipped, live-proven | [taskchad-team-drill](features/taskchad-team-drill.md) | `/taskchaddrill`, `/teams` |
| Autonomous Team Scheduler | Shipped, Telegram-proven | [autonomous-team-scheduler](features/autonomous-team-scheduler.md) | `/teamtick`, `/teams` |
| BrowserOps + Browser Viewer | Shipped, live-proven | [browserops-browser-viewer](features/browserops-browser-viewer.md) | `/browserops`, `/browser` |
| Cabinet Rooms | Shipped baseline, manual exists | [cabinet-rooms](features/cabinet-rooms.md) | `/cabinet`, `/standup`, `/discuss`, `/cabinet` dashboard |
| Jarvis Cognitive Loop | Shipped/live-runtime proven | [jarvis-cognitive-loop](features/jarvis-cognitive-loop.md) | `/jarvis`, status/doctor, scheduled loops |
| Direct Integration Capability Contract | Shipped, policy-enforced | [direct-integration-capability-contract](features/direct-integration-capability-contract.md) | direct integration wrapper, `/send`, status/doctor |
| Memory, Hive, And Chat Observer | Active baseline | [memory-hive-chat-observer](features/memory-hive-chat-observer.md) | `/memories`, `/hive`, `/chat` |
| Scheduled Jobs, Settings, And Audit | Active baseline | [scheduled-settings-audit](features/scheduled-settings-audit.md) | `/scheduled`, `/settings`, `/audit` |

### Existing Deep Public Manuals

| Document | Use |
|---|---|
| [BrowserOps Agent Browser Manual](../browserops-agent-browser-manual.md) | Deep BrowserOps operating contract, safety policy, validation, and failure modes. |
| [Cabinet Room Manual](../cabinet-room-manual.md) | Deep Cabinet room-state, dashboard, and participant-control context. |

Private proof handoffs, tracker entries, and migration notes stay out of the
public framework export. Feature pages summarize the latest proof without
requiring those private documents.

## Manual Maintenance Rules

When a feature ships or materially changes:

1. Update or create the matching page under `docs/manual/features/`.
2. Add it to the Active Feature Manuals table if it is new.
3. Keep `AGENTS.md` slim; add only a pointer or invariant that all agents need.
4. Keep `PRPs/active/TRACKER.md` focused on current state and next work, not a
   full feature manual.
5. Link proof handoffs from the feature page instead of copying whole handoffs.
6. Do not paste secrets, account IDs, token values, cookies, raw browser state,
   private phone/email details, or unredacted local env values.
7. If a feature is public-framework safe, say whether it has been exported.
   Public export still goes through `scripts/sanitize.py`; never manually copy.

## Feature Coverage Map

This first manual pass intentionally seeds the highest-churn Homie features.
Remaining features should be folded in during follow-up passes:

- Voice/Cabinet voice
- Multi-channel adapters and Mission Control relay surfaces

Use the template, tracker, handoffs, recent commits, and targeted vault context
when adding those pages.
