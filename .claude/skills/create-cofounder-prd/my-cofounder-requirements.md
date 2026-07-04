# My Co-founder Projects - Requirements Template

> Fill this out before running `/create-cofounder-prd <path to this file>`. Your answers
> drive the phased build plan: which scheduler triggers it, which engine runs the builds,
> which chat platform (if any) you steer it from, and how autonomous it is.

The system becomes the orchestrator of the larger, multi-session tasks you hand it: you drop
a spec, it breaks the work into isolated workflows, dispatches and monitors them, runs an
executable completion check, and pings you only when it needs you or the work is done. The
Build mode (below) sets whether it reuses an existing agent or stands alone; three adapter
choices (Sections 3, 4, 5) make it fit your stack. Sections marked **[required]** apply to
everyone.

---

## Build mode [required]

Are you adding this to something that already exists, or building it standalone?

- [ ] **Extend an existing second brain / agent** - you already have a proactive loop, a
  memory store, a chat integration, and shared utilities. The plan REUSES them (add a pass,
  reuse the vault, reuse the chat client + utilities) - a lighter build.
- [ ] **Standalone from scratch** - no existing agent to build on. The plan BUILDS the minimal
  pieces it needs (a small scheduler, a state folder, a notify path, the few utilities) so the
  system stands on its own.

Your three adapter choices (Sections 3-5) apply either way; Build mode just tells the plan
whether each one REUSES something you have or BUILDS a minimal version. The core is identical.

---

## Agent integration / discoverability [required]

How does your **interactive** agent (the one you chat with / open a coding session with) learn
what it knows at the start of a conversation? This is how the build plan makes the co-founder
system discoverable, so a fresh conversation knows the system exists and how to steer a project
(otherwise it is built but unusable - a fresh agent has no idea how to update a project doc).

- **How my agent loads always-on context** (pick all that apply):
  - [ ] A `CLAUDE.md` / project rules file
  - [ ] A system prompt I control
  - [ ] A SessionStart-style hook / a chat-engine context builder
  - [ ] A memory index it reads each session
  - [ ] None yet - build me a minimal one
- **Do I have more than one entry point** (e.g., a coding agent AND a chat bot)? If so, do they
  share one context builder? ___

---

## 1. About You [required]

- **Name:** ___
- **What I want it to build** (1-2 sentences - the kind of projects you'll hand it): ___
- **Timezone:** ___

---

## 2. Targets [required]

- **Repos it may build in** (names/URLs, or "any of mine"): ___
- **Greenfield allowed?** (can it create brand-new repos for new projects) [ ] Yes [ ] No
  - If yes, owner/org + visibility for new repos: ___
- **Anything it must NOT touch:** ___

---

## 3. Scheduler / trigger [required]

How does the orchestrator wake up to run a pass? Pick one:

- [ ] **I have an existing proactive loop / heartbeat** - add a pass to it. Describe it
  briefly (language, how it's scheduled): ___
- [ ] **A standalone scheduler** - I'll run it on a timer (cron / Task Scheduler / launchd /
  systemd). 
- [ ] **None yet** - build me a minimal scheduled runner as part of the plan.

- **Cadence** (how often a pass runs, e.g., every 30 min): ___
- **Run around the clock (24/7) or only certain hours?** ___

---

## 4. Build engine [required]

What actually runs the build workflows (the muscle)? Pick one:

- [ ] **Archon** (workflow engine with worktree isolation + a run-state DB)
- [ ] **Claude Code `/loop`** (a headless, detached loop session per project)
- [ ] **CI (GitHub Actions)** (workflow_dispatch + the runs API)
- [ ] **Custom / other:** ___

Confirm for your engine (the orchestrator needs all three):
- **Worktree / workspace isolation per build?** [ ] Yes [ ] No / unsure
- **How do I read a run's status by id?** (DB, API, status file): ___
- **How do I dispatch DETACHED** so the build survives the orchestration pass exiting? ___
  (A plain background child gets killed when the pass ends - this is the most common failure.)

---

## 5. Chat / notification platform [required]

Where do you get pinged and steer projects? Pick one:

- [ ] **Slack**  [ ] **Discord**  [ ] **Teams**  [ ] **Email**  [ ] **Webhook**
- [ ] **None (markdown-only)** - I'll read the Activity Log and edit the project file directly

If you picked a platform:
- **One thread per project?** [ ] Yes [ ] No
- **Should it ingest my replies** (so I can steer a project by replying)? [ ] Yes [ ] No
- **Channel/space to use:** ___

---

## 6. State substrate [required]

Where do the project markdown files + state live?

- [ ] **Obsidian vault**  [ ] **A git repo**  [ ] **A plain folder**
- **Synced across machines?** (how, e.g., git-sync, Obsidian Sync, none): ___

---

## 7. Autonomy + merge policy [required]

- **How bold should it be?**
  - [ ] **Approval-gated** - park for my review before each dispatch and before merging
  - [ ] **Full auto-advance** - no gates; drive new -> done on its own
- **Ping me on which states?** [ ] done [ ] blocked [ ] awaiting-human [ ] every dispatch
- **Merge policy:**
  - [ ] Pre-existing repos get a **PR left for my review** (never auto-merge)
  - [ ] System-owned / greenfield repos may **commit straight to main**
  - [ ] Full auto-merge on green CI everywhere

---

## 8. Caps [required]

Guardrails so it never runs away (tripping any parks the project for you):

- **Max iterations per project:** ___ (e.g., 100)
- **Max wall-clock hours per project:** ___ (e.g., 72)
- **Max concurrent builds across all projects:** ___ (e.g., 3)
- **Spend cap?** [ ] Yes, $___/project [ ] No (time-dominant caps only)

---

## 9. Completion + provider [required]

- **Completion check style** (the executable signal of "done", run in the build workspace -
  e.g., `uv run pytest`, `bun run ci`, `npm test && npm run build`): ___
- **Any subjective-quality domains?** (games, design, prose - where "tests pass" is not
  "good") [ ] Yes -> add a human-verdict gate after the check is green [ ] No
- **Provider/model backend** the orchestrator and builds should use (e.g., Claude / Pi /
  Codex / OpenRouter): ___
- **Per-project model override needed?** [ ] Yes [ ] No
- **Force a single model for the first runs?** (e.g., Opus everywhere while testing): ___

---

## 10. Infrastructure [required]

- **Operating System where it runs:** [ ] Windows [ ] macOS [ ] Linux
- **Runs on:** [ ] My machine [ ] A VPS / server [ ] Both
- **Existing tools I already have:** ___
  (e.g., "Archon installed", "a heartbeat already runs", "gh authenticated", "uv installed")

---

## 11. Anything else [optional]

Constraints, preferences, hard rules (e.g., "no em dashes in any output"), or context the
plan should respect: ___

---

> After filling this out, run: `/create-cofounder-prd <path to this file>`
