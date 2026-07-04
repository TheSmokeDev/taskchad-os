---
title: Co-founder Projects - Plan + What We Actually Built (workshop edition)
created: 2026-06-07
for: TheHomie workshop, Fri Jun 12 2026 (long-running / autonomous agents)
status: shipped + running its first real project live (North Star)
related:
  - "[[cofounder-projects-design]]"
  - "[[north-star-game]]"
  - "[[archon]]"
  - "[[dark-factory-plan]]"
---

# Co-founder Projects: the plan, and what we actually built

This is the single-file record of the **proactive / autonomous addition to the Second
Brain**: the plan we set out with, the system we actually shipped, and how it plugs into
the The Homie. It is the thing to share at Friday's workshop. Everything below was
built and deployed in one working session and is, as of this writing, autonomously
building its first real project (the North Star game) on the VPS.

The one-line story: **the The Homie stopped being something you query and became
something you delegate whole tasks to.** It is now the orchestrator of the larger,
multi-session tasks you hand it: you drop a spec as a markdown file, and the The Homie's
heartbeat carries it forward between your check-ins, breaking the work into workflows and
dispatching + monitoring them, and pings you only when it needs you or the work is done.

---

# PART 1 - THE PLAN

## The problem

The The Homie heartbeat was **reactive**: every 30 minutes it surfaced email, calendar,
tasks, drafts, and content ideas. Useful, but it never *did* multi-step build work. There
was no way for a long piece of engineering to advance across many sessions. Context rot
kills any single long agent session; there was no durable, executable-checked,
human-steerable loop that turns a spec into shipped commits.

## The shape: the Ralph pattern

The fix borrows the Ralph loop (Geoffrey Huntley's pattern, the Anthropic ralph-wiggum
reference). The core moves:

- **State lives outside the model.** A project is a markdown file. The document remembers;
  the chat does not. Every run starts with fresh context and re-reads the file. The model
  is amnesiac on purpose, which is exactly what sidesteps context rot.
- **Orchestrate, do not execute.** The heartbeat pass dispatches a build workflow and
  returns. It never runs the long build inline. Each pass stays well under the 30-minute
  heartbeat budget.
- **One item per loop.** Each pass advances the work by one concrete step: dispatch one
  workflow, or run one completion check, then stop.
- **Executable completion.** "Done" is a test/typecheck/lint pass, never the agent saying
  so. Same discipline the content pipeline already uses for assets, applied to builds.
- **Hard caps.** A bounded iteration count and a wall-clock ceiling stop a runaway loop.
  Tripping a cap parks the project for a human; it never crashes or spins forever.
- **Short prompts beat long ones.** The orchestration prompt is deliberately terse.

This is the generalized, spec-driven sibling of the [[dark-factory-plan]]: any project, any
repo, steered by a human through Slack and markdown.

## State separation (three sections, three owners)

Every project file has three sections with strict ownership:

1. **Spec (STATIC).** What we are building, success criteria, constraints. Only the human
   edits it. The orchestrator reads it every run and never rewrites it.
2. **Plan / Working Memory (MUTABLE).** Current TODO, next move, open questions. The
   orchestrator may rewrite this freely.
3. **Activity Log (APPEND-ONLY).** Newest at the bottom. Short status notes, never edited.

Machine state (the status enum, in-flight job id, iteration count, caps, last-ingested
Slack reply ts) lives in YAML frontmatter plus a per-machine JSON state file.

## The status state machine

Deterministic transitions in Python; judgment calls in the LLM.

`new -> building -> testing -> done`, with `blocked` and `awaiting-human` as the human-gate
states. A new project gets a workflow dispatched. While building, Python polls the job; a
running job just gets a one-line note (no LLM, the cheap common case). When the job
finishes, the LLM decides the next move. In testing, the executable completion check runs;
pass means done, the same test failing twice means blocked. Any cap tripped means
awaiting-human. Done archives the file to a `done/` folder.

## The design decisions that mattered (locked with Cole)

- **Full auto-advance.** Drive new to done with no approval gates, including authoring new
  workflows on the fly. Ping only on done / blocked / awaiting-human; routine progress goes
  to the markdown Activity Log so the phone stays quiet.
- **Completion is an executable check in the build worktree.** Never trust agent
  self-report. For a game, "tests pass" is not "good," so the human fun verdict stays a
  separate gate after the executable check is green.
- **Caps are time-dominant:** 100 iterations, 72 wall-clock hours. No spend cap.
- **Concurrency cap of 3** workflows across all projects.
- **Provider follows the The Homie.** The provider/model for any workflow the system
  authors or clones is computed in Python from `SB_AGENT_BACKEND` (default Claude), with a
  per-project override. Flip one env var to move all new build work onto Pi later.
- **State in markdown + git + a small JSON file**, never in the model's context.

---

# PART 2 - WHAT WE ACTUALLY BUILT

The system shipped as one new orchestrator module plus small, surgical changes to the
existing The Homie. It reuses the The Homie's own primitives throughout (file
locking, state I/O, daily-log helpers, the Agent SDK invocation, the Slack integration,
the frontmatter stamp helpers, the chat session store).

## The new orchestrator: `.claude/scripts/cofounder_projects.py`

A single module (~1,200 lines) plus a CLI (`--once`, `--test`, `--project`, `--dry-run`).
It follows a strict **Python-before-LLM** discipline: every deterministic thing happens in
Python first; the LLM is only invoked for genuine judgment.

The deterministic layer (all unit-tested):

- `discover_projects` / `parse_project` - find and parse project markdown (frontmatter +
  the three sections); skip `_`-prefixed files, README, and the `done/` subdir; malformed
  files are skipped with a warning, never crash the pass.
- `poll_archon_run` - read an Archon run's status. **SQLite-first** (the VPS keeps run state
  in `~/.archon/archon.db`), with a Postgres fallback only if a DATABASE_URL is set. Any
  failure degrades to "unknown" and never raises.
- `resolve_run_id_by_branch` - find a freshly dispatched run by its worktree branch.
- `dispatch_archon_workflow` - fire-and-forget dispatch with worktree isolation
  (`IS_SANDBOX=1 archon workflow run ... --branch ... --cwd ...`), detached so it survives.
- `run_completion_check` - run the project's executable `completion_check` in the build
  worktree; pass/fail plus an output tail.
- `caps_tripped` - iteration + wall-clock cap enforcement (returns a reason or None).
- `needs_attention` / `_is_build_status` - decide deterministically whether a project needs
  the expensive LLM pass this run (a still-running job does not).
- `resolve_archon_provider` + `workflow_provider` / `stamp_workflow_provider` /
  `clone_workflow_for_provider` - provider/model resolution from `SB_AGENT_BACKEND`, and
  the clone-and-stamp path for reusing a workflow whose baked-in provider does not match.
- `resolve_repo_path` - resolve the absolute checkout path for the dispatch `--cwd`.

The judgment layer:

- `build_project_prompt` - a SHORT prompt: the Spec, current Plan, recent Activity Log, the
  Archon job status, any new Slack replies, the available workflows, the resolved
  provider/model, and the decision contract (reuse vs author a workflow, dispatch, append a
  log line, set status). It forbids rewriting the Spec and reminds the model that
  completion is the executable check.
- `run_one_project` - deterministic gates first (caps, poll, Slack replies, the hard
  "never dispatch while a job is running" gate, the executable completion check), then the
  SDK `query()` orchestration pass only if a real decision remains, then re-stamp machine
  state and ping Slack on a terminal flip.
- `run_cofounder_projects` - the loop over all projects, plus the global concurrency cap.

## How it plugs into the heartbeat: `.claude/scripts/heartbeat.py`

This is the integration seam, and it is small on purpose. The existing `run_heartbeat` was
renamed to `_run_heartbeat_core`, and a thin wrapper now runs the co-founder pass in a
`finally` block:

```python
async def run_heartbeat(test_mode=False):
    result = None
    try:
        result = await _run_heartbeat_core(test_mode=test_mode)
    finally:
        await run_cofounder_projects_pass(test_mode=test_mode)
    return result
```

Three properties fall out of that shape:

- **It runs on EVERY heartbeat invocation, 24/7.** The active-hours gate short-circuits the
  alert/draft/habits work, but the `finally` still fires, so the autonomous loop keeps
  advancing overnight.
- **It is its own separate reasoning pass**, with the `archon` skill loaded, NOT folded into
  the main heartbeat prompt.
- **Its failure can never break the normal alert** (it is isolated and self-wrapping; a
  failure logs a `COFOUNDER_PROJECTS_ERROR` marker to the daily log).

It no-ops fast when there are no live projects.

## The other touches

- `.claude/scripts/config.py` - the `COFOUNDER_*` constants (folder path, state file, caps,
  concurrency, Slack channel, `SB_AGENT_BACKEND`, the first-run model override) and the new
  folder added to `ensure_directories`.
- `.claude/scripts/integrations/slack_api.py` - `get_thread_replies(channel, thread_ts,
  after_ts)` so the loop can ingest the human's replies to a project thread (filtering the
  root + bot messages).
- `.claude/scripts/tests/test_cofounder_projects.py` - 29 unit tests covering the whole
  deterministic layer with mocks (discovery, parse, caps, the attention classifier, the
  Archon poll, Slack reply parsing, the no-double-dispatch gate, provider resolve, and the
  clone-and-stamp). ruff + mypy strict clean.

## The vault surface (where a human interacts)

- `TheHomie/Co-founder Projects/` - the watched folder. `_TEMPLATE.md` is the project
  schema; `README.md` explains the folder, the status enum, and how to add a project.
- `vault/memory/plans/cofounder-projects-design.md` - the full design doc.
- A project = one markdown file here. Drop one (or co-author it with the The Homie in the
  terminal), set `status: new`, and the next heartbeat takes it from there.

---

# PART 3 - HOW IT INTEGRATES INTO THE SECOND BRAIN

The point worth making at the workshop: this was not a bolt-on. It is built out of the
The Homie's own parts, which is why it is small and why it inherits the The Homie's
reliability and memory.

- **The heartbeat is the engine.** The The Homie already wakes every 30 minutes (cron on
  the VPS). The co-founder loop is just one more pass on that existing heartbeat, so it gets
  scheduling, the Agent SDK setup, and the credentials for free.
- **The vault is the memory.** Projects live in the Obsidian vault next to the rest of the
  The Homie's memory, and they sync between machines via the same git-sync. State is
  markdown + git, exactly like the rest of the Brain.
- **Slack is the conversation surface.** Pings go to Slack and register as heartbeat threads
  in the same chat session store the The Homie already uses, so a reply can steer a
  project the same way a reply steers a heartbeat alert.
- **It reuses the Brain's primitives.** `file_lock`, `load_state` / `save_state`,
  `append_to_daily_log`, the `validate_bash_command` safety hook, the frontmatter stamp
  helpers, the Slack client, and the canonical `query()` invocation are all the existing
  The Homie code.
- **Archon is the muscle.** The Brain orchestrates; Archon workflows (fire-and-forget,
  worktree-isolated) do the actual building and testing. Clean separation: the Brain decides
  and monitors, Archon builds.
- **Provider follows the Brain.** Because the build provider is derived from the Brain's own
  `SB_AGENT_BACKEND`, flipping the Brain onto Pi later moves the build work with it.

So the The Homie now has two modes on one heartbeat: the reactive pass (surface what
needs attention) and the proactive pass (advance the projects you handed it).

---

# PART 4 - THE PROOF: the first real run (North Star, live)

The first project is a greenfield rebuild of Cole's game **North Star**, scoped to one
polished town-defense loop plus an agent test/replay/watch harness (the
`north-star-first-heartbeat` project file). It was dropped in, and the system drove it with
zero hand-holding on each step:

- Created the private repo `coleam00/north-star`, seeded it with the spec + build rules,
  and dispatched the first workflow.
- **M0 (agent harness) built and merged to main** - deterministic sim, seeded RNG,
  record/replay with state-hash checkpoints, a replay viewer with transport controls and
  debug overlays, a live spectator, and the headless bot harness. CI green, 18/18 tests.
- **M1 (core defense loop) built and merged** - hero with sword + dodge (hit-stop,
  knockback), three enemy types with element weaknesses, archer + mage towers with homing
  and AoE, three-wave tier escalation, the war-horn three-tier dial, currency + an upgrade,
  and the full phase machine. CI green, 52/52 tests.
- **Renderer + audio + UI** (the full-graphics, full-audio, watchable layer) was open as a
  PR and finishing at the time of writing.

It self-advanced milestone to milestone (build, run the executable check, merge, dispatch
the next), exactly as designed.

### What this run also taught us (kept honest for the workshop)

The first dispatch died: the LLM backgrounded the workflow as a child of the orchestration
pass, and when that pass exited the build was orphaned and killed, leaving a "running" row
with no progress. The monitoring loop caught it, recovered it with a properly detached
dispatch, and we hardened the system so every dispatch now detaches (survives the pass
exiting). That is the real lesson of long-running agents: the orchestration is the easy
part; making the work survive process boundaries and surface its own failures is the part
that earns the "autonomous" label.

---

# PART 5 - HONEST ROUGH EDGES / WHAT IS NEXT

- **Greenfield repo creation is not yet automated in Python.** For the first run the repo
  was created by hand; the loop currently dispatches and monitors an existing repo.
  Auto-creation (gh create, clone, register) is the clear fast-follow.
- **Step cadence is about one move per 30-minute heartbeat.** Fine for unattended runs;
  could batch multiple safe steps per pass later.
- **The bundled Agent SDK CLI emits stream/suspend noise** under a detached process on the
  VPS. Non-fatal (the pass completes and dispatches) but worth cleaning up.
- **A dedicated `#cofounder-projects` Slack channel** does not exist yet; pings currently go
  to `#thehomie`.
- **Auto-merge vs PR-for-review** on a system-owned repo is a policy toggle; the first run
  auto-merged green PRs per the full-auto decision.

---

# APPENDIX - file + command map

**New / changed code (in `thehomie`, on `main`):**
- `.claude/scripts/cofounder_projects.py` (new) - the orchestrator + CLI.
- `.claude/scripts/tests/test_cofounder_projects.py` (new) - 29 unit tests.
- `.claude/scripts/config.py` - `COFOUNDER_*` constants + `ensure_directories`.
- `.claude/scripts/integrations/slack_api.py` - `get_thread_replies`.
- `.claude/scripts/heartbeat.py` - `_run_heartbeat_core` + `run_cofounder_projects_pass` +
  the `run_heartbeat` wrapper.
- `.claude/scripts/run_heartbeat.sh` - archon on PATH + first-run config.

**Vault surface:**
- `TheHomie/Co-founder Projects/` (`_TEMPLATE.md`, `README.md`, project files).
- `vault/memory/plans/cofounder-projects-design.md` (design doc).
- `vault/memory/plans/cofounder-projects-plan-and-build.md` (this file).

**Run it:**
```bash
cd .claude/scripts && uv run python cofounder_projects.py --once          # one orchestration pass
cd .claude/scripts && uv run python cofounder_projects.py --test --once   # no dispatch / SDK
cd .claude/scripts && uv run python cofounder_projects.py --dry-run --project <slug>
```
On the VPS it runs automatically as part of the `*/30` heartbeat, 24/7.

**Add a project:** copy `_TEMPLATE.md` to `<slug>.md` in `TheHomie/Co-founder Projects/`,
fill the Spec, set `repo` / `branch` / `completion_check`, leave `status: new`.
