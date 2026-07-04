---
name: create-cofounder-prd
description: Generate a personalized PRD for an autonomous Co-founder Projects system from a completed requirements template. Adapts to any scheduler (existing loop, cron, or build-one-if-missing), any build engine (Archon, Claude Code loop, CI, custom), and any chat platform (Slack, Discord, Teams, email, webhook, or markdown-only). Use when the user has filled out my-cofounder-requirements.md and wants their build plan. Triggers on "create my cofounder PRD", "generate my cofounder projects PRD", "/create-cofounder-prd", or after completing the requirements template.
argument-hint: <path-to-requirements> [output-path]
---

# Autonomous Co-founder Projects PRD Generator

Generate a personalized Product Requirements Document for building an autonomous
"co-founder" projects system: a layer that becomes the **orchestrator of the larger,
multi-session tasks you hand it**. You drop a spec; it breaks the work into workflows,
dispatches and monitors them in isolation, and steers toward done, with a human steering
through chat and markdown edits.

The PRD works two ways, set by the **Build mode** answer in the template:
- **Extend an existing second brain / agent** - reuse its loop, memory, chat, and utilities;
  the phases say "add a pass" and "reuse X" (a lighter build).
- **Standalone from scratch** - no existing agent; the phases BUILD the minimal pieces (a
  small scheduler, a state folder, a notify path, the few utilities) so it stands alone.

The core is identical in both modes. It adapts to the user's stack through three adapter
choices, so the same core fits any environment:

1. **Scheduler / trigger** - reuse an existing proactive loop (a heartbeat), run a
   standalone cron, or (if they have neither) the PRD includes a phase to build a minimal one.
2. **Build engine** - Archon, a Claude Code `/loop` session, CI (GitHub Actions), or a
   custom runner. The orchestrator only needs dispatch + poll + completion-env.
3. **Chat platform** - Slack, Discord, Teams, email, a webhook, or none (markdown-only).

A blank template is bundled at `${CLAUDE_SKILL_DIR}/my-cofounder-requirements.md`. A worked
example (Cole's real Second-Brain-plus-Archon-plus-Slack setup) is bundled at
`${CLAUDE_SKILL_DIR}/example-my-cofounder-requirements.md`.

## Parameters

- **`$0`** (required) - Path to the filled-out requirements file (e.g., `./my-cofounder-requirements.md`)
- **`$1`** (optional) - Output path for the PRD. Defaults to `.agents/plans/cofounder-projects-prd.md`

## Workflow

1. **Read the requirements** - Read the file at `$0`. If no argument was provided, ask for
   the path. If they have not filled one out, point them at the blank template at
   `${CLAUDE_SKILL_DIR}/my-cofounder-requirements.md` and the worked example.

2. **Determine the build mode + adapter mix.** First read the **Build mode** answer (extend
   an existing second brain vs standalone from scratch) - it sets whether every phase REUSES
   an existing piece or BUILDS a minimal one. Then the three adapter answers drive which
   phases appear and how they are specialized:
   - **Scheduler** (Section 3): `existing-loop` | `standalone-cron` | `none` (build one)
   - **Engine** (Section 4): `archon` | `claude-code-loop` | `ci` | `custom`
   - **Chat** (Section 5): a named platform, or `none` (markdown-only)

   Use the **Phase Map** below to decide which phases to include and how to specialize them.

3. **Load the architecture reference** - Read
   `${CLAUDE_SKILL_DIR}/references/architecture-reference.md` for the blueprint: the Ralph
   pattern, the three-section project model, the status state machine, the three adapter
   interfaces (Scheduler/Engine/Chat), the orchestration pass, and the hard-won lessons that
   are non-negotiable requirements (detached dispatch, zombie detection, executable
   completion, the no-double-dispatch gate, caps to awaiting-human).

4. **Research the user's specific stack** - Do not assume familiarity. For each adapter they
   chose, do a quick web check and distill into per-phase implementation notes:

   **Always verify:**
   - **The agent SDK / runtime** they will use for the orchestration pass (e.g., Claude
     Agent SDK: how to start a query, system-prompt presets, allowed tools, streaming,
     credentials). Whatever runtime drives their reasoning pass.
   - **The provider/model knob** they named (Section 9): current model ids + how to set a
     workflow's provider/model so the orchestrator can stamp it.

   **For their chosen ENGINE (Section 4):**
   - **Archon** - `archon workflow run --branch --cwd`, the run-state DB location/schema,
     worktree path convention, how to dispatch DETACHED so the build survives the pass exiting.
   - **Claude Code `/loop`** - how a loop session is launched headless and detached, and how
     to read its status/output.
   - **CI (GitHub Actions)** - `workflow_dispatch` API, the runs API for polling, artifacts.
   - **Custom** - confirm it exposes: dispatch returning an id, a queryable run-state, and a
     way to run the completion check in the build workspace.
   - In every case confirm: worktree/workspace isolation, a queryable run-state, and that the
     dispatch DETACHES (a plain background child dies when the pass exits - this is the single
     most common failure).

   **For their chosen CHAT platform (Section 5), if not markdown-only:**
   - Auth (bot token, app token, webhook url), the send API, the read-replies/thread API, and
     any setup gotchas (e.g., Slack Socket Mode needs an App Token + Bot Token).

   **For their SCHEDULER (Section 3):**
   - `existing-loop` - how to add a pass to it safely (isolated so it cannot break the host).
   - `standalone-cron` / `none` - the OS scheduler for their platform (cron / Task Scheduler /
     launchd / systemd timer), and a minimal runner script shape.

   **For their AGENT INTEGRATION / discoverability (the "Agent integration" template section):**
   - How their interactive agent loads always-on context: a `CLAUDE.md` / project rules file, a
     system prompt, a SessionStart-style hook, a chat-engine context builder, or a memory index.
     The discoverability phase reuses that mechanism (or builds a minimal one) to surface the
     index doc. If they have multiple entry points (a coding agent AND a chat bot), confirm
     whether they share one context builder so the index reaches all of them.

   **The goal:** every phase has enough specificity that a coding agent can implement it
   without guessing. Distill research into per-phase notes; do not dump raw research.

5. **Generate the PRD** - Create a phased build plan at the output path (`$1`, or
   `.agents/plans/cofounder-projects-prd.md` by default) using the Phase Map. Renumber phases
   contiguously in the output (skip, do not stub, phases that do not apply).

6. **Confirm output** - Tell the user where the PRD was saved, summarize their adapter mix in
   one line, and suggest they start with Phase 1.

---

## Phase Map (which phases apply)

| Phase | Always | Condition |
|-------|:------:|-----------|
| 1. Project model + state substrate | ✓ | always (markdown spec schema, frontmatter, state file, discovery/parse) |
| 2. Scheduler / trigger | ✓ | content varies: add a pass to an existing loop, set a cron, OR build a minimal heartbeat if `none` |
| 3. Deterministic core (Python before LLM) | ✓ | poll, caps, attention classifier, the no-double-dispatch gate, status machine |
| 4. Engine adapter | ✓ | specialized to `archon` / `claude-code-loop` / `ci` / `custom`; DETACHED dispatch is mandatory |
| 5. Orchestration pass (the LLM step) | ✓ | the short prompt + decision contract + provider/model stamping |
| 6. Chat adapter | - | only if a chat platform is chosen; if `none`, a short "markdown-only" note instead |
| 7. Completion checks + caps + safety | ✓ | executable completion, subjective-quality-human gate, zombie detection, detached-dispatch hardening |
| 8. Discoverability (agent integration) | ✓ | always - make the interactive agent KNOW the system: an index doc, a reference in always-loaded context, session-start injection. Without it the system is built-but-unusable |
| 9. Deploy + first project | ✓ | wire to the scheduler, env/config, drop a first spec, watch it run |

When a phase does not apply, omit it entirely and renumber contiguously. Mention the
canonical phase name in parentheses if useful for cross-referencing.

---

## PRD Structure

**Header:**
- Project name (personalized: "[User's Name]'s Co-founder Projects")
- Date generated
- Summary: 1-2 sentences with their adapter mix (scheduler / engine / chat) and what they
  want it to build (their target repos / domains)

### Phase: Project model + state substrate [always]
- The watched folder + the three-section project file (Spec static / Plan mutable / Activity
  Log append-only) and the frontmatter schema (status enum, current_job_id, branch, caps,
  completion_check, chat refs). Pull the exact schema from the architecture reference.
- The per-machine JSON state file (reply cursor, fail streak, wall-clock start).
- Discovery/parse: skip `_`-prefixed, README, and the `done/` archive; tolerate malformed
  files (skip with a warning, never crash).
- State substrate from Section 6: an Obsidian vault, a plain git repo, or a folder. If synced
  across machines, note the sync mechanism.
- Estimated complexity: Low

### Phase: Scheduler / trigger [always, content from Section 3]
- **existing-loop:** add the orchestration pass to their loop, wrapped so its failure cannot
  break the host (a try/finally pass is the clean pattern). Run it on every invocation,
  including off-hours, so long work advances overnight.
- **standalone-cron:** a CLI entry point + an OS timer (cron / Task Scheduler / launchd /
  systemd) at their cadence (Section 3).
- **none -> build a minimal heartbeat:** a small scheduled runner (a script the OS timer
  calls) that invokes one orchestration pass. This is the "create one if it does not exist"
  path; keep it tiny - it only needs to call the pass.
- Estimated complexity: Low (reuse) / Medium (build one)

### Phase: Deterministic core (Python before LLM) [always]
- The discipline: everything mechanical happens in code before any model call.
- `poll(current_job_id)`, `caps_tripped` (iterations + wall-clock), the attention classifier
  (`needs_attention`), and the hard gate "never dispatch while a job is running."
- The status state machine transitions (deterministic), with tolerance for a non-enum active
  status (treat anything not in {new, testing, blocked, awaiting-human, done} as building).
- Estimated complexity: Medium

### Phase: Engine adapter [always, specialized by Section 4]
- Implement `dispatch(name, branch, message, workdir) -> run_id` (MUST detach),
  `poll(run_id) -> (status, workdir)`, and `completion_env(workdir)`.
- Engine-specific notes from research (Archon CLI + run-state DB + worktree paths; Claude
  Code loop; CI dispatch + runs API; custom).
- Hard requirement: DETACHED dispatch (new session / nohup / managed daemon / remote runner).
  Call out that a plain background child dies when the pass exits.
- Estimated complexity: Medium-High

### Phase: Orchestration pass (the LLM step) [always]
- The builder writes their OWN short prompt (their voice, their stack) - do not hand them a
  finished one. The PRD must hand them the **Prompt Contract (architecture reference Section
  9)** as the requirement their prompt has to satisfy.
- That contract in brief: a SHORT prompt fed spec + plan + recent log + job status + new chat
  replies + available workflows; it decides ONE move (reuse vs author a workflow), dispatches
  DETACHED, appends one log line, sets `status` to exactly one enum value + `current_job_id`,
  and MUST NOT rewrite the Spec; completion is the executable check, not its say-so.
- Provider/model: compute in code from their backend knob (Section 9), stamp authored/cloned
  workflows, re-stamp after to prevent drift.
- Re-stamp machine state in code after the pass (do not trust the model for bookkeeping).
- Estimated complexity: Medium

### Phase: Chat adapter [only if Section 5 names a platform]
- `notify(project, text, level)`, `read_replies(project, after)`, `register_thread(...)`.
- Quiet by default: notify only on done / blocked / awaiting-human; routine notes go to the
  Activity Log. Thread per project; ingest the human's replies and advance a cursor.
- Platform specifics from research (auth, send, read-replies). If `none`: replace this phase
  with a one-paragraph "markdown-only" note (the human reads the Activity Log and edits the
  file; no chat phase).
- Estimated complexity: Medium

### Phase: Completion checks + caps + safety [always]
- Executable `completion_check` per project, run in the build workspace; pass -> done, the
  same check failing twice -> blocked. Never trust self-report.
- For subjective domains (games, design): the executable check is a floor; add a human
  verdict gate after it is green (the system parks at awaiting-human rather than auto-done).
- Caps flip to awaiting-human, never crash. Concurrency cap across projects.
- Zombie detection + recovery (a "running" record with no live worker and no output growth
  across a cycle is dead -> re-dispatch detached). Detached-dispatch hardening.
- Estimated complexity: Medium

### Phase: Discoverability - make the interactive agent know the system [always]
- The half that makes the system usable, not just running. A fresh interactive agent (a new
  coding-agent session, the chat bot) will NOT know this system exists or how to steer it
  unless it is in the agent's always-loaded context. Build the three pieces (architecture
  reference Section 4.4):
  - **Index doc** (e.g., `COFOUNDER-PROJECTS.md`): one-paragraph what-it-is; the three-section
    ownership table; the status enum; a worked **"how to update a project doc"** example (set
    status to an active value, add tasks to the Plan, append an Activity Log note, never touch
    the Spec); the **orchestrator-only** rule (a reply/edit is an instruction - do NOT build
    inline); an active-projects list; pointers to the design + a specific project file.
  - **Always-loaded reference:** name the index in whatever the agent reads every session (a
    `CLAUDE.md` / global rules / system prompt), beside however they index other subsystems.
  - **Session-start injection:** if their agent loads context at conversation start (a
    SessionStart hook / chat-engine context builder / memory index), inject the index there too
    so non-CLAUDE.md surfaces (a chat bot, a different backend) also know. Reuse the SAME
    context builder across every entry point.
- This applies even to markdown-only stacks (the human still opens a fresh agent to edit a
  project, and it must know the rules).
- Dependencies: the project model (Phase 1) must exist so the index can describe it.
- Estimated complexity: Low

### Phase: Deploy + first project [always]
- Wire the pass into the scheduler from Phase 2; set env/config (backend knob, caps, chat
  channel, engine paths). Drop a first project spec, set status `new`, and watch one pass
  dispatch + monitor it. Confirm a detached build survives the pass exiting.
- **Acceptance includes discoverability:** open a FRESH agent session and ask it to "mark the
  project in progress and add a task" - it should know to edit the project doc per the rules
  (Plan + Activity Log, not the Spec) without being told the system's mechanics.
- Estimated complexity: Low-Medium

**Each phase includes:** what to build (1-2 sentences), key modules/files, dependencies
(which phases precede), estimated complexity, and personalization notes (how their answers
shape it).

**Footer:**
- Recommended build order: phases 1-5 are largely sequential; the chat adapter (6) can
  parallelize once the core runs; 7 hardens; 8 makes it discoverable to the interactive agent;
  9 ships. 
- "This PRD was generated from your requirements. Revisit as your stack evolves."

---

## Personalization Rules

- **Honor the Build mode in every phase.** If **extend an existing second brain**: phases
  REUSE what they have - "add a pass to your loop", "store projects in your existing vault",
  "reuse your chat client", "reuse your file-lock / state-I/O / daily-log / agent-SDK
  helpers." If **standalone from scratch**: the same phases BUILD minimal versions - a tiny
  scheduled runner, a plain state folder, a notify path (or markdown-only), and the few small
  utilities. The core (project model, deterministic gates, engine adapter, orchestration,
  completion + caps) is identical either way; only "reuse vs build" changes. Say which in each
  phase's personalization note.
- **Use THEIR adapter names everywhere** - not "the engine" but "Archon" / "GitHub Actions";
  not "chat" but "Discord" / "Slack"; not "the scheduler" but "your existing heartbeat" /
  "a systemd timer."
- **Detached dispatch is non-negotiable** in the engine phase regardless of engine. State it
  plainly with the failure mode (orphaned build dies when the pass exits).
- **Discoverability is non-negotiable** in Phase 8 regardless of stack. The built system is
  unusable until a fresh interactive agent knows it exists - reuse their always-loaded context
  mechanism (CLAUDE.md / system prompt / SessionStart hook) for the index, or build a minimal
  one. For **extend** mode, point at their existing context file + session loader; for
  **standalone**, the minimal version is the index doc + whatever single always-on context their
  runtime reads. State the failure mode (a fresh chat has no idea how to steer it).
- **If scheduler is `none`,** include the build-a-minimal-heartbeat phase and keep it tiny.
  If `existing-loop`, do NOT include a build-a-scheduler phase - show the add-a-pass seam.
- **If chat is `none`,** omit the chat adapter phase; replace with a markdown-only note and
  make sure the Activity Log + file-edit steering is described in the orchestration phase.
- **Map autonomy level (Section 7)** to concrete behavior: approval-gated (park for review
  before each dispatch/merge) vs full auto-advance (no gates, ping only on terminal states).
  Reflect the merge policy (PR-for-review on pre-existing repos vs straight-to-main on
  system-owned/greenfield).
- **Map caps (Section 8)** directly into the caps phase (iterations, wall-clock, concurrency,
  optional spend).
- **Map completion-check style (Section 9)** into the completion phase; if any target domain
  is subjective (games/design), always add the human-verdict gate.
- **Provider/model (Section 9):** one backend knob drives both the orchestrator's reasoning
  and the build provider; show the per-project override.
- Always renumber phases contiguously in the output (1, 2, 3..., not 1, 3, 4, 7).
