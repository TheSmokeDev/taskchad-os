# My Co-founder Projects - Requirements Template (worked example: Cole's setup)

> This is a filled-in example. It is the real TheHomie configuration (an existing Second
> Brain heartbeat + Archon + Slack) that the system was first built and proven on. Use it as
> a reference for how to answer; your own answers will differ.

---

## Build mode [required]

- [X] **Extend an existing second brain / agent** - there is already a The Homie heartbeat,
  an Obsidian vault, a Slack integration, and shared utilities (file-lock, state I/O, daily
  log, the Agent SDK query pattern). The plan reuses all of them.
- [ ] Standalone from scratch

---

## Agent integration / discoverability [required]

- **How my agent loads always-on context:**
  - [X] A `CLAUDE.md` (read by both Claude Code sessions and the Slack chat daemon via the
    `claude_code` preset + project setting source)
  - [X] A SessionStart-style hook / chat-engine context builder (a shared `session_context.py`
    `build_context()` feeds BOTH the Claude Code SessionStart hook AND the Slack chat engine)
  - [X] A memory index it reads each session (`MEMORY.md`, `REPOSITORIES.md`, and now
    `COFOUNDER-PROJECTS.md`)
- **More than one entry point?** Yes - a local Claude Code session and a VPS Slack chat daemon.
  They SHARE one context builder (`session_context.build_context`), so the co-founder index
  reaches both. (Reuse the same builder for every surface, or one of them ends up blind.)

---

## 1. About You [required]

- **Name:** Cole Medin
- **What I want it to build:** Full software projects from a markdown spec - games, apps,
  internal tools - carried across many sessions without me babysitting each step.
- **Timezone:** CST

---

## 2. Targets [required]

- **Repos it may build in:** Any repo I own (coleam00/*).
- **Greenfield allowed?** [X] Yes - owner `coleam00`, **private** for new repos.
- **Anything it must NOT touch:** No destructive git in a live checkout; never delete repos.

---

## 3. Scheduler / trigger [required]

- [X] **I have an existing proactive loop / heartbeat** - the The Homie heartbeat (Python,
  Claude Agent SDK), scheduled by cron `*/30 * * * *` on a VPS. Add a pass to it.
- **Cadence:** every 30 minutes.
- **Run around the clock?** Yes, 24/7 (the build advances overnight; the alert work self-gates
  to active hours, the co-founder pass does not).

---

## 4. Build engine [required]

- [X] **Archon** (worktree isolation + a run-state DB).
- **Worktree isolation per build?** [X] Yes (`--branch` creates an isolated worktree).
- **How do I read a run's status by id?** SQLite at `~/.archon/archon.db`
  (`remote_agent_workflow_runs`: status, working_path, completed_at).
- **How do I dispatch DETACHED?** `setsid nohup bash -lc 'IS_SANDBOX=1 archon workflow run
  <name> --branch <b> --cwd <repo> "<msg>"' > /tmp/archon-dispatch.log 2>&1 & disown`
  (a plain background child died when the pass exited - this is the fix).

---

## 5. Chat / notification platform [required]

- [X] **Slack**
- **One thread per project?** [X] Yes
- **Ingest my replies?** [X] Yes (reply in the thread to steer a project)
- **Channel/space:** `#cofounder-projects` (using `#thehomie` until the dedicated channel
  exists)

---

## 6. State substrate [required]

- [X] **Obsidian vault** - projects live in `TheHomie/Co-founder Projects/` (one markdown file
  each).
- **Synced across machines?** Yes, via git-sync (2-minute timer, both machines).

---

## 7. Autonomy + merge policy [required]

- [X] **Full auto-advance** - no gates; drive new -> done on its own.
- **Ping me on:** [X] done [X] blocked [X] awaiting-human (NOT every dispatch - routine
  progress goes to the Activity Log only).
- **Merge policy:**
  - [X] Pre-existing repos get a **PR left for review** (never auto-merge)
  - [X] Greenfield / system-owned repos may **commit straight to main**

---

## 8. Caps [required]

- **Max iterations per project:** 100
- **Max wall-clock hours per project:** 72
- **Max concurrent builds:** 3
- **Spend cap?** [X] No (time-dominant caps only)

---

## 9. Completion + provider [required]

- **Completion check style:** per-project executable, e.g., `bun run ci` (games) or
  `uv run pytest` (Python) - run in the Archon build worktree.
- **Subjective-quality domains?** [X] Yes (games) - the executable check is a floor; the loop
  parks at awaiting-human for my fun/quality verdict rather than auto-marking done.
- **Provider/model backend:** Claude, derived from `SB_AGENT_BACKEND` (so flipping the brain
  to Pi later moves the builds too).
- **Per-project model override?** [X] Yes (`archon_provider` / `archon_model_tier` frontmatter).
- **Force a single model for first runs?** Opus everywhere (`COFOUNDER_ARCHON_FORCE_MODEL=opus`)
  while riding the Claude subscription.

---

## 10. Infrastructure [required]

- **OS where it runs:** [X] Linux (VPS) - developed on Windows.
- **Runs on:** [X] A VPS (the cron heartbeat host).
- **Existing tools:** Archon installed (`/root/.bun/bin/archon`), the heartbeat already runs,
  `gh` authenticated as coleam00, `uv` installed.

---

## 11. Anything else [optional]

Hard rule: no em dashes anywhere in output (code, comments, UI strings, docs, commits). Use a
hyphen, a comma, or rewrite.

---

> After filling this out, run: `/create-cofounder-prd <path to this file>`
