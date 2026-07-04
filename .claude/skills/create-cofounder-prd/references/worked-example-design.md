---
title: Co-founder Projects - Autonomous Project Orchestration
created: 2026-06-07
status: design + first cut shipped
tags: [autonomous-agents, archon, heartbeat, ralph-loop, thehomie]
related:
  - "[[dark-factory-plan]]"
  - "[[archon]]"
  - "[[repositories/archon]]"
---

# Co-founder Projects - Autonomous Project Orchestration

This is the system that turns the The Homie from something you *query* into
something you *delegate whole tasks to*. It becomes the **orchestrator of the
larger, multi-session tasks you hand it**: Cole drops a spec as a markdown file,
and the autonomous heartbeat carries it forward between his check-ins, breaking
the work into workflows and dispatching + monitoring them. Cole steers via Slack
replies or edits to the markdown; the system does the long build work via
isolated, fire-and-forget workflows.

This doc is both the design of the full loop and the source material for the
Friday Jun 12 TheHomie workshop on long-running agents.

## The vision in one paragraph

A new orchestration layer sits on top of the existing heartbeat. Each markdown
file in `TheHomie/Co-founder Projects/` is one active project: a living
document holding the spec, the current status, and an append-only activity
log. Every heartbeat (~30 minutes) runs a separate, fast, orchestrate-only
pass that, per project, polls any running Archon workflow, ingests any new
Slack reply or markdown edit from Cole, decides the next move, appends a short
status note, and pings Cole only when it needs input or thinks the work is
done. The heartbeat orchestrates. Archon workflows are the engine. State lives
in markdown plus git plus a small JSON file, never in the model's context
window.

## Why this shape (the Ralph pattern)

The hard problem with long-running engineering work is context rot. A single
agent session that tries to carry a multi-day build in its head degrades as
the context window fills. The Ralph loop (Geoffrey Huntley's pattern, and the
Anthropic ralph-wiggum reference plugin) solves this by making the agent
amnesiac on purpose and pushing all durable state out into files:

- **State lives outside the model.** The project document remembers; the chat
  does not. Each run starts with fresh context and re-reads the current file.
- **One item per loop.** Each pass advances the work by one concrete step
  (dispatch one workflow, run one completion check), then stops.
- **Specs are the source of truth.** The static Spec section is never rewritten
  by the agent. It is the contract the loop keeps re-deriving from.
- **Executable completion.** "Done" is a test/typecheck/lint pass, never the
  agent's self-report. The same discipline `cofounder_pipeline.verify_outputs`
  already applies to content assets, applied here to builds.
- **Hard caps.** A bounded iteration count and a wall-clock ceiling stop a
  runaway loop. Tripping a cap parks the project for a human, it never crashes
  or spins forever.
- **Short prompts beat long ones.** The research finding is blunt: a ~100-word
  orchestration prompt outperformed a ~1,500-word one. The orchestration pass
  is deliberately terse.

This is the same philosophy as the [[dark-factory-plan]] (a self-evolving
codebase driven by Archon workflows). Co-founder Projects is the generalized,
spec-driven version: any project, any repo, steered by a human through Slack
and markdown.

## State separation (three sections, three owners)

Every project file has three sections with strict ownership rules:

1. **Spec (STATIC).** What we are building, success criteria, constraints.
   Only Cole edits this. The orchestrator reads it every run and must never
   rewrite it.
2. **Plan / Working Memory (MUTABLE).** The current TODO, the next workflow to
   run, open questions. The orchestrator may rewrite this freely.
3. **Activity Log (APPEND-ONLY).** Newest entry at the bottom. The orchestrator
   appends short notes. Nothing here is ever edited or deleted.

Machine state (the status enum, in-flight job IDs, iteration count, caps,
last-ingested Slack ts) lives in YAML frontmatter plus a per-machine JSON
state file at `.claude/data/state/cofounder-projects-state.json`. The
frontmatter is the project-visible machine state; the JSON file is per-machine
bookkeeping that should not sync via the vault (fail streaks, wall-clock
starts, last reply ts).

## Project markdown schema

```markdown
---
status: new            # new | building | testing | blocked | awaiting-human | done
created: 2026-06-07T14:30:00-05:00
last_heartbeat: 2026-06-07T14:30:00-05:00
repo: thehomie            # target repo from REPOSITORIES.md (or "greenfield")
greenfield_name: null            # new repo name for greenfield projects
branch: feat/<slug>              # worktree branch this project builds on
current_job_id: null             # Archon run id of the in-flight workflow, or null
active_workflows: []             # [{name, run_id, branch, dispatched_at, status}]
iterations: 0
max_iterations: 100              # hard cap -> awaiting-human
max_wall_clock_hours: 72         # wall-clock cap -> awaiting-human
completion_check: "uv run pytest"   # the EXECUTABLE signal of done (run in the worktree)
archon_provider: null            # OPTIONAL override of the SB_AGENT_BACKEND-derived provider
archon_model_tier: null          # OPTIONAL override: strong | mid | cheap
slack_channel: null              # thread root for this project's conversation
slack_thread_ts: null
---

# <Project Title>

## Spec (STATIC - orchestrator MUST NOT rewrite)
## Plan / Working Memory (MUTABLE - orchestrator may rewrite)
## Activity Log (APPEND-ONLY - newest at bottom)
```

## Status state machine

Deterministic transitions happen in Python; judgment calls happen in the LLM.

- `new` -> the LLM reads the Spec, picks and dispatches the first workflow ->
  `building`. Records `current_job_id` and `branch`. For a greenfield project,
  the orchestrator first creates the repo, clones it, makes an initial commit,
  and registers it in `REPOSITORIES.md`, then dispatches.
- `building` -> Python polls `current_job_id`:
  - `running` -> append a tiny "still running" note, leave status, skip the LLM
    for this project. This is the cheap common case.
  - `completed` -> hand to the LLM: decide the next workflow, or move to run the
    completion check -> `testing`.
  - `failed` / `cancelled` -> hand to the LLM: resume, reset, or escalate ->
    `blocked`.
- `testing` -> run the `completion_check` (executable, in the Archon worktree).
  Pass -> `done` and ping Slack. Fail -> back to `building` with a fix
  workflow; if the same test fails twice (`fail_streak >= 2`) -> `blocked`.
- `blocked` / `awaiting-human` -> wait for a Slack reply or a markdown edit. On
  human input, the LLM resumes the appropriate status.
- `done` -> terminal. The file is archived to `Co-founder Projects/done/<slug>.md`
  and the orchestrator skips it.

## Idempotency and locking (gates before any dispatch)

1. **Never dispatch if a job is already running.** If `current_job_id` is set
   and its Archon status is `running`, the only action is to append a status
   note. This is a hard gate in Python, before the LLM is ever invoked.
2. **`file_lock` the project file** while mutating frontmatter, so two
   overlapping heartbeats cannot double-dispatch.
3. **Global concurrency cap.** At most 3 Archon workflows run concurrently
   across all projects. Further dispatches queue (wait for a slot).
4. **Caps flip to `awaiting-human`, never crash.** Before any LLM iteration,
   check `iterations >= max_iterations` and wall-clock elapsed. If a cap is
   tripped, set `awaiting-human`, ping Slack once, skip. Each LLM-decision
   iteration increments `iterations`.

## Workflow-reuse heuristic

Given the live `archon workflow list --json`, the LLM decides:

- **Generic / default** (`archon-plan-to-pr`, `archon-feature-development`,
  `archon-fix-github-issue`, `archon-ralph-dag`, `archon-assist`). Prefer these
  for stack-agnostic build and fix steps. They are reusable across projects.
- **Reuse a project-specific workflow** already authored for this project
  (recorded in `active_workflows`).
- **Reuse a recently-built one** from another project if it fits.
- **Build new** only when the task needs bespoke steps no generic workflow
  covers. Author `.archon/workflows/<slug>-<purpose>.yaml`, validate it
  (`archon validate workflows <name>`), then run. Record it in
  `active_workflows`.

Rule of thumb baked into the prompt: generic workflows are reusable;
tech-stack or architecture-specific ones usually are not.

### Provider mismatch on reuse: clone-and-stamp

A reuse candidate's provider and model are baked into its YAML, and there is
no dispatch-time `--provider` / `--model` flag. So before reusing, Python
compares the candidate's workflow-level `provider:` against the resolved
thehomie provider. On a match, run it as-is. On a mismatch, the
orchestrator clones it to `.archon/workflows/<name>-<provider>.yaml`, rewrites
only the workflow-level and per-node `provider:` / `model:` fields to the
resolved values, validates the clone, and runs the variant. The clone is
deterministic: Python rewrites the frontmatter, the LLM does not retype the
body.

## Provider and model selection (follows the second brain)

When the orchestrator authors or clones a workflow, the Archon
`provider:` / `model:` block is computed in Python from the second brain's own
backend env var, never guessed by the authoring LLM. The LLM writes the
workflow body; Python stamps the provider and model frontmatter, and re-stamps
after the LLM finishes as a defense against drift.

The knob is `SB_AGENT_BACKEND` (default `claude`). The mapping:

| `SB_AGENT_BACKEND` | Archon `provider:` | strong / mid / cheap |
|---|---|---|
| `claude` (default) | `claude` | `opus` / `sonnet` / `haiku` |
| `pi` | `pi` | the same `PI_MODEL_STRONG` / `PI_MODEL_MID` / `PI_MODEL_CHEAP` refs the Pi backend reasons with |
| `codex` | `codex` | codex tier models |

Plan and architecture nodes get the `strong` tier; implement and loop nodes
get `mid`; classify and cheap nodes get `cheap`. Two gotchas the resolver and
authoring logic respect:

1. **Loop nodes ignore per-node provider/model.** They inherit the
   workflow-level frontmatter. So always set the workflow-level provider and
   model to the resolved values; never rely on a per-node override to steer a
   Ralph loop.
2. **No dispatch-time override exists**, which is exactly why reuse-on-mismatch
   has to clone-and-stamp rather than override at run time.

A project may override per-project via `archon_provider` /
`archon_model_tier` frontmatter; the resolver reads those first and falls back
to `SB_AGENT_BACKEND` only when they are null.

Flipping one env var (`SB_AGENT_BACKEND=pi`) moves both the orchestrator's own
reasoning and all newly-dispatched build work onto Pi. That is the clean close
to the June-15 Anthropic-metering motivation: one knob drives both layers.
Worth stating plainly: tying the build provider to the reasoning provider is a
policy choice (the two axes are technically independent); the per-project
override is the escape hatch when a project needs a different build model than
the global default.

There is also an operational override, `COFOUNDER_ARCHON_FORCE_MODEL`. When
set, it forces a single model across every tier. The first test run uses
`COFOUNDER_ARCHON_FORCE_MODEL=opus` (Opus everywhere) while Cole rides the
Claude subscription before June 15.

## Slack conversation model

- A dedicated channel, `#cofounder-projects`, with one thread per project.
- The first time a project needs Cole, the orchestrator sends a Slack message,
  captures `{channel, ts}`, stamps `slack_channel` / `slack_thread_ts` into the
  frontmatter, and registers the thread via `save_heartbeat_thread(...)` so the
  live chat daemon can also handle real-time replies.
- On every run, Python polls `get_thread_replies(channel, thread_ts,
  after_ts=last_reply_ts)`. Any new reply marks the project as needing LLM
  attention, passes the reply text into the orchestration prompt, and advances
  `last_reply_ts`.
- Cole can alternatively edit the markdown directly; the next run reads the
  current file, so edits are picked up for free.
- The channel stays quiet. Pings fire only on `done`, `blocked`, or
  `awaiting-human`. Routine progress goes to the markdown Activity Log only,
  so Cole's phone is not buzzing on every "still running" note.

## Safety and honesty

- Completion is the executable `completion_check`, never the agent's word.
- `git reset --hard` on a broken build happens inside the Archon worktree via
  the workflow, never in the live checkout. The orchestrator never runs
  destructive git in the project root.
- Greenfield repos are created private under `coleam00` and committed straight
  to `main` (the system owns them end to end). Pre-existing repos get a PR left
  for review; the system never auto-merges into a repo it did not create.
- The VPS GitHub token can create repos but cannot delete them (it lacks the
  `delete_repo` scope). This is treated as a safety feature, not a limitation.
  Autonomous repos use an identifiable naming convention so strays are obvious.
- The orchestration pass is fast and orchestrate-only. It dispatches Archon and
  returns; it never runs the long build inline. Every design choice (fire-and-
  forget dispatch, poll-by-job-id, short prompts, fast Python gates) serves
  keeping each run well under the 30-minute heartbeat budget.

### Honest cost framing (for the workshop)

A single Ralph loop spends more tokens than one careful session. The win is not
fewer tokens. The win is wall-clock: worktree isolation lets multiple builds
run in parallel without conflicting, so the same token spend delivers faster.
"Same wallet, faster delivery." Do not conflate worktree throughput with
multi-agent fan-out; they are different levers.

## Where this runs

The autonomous loop runs where the `archon` CLI is installed and Archon's
run-state DB is reachable: the DigitalOcean VPS (`/root/thehomie`), the
cron heartbeat host. On Cole's Windows box the dispatch path is a graceful
no-op (the `archon` CLI is not present), so the module is safe to develop and
unit-test locally; only the live dispatch happens on the VPS.

Operational facts verified on the VPS (2026-06-07):

- `archon` lives at `/root/.bun/bin/archon`, not on the non-interactive SSH
  PATH. The orchestrator prepends `/root/.bun/bin:/root/.local/bin` to the
  subprocess PATH.
- Archon keeps run-state in **SQLite** at `~/.archon/archon.db` (tables
  `remote_agent_workflow_runs`, `remote_agent_workflow_events`), not Postgres.
  The poll query reads that DB via the stdlib `sqlite3` module. The Postgres
  path is the alternate, used only if `DATABASE_URL` is set in `~/.archon/.env`.
- `archon` requires `--cwd <repo>` on every call (it errors "Not in a git
  repository" otherwise).
- A dispatched run's worktree path is read from the run row's `working_path`
  column; the completion check runs against that path. Do not reconstruct it.
- The heartbeat cron fires `*/30 * * * *` (24/7); the script self-gates active
  hours. The co-founder pass runs on every invocation, including overnight.

## First cut vs full system

**First cut (shipped now).** `cofounder_projects.py` can: discover a project,
parse its frontmatter, poll an Archon run by id (SQLite-first), read Slack
thread replies, enforce caps, gate dispatch on "no active job already
running," dispatch and monitor one Archon workflow fire-and-forget with
worktree isolation, append a short Activity Log note, and ping Slack on
`done` / `blocked` / `awaiting-human`. The heartbeat runs it inline as a
separate reasoning pass, 24/7, wrapped so its failure can never break the
normal alert. The deterministic layer is fully unit-tested with mocks.

**Full system (designed now, iterated across the week).**

- Full auto-advance through `new -> building -> testing -> done` driven by
  executable completion checks, including authoring brand-new workflows on the
  fly and marking done with no approval gates.
- The complete workflow-reuse heuristic (reuse / generic / build-new) with
  new-workflow authoring plus `archon validate`.
- Provider derivation and clone-and-stamp on mismatch, with the per-project
  override and the `SB_AGENT_BACKEND=pi` flip moving build work onto Pi.
- The Slack reply and markdown co-authoring loop fully closing run to run.
- Caps reliably parking projects at `awaiting-human`, with
  `COFOUNDER_PROJECTS_ERROR` markers surfaced in the heartbeat.

## Workshop demo (Fri Jun 12)

The first demo project rebuilds **North Star** (Cole's already-started
Phaser 3 + TypeScript + Bun indie game) in a fresh repo, scoped to just the
initial level and engine. For a game, "tests pass" does not mean "good," so the
`completion_check` is scoped to "engine plus initial level builds, typecheck
and tests pass, and a `bun run playtest` smoke succeeds," not subjective fun.
Fun and quality stay a human review after `done`, per Cole's standing North
Star rule (playtest with `bun run playtest` plus judge frames; passing tests
is not a good experience on its own).

The demo shows the full loop live: drop a spec, create the repo, auto-advance
through workflows (including authoring a new one), status updates flow to the
Activity Log and Slack, and the project reaches `done`.

## Related

- [[dark-factory-plan]] - the self-evolving-codebase sibling experiment.
- [[archon]] - the workflow engine that does the actual building.
- [[repositories/archon]] - the Archon codebase itself.
