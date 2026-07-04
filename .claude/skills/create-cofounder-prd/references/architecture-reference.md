# Architecture Reference: Autonomous Co-founder Projects

This is the blueprint the `create-cofounder-prd` skill reads to generate a portable PRD.
It describes a system that becomes the orchestrator of the larger, multi-session tasks you
hand it: you drop a spec, and it breaks the work into isolated workflows, dispatches and
monitors them, and steers toward done, with a human steering through chat and markdown edits.
It is written abstractly: the three external dependencies (a scheduler, a build engine, a
chat platform) are **adapters** so the same core fits any stack.

The one-line frame: the agent becomes the **orchestrator of the larger tasks you hand it**.
The human drops a spec; the system carries it forward between check-ins, breaking it into
workflows and dispatching + monitoring them.

## Build modes (extend an existing agent, or build from scratch)

The same system stands up two ways, and the PRD says which in every phase:

- **Extend an existing second brain / agent** - reuse its scheduler (add a pass), its memory
  store (drop the project files in), its chat integration, and its shared utilities (file
  locking, state I/O, an append-only log, the agent-SDK call). A lighter build.
- **Standalone from scratch** - build the minimal versions of exactly those pieces: a tiny
  scheduled runner (the SchedulerAdapter "build one" mode), a plain state folder, a notify
  path (the ChatAdapter, or markdown-only), and a handful of small utilities.

The CORE is identical in both modes. The project model, the status machine, the
deterministic-gates-then-LLM orchestration, the engine adapter, and completion + caps have no
dependency on a second brain. Only the three adapters and a few utilities differ, and that
difference is just "reuse vs build" - which is why from-scratch is fully supported: the
thehomie-specific surface is small and adapter-shaped.

---

## 1. The pattern (the Ralph loop)

The hard problem is context rot: a single long agent session degrades as its context fills.
The fix is to make the model amnesiac on purpose and push all durable state into files.

- **State lives outside the model.** A project is a markdown file. The document remembers;
  the chat does not. Every run starts fresh and re-reads the file.
- **Orchestrate, do not execute.** Each run dispatches a build workflow and returns. The
  long build happens in the engine, never inline. Each run stays short and cheap.
- **One item per loop.** Each run advances the work by one concrete step (dispatch one
  workflow, or run one completion check), then stops.
- **Executable completion.** "Done" is a test/typecheck/lint/build passing, never the agent
  saying so. For subjective domains (games, design), the executable check is a floor and a
  human verdict is the real gate after it is green.
- **Hard caps.** A bounded iteration count and a wall-clock ceiling stop a runaway loop.
  Tripping a cap parks the project for a human; it never crashes or spins forever.
- **Short prompts beat long ones.** The orchestration prompt is deliberately terse.
- **Deterministic gates first, LLM only for judgment.** Everything mechanical (parse, poll,
  enforce caps, gate "do not dispatch while a job runs") happens in code before any model
  call. The model is invoked only when a real decision remains.

---

## 2. The project model (engine-agnostic)

A project is one markdown file with three sections under strict ownership, plus machine
state in YAML frontmatter.

```markdown
---
status: new            # new | building | testing | blocked | awaiting-human | done
created: <iso8601>
last_run: <iso8601>
repo: <target repo or "greenfield">
branch: <worktree branch this project builds on>
current_job_id: <engine run id of the in-flight workflow, or null>
iterations: 0
max_iterations: 100
max_wall_clock_hours: 72
completion_check: "<the executable signal of done, run in the build workdir>"
chat_channel: null     # set on first ping
chat_thread: null
---

# <Project Title>
## Spec (STATIC - the orchestrator MUST NOT rewrite; only the human edits)
## Plan / Working Memory (MUTABLE - the orchestrator may rewrite)
## Activity Log (APPEND-ONLY - newest at the bottom)
```

- **Spec (static):** what to build, success criteria, constraints. The source of truth.
- **Plan (mutable):** current TODO, next move, open questions.
- **Activity Log (append-only):** short status notes, never edited.

Machine state lives in the frontmatter plus a small per-machine JSON state file (last
chat-reply cursor, fail streak, wall-clock start). State is markdown + git, not model
context.

---

## 3. The status state machine

Deterministic transitions in code; judgment in the LLM.

- `new` -> read the spec, pick + dispatch the first workflow -> `building`.
- `building` -> poll `current_job_id`:
  - running -> append a tiny note, skip the LLM (the cheap common case).
  - completed -> hand to the LLM: decide the next workflow, or move to `testing`.
  - failed / cancelled / lost -> hand to the LLM: resume, reset, or escalate -> `blocked`.
- `testing` -> run the executable `completion_check` in the build workdir. Pass -> `done`.
  Fail -> back to `building` with a fix; the same check failing twice -> `blocked`.
- `blocked` / `awaiting-human` -> wait for a chat reply or a markdown edit, then resume.
- `done` -> terminal; archive the file to a `done/` folder.

Be tolerant of a non-enum active status (an LLM may write `in_progress` instead of
`building`): treat anything not in {new, testing, blocked, awaiting-human, done} as an
active build so the monitor keeps polling and does not stall.

---

## 4. The adapters (this is where generality lives)

The core above never changes. The things that vary per stack are adapters. Three run the
autonomous loop (Scheduler, Engine, Chat). A fourth - **Discoverability** (4.4) - is what makes
the system actually *usable*: it is how the human's interactive agent learns the system exists
and how to steer it. Skip it and you get a system that runs but that no fresh conversation
knows how to drive (see the failure in 4.4 and the Appendix).

### 4.1 SchedulerAdapter - "wake the orchestrator periodically"

```
should_run() -> bool        # is it time / allowed to run a pass now
```

Implementation modes:
- **Reuse an existing proactive loop** (an existing heartbeat / agent loop): add the
  orchestration pass to it, isolated so its failure cannot break the host loop. Run it on
  every invocation (even off-hours) so long work advances overnight.
- **Standalone scheduler:** cron / Task Scheduler / launchd / systemd timer calling a CLI.
- **None yet -> build a minimal one:** the PRD includes a phase to build a tiny scheduled
  runner (a script + an OS timer) if the adopter has no loop. This is the "create a
  heartbeat if it does not exist" path.

The pass must be safe to run concurrently with itself (file-lock the project file; the
"do not dispatch while a job runs" gate is the second line of defense).

### 4.2 EngineAdapter - "run a build workflow in isolation"

```
dispatch(name, branch, message, workdir) -> run_id   # MUST detach (survive the pass exiting)
poll(run_id) -> (status, workdir)                     # running | completed | failed | unknown
completion_env(workdir) -> env                        # how to run the completion_check there
```

Requirements that are not optional:
- **Detached dispatch.** The build MUST survive the orchestration pass exiting. A plain
  background child gets killed when the pass ends (a real failure we hit: a "running"
  record with no process and no progress). Detach it (new session / nohup / a managed
  daemon / a remote runner).
- **Worktree / workspace isolation** so parallel builds do not collide.
- **A queryable run-state** (a DB, an API, a status file) so the orchestrator can poll by
  run id without blocking.
- **Zombie detection:** a "running" record with no live worker and no output growth across
  more than one cycle is dead; treat it as failed and recover (re-dispatch, detached).

Implementations: an Archon-style workflow engine (worktree + run-state DB + CLI dispatch);
a Claude Code `/loop` session; a CI system (GitHub Actions dispatch + run API); a custom
runner. The orchestrator only needs the three methods above.

### 4.3 ChatAdapter - "talk to the human"

```
notify(project, text, level)            # level in {info, done, blocked, awaiting-human}
read_replies(project, after) -> [msg]   # the human's replies since a cursor
register_thread(project, ref)           # so replies are reply-able
```

Implementations: Slack, Discord, Teams, email, a webhook, or none (markdown-only, where the
human reads the Activity Log and edits the file). Keep it quiet: notify only on
done / blocked / awaiting-human; routine progress goes to the Activity Log.

### 4.4 DiscoverabilityAdapter - "make the interactive agent know the system exists"

This is the half that the autonomous loop quietly depends on and that is the easiest to forget.
The whole "human steers through chat and markdown edits" premise assumes the human's
**interactive** agent (a fresh Claude Code session, the chat-bot, whatever they talk to) already
knows: that this system exists, where project files live, the three-section ownership rules, the
status enum, and that a project reply/edit is an instruction to the orchestrator (not a cue to
go build it itself). **A fresh agent knows none of that unless it is put into the agent's
always-loaded context.** A capability the agent cannot see at conversation start effectively
does not exist to it.

The generalized principle: **for any proactive subsystem, "built" is not "usable" until it is
discoverable.** Make it discoverable three ways (reuse the adopter's context mechanism, or build
a minimal one):

```
index_doc            # a lean overview the agent reads on demand (the source of truth for "how to use it")
always_loaded_ref    # a pointer to the index from the agent's always-on rules / system prompt
session_injection    # the index (or its pointer) injected at the start of every conversation
```

- **Index doc** (e.g., `COFOUNDER-PROJECTS.md`): what the system is in one paragraph; the
  three-section ownership table (Spec static / Plan mutable / Activity Log append-only); the
  status enum; **a worked "how to update a project doc" example** (set status to an active value,
  add tasks to the Plan, append an Activity Log note, never touch the Spec); the **orchestrator-only
  rule** (a reply/edit is an instruction; do NOT build inline); an active-projects list; and
  progressive-disclosure pointers to the design + a specific project file.
- **Always-loaded reference:** name the index in whatever the agent reads every session - a
  `CLAUDE.md` / a global rules file / the agent's system prompt - right next to however they
  already index other subsystems (repos, etc.). This is the line that makes a fresh chat able to
  find it at all.
- **Session-start injection:** if their agent loads context at conversation start (a SessionStart
  hook, a chat-engine context builder, a memory index), inject the index there too so it is
  present even for non-CLAUDE.md surfaces (a Slack bot, a different backend). Reuse the SAME
  context builder across every entry point so all surfaces stay consistent.

Implementations by build mode: **extend** -> add the index file and one reference line to their
existing always-on context + their existing session-start loader. **standalone** -> the minimal
version is just the index doc plus whatever single always-on context their chosen runtime reads.
Markdown-only stacks still need this: even with no chat, the human opens a fresh agent session to
edit a project, and that agent must know the rules.

---

## 5. The orchestration pass (per project, per run)

Deterministic gates first, then the LLM only if a real decision remains:

1. Enforce caps (iterations, wall-clock). Tripped -> set `awaiting-human`, notify once, skip.
2. Poll `current_job_id` via the EngineAdapter.
3. Ingest new chat replies via the ChatAdapter (advance the cursor).
4. Hard gate: if a job is running and there is no new human input, append a tiny note and
   return (never dispatch).
5. If `testing`, run the executable `completion_check` in the build workdir and apply the
   deterministic transition (pass -> done; fail twice -> blocked; else -> building).
6. If a real decision remains (new project, job finished, human replied), run a SHORT LLM
   pass that: reads the spec + plan + recent log + job status + replies + the available
   workflows, decides reuse-vs-author a workflow, dispatches it DETACHED via the
   EngineAdapter, appends one Activity Log line, and sets `status` + `current_job_id`.
7. Re-stamp machine state in code (do not trust the model for bookkeeping). Notify on a
   terminal flip (done / blocked / awaiting-human).

---

## 6. Provider / model selection

When the orchestrator authors or clones a workflow, compute the provider/model in code from
a single backend knob (the same one the host agent reasons with), with a per-project
override. This keeps one switch driving both the orchestrator's reasoning and the build
work. The model is never picked by the authoring LLM; code stamps it and re-stamps after to
prevent drift. A reuse candidate whose baked-in provider does not match gets cloned and
re-stamped rather than run on the wrong provider.

---

## 7. Safety and the lessons baked in as requirements

- **Detached dispatch is mandatory** (see 4.2) or every autonomous build dies with its pass.
- **Zombie detection + recovery** for stuck "running" records.
- **Executable completion only** (never self-report); subjective quality is a separate human
  gate after the check is green.
- **Caps flip to awaiting-human, never crash.**
- **The no-double-dispatch gate** (a running job means the only action is a status note).
- **The orchestrator never runs destructive git in the live checkout;** resets happen inside
  the isolated workspace via the workflow.
- **Merge policy is explicit:** a system-owned/greenfield repo may commit straight to main;
  a pre-existing repo gets a PR left for review (no auto-merge) unless the adopter opts into
  full auto.
- **Discoverability is mandatory** (see 4.4) or the system is built-but-unusable: a fresh
  interactive agent will not know it exists or how to steer it. Ingrain it into the agent's
  always-loaded context (index doc + always-on reference + session-start injection).

---

## 8. Reference data flow

```
SchedulerAdapter.should_run()
        -> for each project file (skip _-prefixed, README, done/):
             parse -> enforce caps -> EngineAdapter.poll(current_job_id)
               -> ChatAdapter.read_replies()
               -> running + no human input? append note, return
               -> testing? run completion_check in workdir, transition
               -> needs a decision? SHORT LLM pass -> EngineAdapter.dispatch(detached)
               -> re-stamp state, ChatAdapter.notify() on a terminal flip
```

The core is fixed; swap the three adapters and the same system runs on any stack.

---

## 9. The orchestration prompt contract (write your own that satisfies it)

The orchestration prompt is the behavior-critical, creative part - so design YOUR OWN in your
own voice and for your own stack. Do NOT copy someone else's verbatim. But to behave
correctly it MUST satisfy this contract. Treat each item as a requirement, not a suggestion:

- **Inputs it is given:** the Spec, the current Plan, the last few Activity Log lines, the
  in-flight job status, any new human (chat) replies, and the list of available workflows.
- **It decides exactly ONE next move:** reuse a generic workflow, reuse a project-specific
  one, or author a new workflow only when nothing fits.
- **It dispatches DETACHED** (the engine's detach idiom) with worktree isolation, and never
  without the isolation/branch + sandbox flags your engine requires.
- **It sets `status` to EXACTLY one enum value** (new | building | testing | blocked |
  awaiting-human | done) and records the run id in `current_job_id`. Forbid invented statuses.
- **It appends ONE short line to the Activity Log** (newest at the bottom) and may rewrite the
  Plan section.
- **It MUST NOT rewrite the Spec.**
- **It is told completion is the executable check, not its own say-so.**
- **Keep it SHORT.** Short prompts beat long ones here; a terse contract-satisfying prompt
  outperforms a sprawling one.

Keeping the prompt short and orchestrate-only is what keeps each pass cheap and fast. The
generated PRD should hand the builder THIS checklist, not a finished prompt.

## 10. Required project-file fields (design your own file around these)

Same principle: design your own project file format, but it must carry the state the loop
depends on. Required frontmatter (names are yours): a **status** enum, a **current job id**, a
**target repo/branch**, **iteration + wall-clock counters and their caps**, and the
**executable completion command**. Plus chat refs once a thread exists. The body needs the
three owned sections: **Spec (static)**, **Plan (mutable)**, **Activity Log (append-only)**.
Per-machine bookkeeping (reply cursor, fail streak, wall-clock start) lives OUTSIDE the file
in a small state store. Anything missing from this list breaks a specific gate, so the PRD
should require the fields, not a particular layout.

---

## Appendix: Lessons / gotchas (the failures that only show up live)

These were learned the hard way on a real run. Bake them into the PRD as requirements so a
new build skips them:

- **Detached dispatch or the build dies.** Dispatching the workflow as a plain background
  child of the orchestration pass gets it killed when the pass exits, leaving a "running"
  record with no progress. Detach it (new session / nohup / a managed daemon / a remote
  runner). This is the single most common failure.
- **Built is not usable without discoverability.** A real failure: the system ran fine, but a
  fresh interactive agent (a new Claude Code session, the chat bot) had no idea it existed or
  how to update a project doc - asked to "mark the project in progress and add tasks," it had
  to go read the source code, and on one stack it instead built the project itself out-of-band.
  Fix: an index doc, named in the agent's always-loaded context, injected at session start
  (see 4.4). Without this the "steer via chat and markdown" half of the system silently does
  not work.
- **Zombie detection.** A "running" record with no live worker process AND no workspace growth
  across a full cycle is dead. Treat it as failed and re-dispatch (detached). The loop cannot
  self-heal a stuck "running" row otherwise; it will wait until the wall-clock cap.
- **Tolerate a non-enum active status.** The orchestrating LLM may write `in_progress` instead
  of `building`. Treat anything not in {new, testing, blocked, awaiting-human, done} as an
  active build so the monitor keeps polling instead of stalling.
- **Loop nodes ignore per-node provider/model.** If you author/clone workflows and stamp the
  provider, loop-style nodes inherit the workflow-LEVEL provider, not the per-node one - stamp
  both, or the loop runs on the wrong model.
- **The engine CLI may not be on the non-interactive PATH.** Cron/headless shells have a
  minimal PATH; prepend the engine's bin dir explicitly in the runner and any subprocess env.
- **Timezone-aware vs naive datetimes in caps.** Compare wall-clock starts and "now" with
  consistent tz handling, or the cap math throws or misfires.
- **Prefer engine dispatch in code over the LLM doing it.** Letting the LLM run the dispatch
  via a shell tool is fragile (it is how the orphaned-build zombie happened). A deterministic
  code-side dispatch that detaches is more reliable; let the LLM DECIDE the workflow, let code
  DISPATCH it.
- **Run-state lives where the engine puts it.** Confirm the actual store (a SQLite file, a
  Postgres DB, an API) and column/field names before writing the poll; degrade to "unknown"
  on any read failure rather than crashing the pass.
