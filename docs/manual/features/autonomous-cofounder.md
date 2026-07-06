# Autonomous Co-Founder

Status: Shipped default-OFF (US-001..US-020); Phase 9 first-project live validation is the operator's flip
Owner: `.claude/scripts/cofounder/` (orchestrator slice), `.claude/chat/` (command surface)
Last updated: 2026-07-04

## What It Does

The autonomous co-founder is a project orchestrator layered on the existing
30-minute heartbeat. The operator drops one markdown spec per project into the
watched vault folder (`vault/memory/cofounder/`); a pass riding the
heartbeat carries each project forward between check-ins by dispatching
detached Archon workflow runs into isolated worktrees, polling the Archon
run-state SQLite read-only, running the project's executable completion check
in the build worktree, and pinging Telegram only on terminal flips
(done / blocked / awaiting-human). Everything mechanical (caps, gates,
polling, completion) resolves in pure Python before any model call; the LLM
runs only when a real decision remains, on the background QUALITY tier.

## Operator Entry Points

- Chat/Telegram: the `/cofounder` command family plus inline `pause` /
  `approve` buttons on notification cards.
- CLI: `cd .claude/scripts && uv run python -m cofounder.run_pass [--test] [--project <slug>]`
- Scheduler: a guarded post-step at the end of `heartbeat.py main()`,
  deliberately OUTSIDE the active-hours gate so builds advance overnight.
- Vault: project files in `vault/memory/cofounder/`; the always-loaded
  index doc `vault/memory/COFOUNDER-PROJECTS.md` (ownership rules, worked
  example, auto-refreshed active-projects list).

## Source Of Truth Files

| Layer | Files |
|---|---|
| Python/runtime | `.claude/scripts/cofounder/` (project_model, repos, state, status, engine_archon, orchestrate, workflow_author, notify, briefing, run_pass); `config.get_cofounder_settings()`; the heartbeat seam at the end of `heartbeat.py main()`; `security/kill_switches.py`; `integrations/capabilities.py` (`cofounder.notify`); `runtime/bootstrap.py` (session briefing); `memory_reflect.py` (reflection routing) |
| Chat/router | `.claude/chat/core_handlers.py` (`handle_cofounder`), `.claude/chat/commands.py` (four registration points), `.claude/chat/router.py` (`cofounder:` button branch) |
| Tests | `.claude/scripts/tests/test_cofounder_*.py` (395 tests incl. one opt-in live-detachment skip) |
| Docs/proof | This page; `vault/memory/COFOUNDER-PROJECTS.md` (index doc); `vault/memory/cofounder/_template.md` |

## Environment Knobs

All knobs live in `.claude/scripts/.env` and resolve at CALL time through
`config.get_cofounder_settings()` (Rule 1 - no restart needed for most, but
the chat bot reads config at import, so command-surface changes want a bot
restart).

| Knob | Default | What it does |
|---|---|---|
| `COFOUNDER_ENABLED` | `false` | Master enable. Ships OFF; flipping to `true` is the operator's Phase 9 action. |
| `COFOUNDER_PROJECTS_DIR` | `<memory>/cofounder` | Watched vault folder, one markdown file per project. Sanitizer-denied; never exports. |
| `COFOUNDER_MAX_ITERATIONS` | `50` | Per-project dispatch cap before the status flips to awaiting-human. Seeds new files; each file's own frontmatter is the enforced truth. |
| `COFOUNDER_MAX_WALL_CLOCK_HOURS` | `72` | Per-project wall-clock cap from first dispatch before parking awaiting-human. |
| `COFOUNDER_MAX_CONCURRENT` | `2` | In-flight build cap across all projects; excess projects queue in discovery order and burn no LLM call. |
| `COFOUNDER_NOTIFY_LEVELS` | `done,blocked,awaiting-human` | Levels allowed to send a Telegram ping. Empty string disables all notifications. |
| `COFOUNDER_ZOMBIE_STALE_MINUTES` | `60` | DB-staleness half of the two-signal zombie rule (the other half is no worktree mtime growth across a full pass). |
| `COFOUNDER_ARCHON_DB` | `~/.archon/archon.db` | Archon run-state SQLite, polled READ-ONLY (`file:...?mode=ro`). The orchestrator can never write it. |
| `COFOUNDER_WORKFLOW_PROVIDER` | `claude` | Backend provider CODE stamps into every authored workflow YAML (workflow level AND every loop node), re-stamped after each pass. |
| `COFOUNDER_WORKFLOW_MODEL` | `sonnet` | Model half of the same backend stamp. |
| `HOMIE_KILLSWITCH_COFOUNDER` | unset (enabled) | Set to `disabled` to refuse the pass AND the notify sender, with counted refusals. Read per call - toggling back re-enables without a restart. `/cofounder pause` and `steer` deliberately keep working while the switch is off (they are the operator's stop controls). |

## Status Enum And Ownership Rules

Status: `new | building | testing | blocked | awaiting-human | done`.
`done` is the only terminal status (project archives to `done/`); `blocked`
and `awaiting-human` are parked and wake only on operator steering. Any
non-enum string an LLM invents (for example `in_progress`) is tolerated as an
ACTIVE build so polling never stalls; code only ever writes the six enum
values.

Every project file has three sections with hard ownership:

| Section | Owner | Rule |
|---|---|---|
| `## Spec` | Operator | STATIC. No public writer exists in the orchestrator; a rewrite attempt raises. |
| `## Plan / Working Memory` | Orchestrator | MUTABLE. The decision step may replace it. |
| `## Activity Log` | Both | APPEND-ONLY, newest at the bottom. Steering lands here as `[steer]` lines. |

The orchestrator-only rule: a chat reply or a project-file edit is an
INSTRUCTION to the orchestrator. Never build a co-founder project inline in a
chat or coding session.

## /cofounder Commands

| Command | What it does |
|---|---|
| `/cofounder status` | Pass-level summary of discovered projects. |
| `/cofounder list` | List project slugs with status. |
| `/cofounder show <slug>` | One project's frontmatter + recent activity. |
| `/cofounder steer <slug> <text>` | Append a timestamped `[steer]` line the next pass consumes. |
| `/cofounder pause <slug>` | Flip to awaiting-human (prior status stashed). |
| `/cofounder resume <slug>` | Restore the stashed prior active status. |
| `/cofounder approve <slug>` | Complete a subjective-gate park: awaiting-human flips to done + archive. |

Notification cards carry inline buttons (`cofounder:pause:<slug>`,
`cofounder:approve:<slug>`) that execute the exact same code path as the
typed commands, through the same admin role gate.

## Merge Policy

Default-deny. Every dispatch into a pre-existing repo appends the
PR-for-review instruction to the build message: the build commits only to its
assigned worktree branch and leaves a pull request for operator review. The
orchestrator itself NEVER merges anything - a source-scan test proves no
merge invocation exists anywhere in `.claude/scripts/cofounder/`, and no
knob for automatic merging exists in v1 (adding one later is its own PRP with
its own gate). Greenfield (system-owned) repos may commit straight to their
default branch.

## Safety Boundaries

- Telegram sends go through the `cofounder.notify` IntegrationAction
  (default-deny capability gate) with one audit row per send attempt at
  `.claude/data/cofounder_notify.jsonl`, plus the kill switch on top.
- Dispatches are detached (`shared.spawn_detached`) with `CLAUDECODE*`
  scrubbed from the child env; the archon.db row is the ONLY dispatch
  receipt - no row within the ~90s grace window means the attempt is failed
  and no phantom `building` state is ever stamped.
- The agent's self-report never counts as completion; only the project's
  executable `completion_check`, run in the build worktree, can flip
  `testing` to `done` (or park for a human verdict when
  `subjective_gate: true`).
- A co-founder failure never breaks the heartbeat (guarded seam, failures
  append to `heartbeat_errors.log`); an unreadable archon.db degrades to
  `unknown` and refuses dispatch conservatively.

## Observability

Each project's pipeline runs inside a `cofounder_pass` dual-lane span
(Langfuse + the observation jsonl) with metadata
`{project, action, status_flip, latency_ms}` - strictly fail-open. Note:
when `LANGFUSE_ENABLED=true` points at a dead server, the OTEL exporter
retries cost roughly 4 seconds per project per pass (bounded by the export
timeout). Set `LANGFUSE_ENABLED=false` while the Langfuse server is down.

## How To Run It

```powershell
# Dry run: full discovery + decision logging, zero dispatch/notify/writes
cd .claude/scripts; uv run python -m cofounder.run_pass --test

# One real pass, one project
cd .claude/scripts; uv run python -m cofounder.run_pass --project <slug>

# Production: no action needed - the pass rides every heartbeat once
# COFOUNDER_ENABLED=true is set in .claude/scripts/.env
```

## How To Test It

```powershell
cd .claude/scripts; uv run pytest tests/ -k cofounder -x
# Ship-gate slice only (kill switch both directions + full-loop --test smoke):
cd .claude/scripts; uv run pytest tests/test_cofounder_ship_gate.py -x
```

## Phase 9 First-Project Runbook

1. Pick a real but low-stakes first spec. Good candidates: an
   already-tracked follow-up in a tracked repo with an objective check
   (`npm run build` + tests) to exercise the PR-for-review policy, or a
   content/demo improvement with `subjective_gate: true` to exercise the
   human-verdict path.
2. Copy `vault/memory/cofounder/_template.md` to `<slug>.md`, fill the
   Spec, set `repo:` to a REPOSITORIES.md slug (or `greenfield`), and set
   `completion_check:` to the executable proof.
3. Set `COFOUNDER_ENABLED=true` in `.claude/scripts/.env`.
4. Kill-switch check, both directions: set
   `HOMIE_KILLSWITCH_COFOUNDER=disabled`, run a pass, confirm the quiet
   refusal (and the counted refusal in the kill-switch counters); unset it
   and confirm the next pass runs. No restart is required either way.
5. Watch the first pass dispatch: `uv run python -m cofounder.run_pass
   --project <slug>`, then confirm the archon.db row and the stamped
   `current_job_id`.
6. Detachment proof (the kill-the-heartbeat test): while the build runs,
   kill the heartbeat/pass process and confirm the Archon run keeps writing
   to its worktree and its `last_activity_at` keeps advancing. The build
   must survive its parent.
7. Confirm the done/blocked/awaiting-human ping arrives on Telegram and that
   the card's pause/approve buttons steer the project.
8. Ship gate: one project driven new -> building -> testing -> done (or
   awaiting-human on a subjective project) with zero operator intervention
   besides the final verdict.

## Fresh-Session Acceptance Script

Run on BOTH Claude Code and Telegram, in a completely fresh session, with
zero coaching:

> "Mark `<project>` in progress and add a task to the plan."

The agent must edit the project file per the ownership rules: update the Plan
section, append an Activity Log line, never touch the Spec, and never start
building the project inline. If it needs coaching, discoverability
(COFOUNDER-PROJECTS.md, the session briefing, CLAUDE.md section) has
regressed.

## Latest Live Proof

- Date: 2026-07-04
- Surface: pytest ship gate (`tests/test_cofounder_ship_gate.py`) - kill
  switch both directions on both surfaces, default decider wiring, and the
  full-loop `--test` smoke (decide-without-dispatch, zero writes, zero HTTP).
- The live first-project loop (Phase 9) is operator work and intentionally
  not claimed here.

## Public Export Status

The orchestrator slice and this page are public-framework safe. Project
files themselves live under the memory vault and are sanitizer-denied; they
never export.

## Co-Founder v2 — Morning Agenda (WS2, propose-don't-act)

Status: Shipped default-OFF (2026-07-05). The first v2 slice: a once-daily
portfolio scan that PROPOSES persona->repo assignments and never executes
anything. Delegation (convoy/mailbox assignments, WS3+) is a separate slice
behind its own flag and operator approval.

**What it does:** the first heartbeat pass on/after `COFOUNDER_AGENDA_HOUR`
(local) reads the portfolio — the repository index + each repo page's
`## Dispatch History` / `## Recent Activity` tails, `GOALS.md`, the open
co-founder projects, and the registered persona roster — and has the
background QUALITY tier propose a daily agenda: which persona should work
which repo on what, and why. Output is one vault artifact
(`<projects_dir>/agendas/AGENDA-YYYY-MM-DD.md`, frontmatter
`status: proposed`, propose-only banner) plus one gated Telegram card
(no inline buttons — there is no project to steer).

**Validation is fail-closed per line:** a proposed line naming a persona not
in the registry or a repo not in the index is dropped with a warning — the
model cannot invent delegation targets. Garbage output is a counted failed
attempt (capped per day) with no artifact and no card.

**Gates (in order):** the shared `cofounder` kill switch ->
`COFOUNDER_AGENDA_ENABLED` (default `false`) -> once-daily due check ->
per-day failed-attempt cap. An empty `COFOUNDER_NOTIFY_LEVELS` (the v1
global-mute convention) also mutes the agenda card;
`COFOUNDER_AGENDA_NOTIFY=false` mutes only the card.

| Knob | Default | What it does |
|---|---|---|
| `COFOUNDER_AGENDA_ENABLED` | `false` | Master enable for the agenda pass — independent of `COFOUNDER_ENABLED` so v2.0 can bake while the v1 pipeline stays dormant (and vice versa). |
| `COFOUNDER_AGENDA_HOUR` | `7` | Earliest local hour the daily scan may run. |
| `COFOUNDER_AGENDA_MAX_ITEMS` | `5` | Cap on proposed agenda lines. |
| `COFOUNDER_AGENDA_MAX_ATTEMPTS` | `3` | Failed proposal attempts per day before the pass goes quiet until tomorrow. |
| `COFOUNDER_AGENDA_NOTIFY` | `true` | Send the agenda Telegram card (kill switch + capability gate + audit row still apply). |

```powershell
# Dry run: full scan + proposal logging, zero writes/cards
cd .claude/scripts; uv run python -m cofounder.agenda --test

# Regenerate today's agenda on demand (due check skipped; gates still apply)
cd .claude/scripts; uv run python -m cofounder.agenda --force
```

Tests: `tests/test_cofounder_agenda.py` (34 — gates, scan fail-open, strict
parse/fail-closed validation, artifact/discovery isolation, dry-run,
card muting, Rule-1 config).

## Co-Founder v2 — The Cofounder Persona (WS1)

Status: Shipped 2026-07-05. The cofounder becomes a first-class persona —
someone you talk to in the Cabinet, not just a pipeline.

**Seeding (idempotent, operator-run):**

```powershell
cd .claude/scripts; uv run python -m cofounder.persona          # seed/refresh
cd .claude/scripts; uv run python -m cofounder.persona --test   # dry run
cd .claude/scripts; uv run python -m cofounder.persona --force  # re-author identity
```

Creates `~/.homie/profiles/cofounder/` through the standard
`personas.lifecycle.create_profile` path (`persona_mutation` kill switch +
audit apply; `no_alias` — cabinet/chat + `/cofounder` are his surfaces, he
gets no CLI wrapper). The seeder:

- writes `config.yaml` blocks **for missing keys only** (strict-read RMW —
  a malformed file is an error, never silently wiped; operator edits always
  win): `persona` (id/name/role), `cabinet` (presence = cabinet-eligible,
  `tools: []` default-deny, `portfolio_context: true`), `learning.enabled`.
- authors `SOUL.md`/`MEMORY.md` only when missing, empty, or still the
  generic lifecycle scaffold; `--force` re-authors deliberately.

**Portfolio context injection (the WS1 seam):** cabinet participant turns
are no-tools by design, so a persona whose config declares
`cabinet.portfolio_context: true` gets a **Portfolio Digest** injected into
its turn context by `cabinet/text_orchestrator._profile_execution_context`:
the newest agenda artifact (frontmatter stripped), the active co-founder
projects block, and the tracked repo slugs — built by
`cofounder.briefing.build_portfolio_digest` (capped ~2400 chars, fail-open:
a broken digest is a bare turn, never a failed turn). The flag is
declarative and persona-agnostic — no id is hardcoded; the schema validator
(`personas/services._validate_cabinet_section`) type-checks it as bool.

**What this changes operationally:** after seeding, `/standup` includes the
Co-Founder (roster auto-snapshots cabinet-eligible personas at meeting
create) and he answers with portfolio state from the digest. Registering
the persona grants NO capability — cabinet turns stay default-deny
no-tools, delegation (WS3+) stays behind its own flag, and every existing
mutation gate is untouched.

Tests: `tests/test_cofounder_persona.py` (16 — kill switch, idempotency,
never-clobber, operator-keys-win, malformed-config error, roster
eligibility, digest content/degradation, injection on/off/fail-open,
validator).

## Co-Founder v2 — Delegation Transport (WS3)

Status: Shipped 2026-07-05. An APPROVED agenda line becomes real assigned
work: one convoy (the work record) + one typed `cofounder_assignment`
mailbox message delivered to the persona — the existing orchestration
service layer, no new store.

**The approval contract (operator resolution #4):** `/cofounder run <n>`
(or the "run it" reply naming a line) ALWAYS executes —
`COFOUNDER_DELEGATION_ENABLED` (default `false`) gates only AUTONOMOUS
delegation, which nothing exercises yet. The `cofounder_delegation` kill
switch (`HOMIE_KILLSWITCH_COFOUNDER_DELEGATION`) is the emergency stop for
the whole surface, approvals included.

**The fail-closed grain (Rule 4):** a persona is a delegation target only
when its own `config.yaml` carries a `delegation:` block; repo work
additionally requires the slug in `delegation.repos`. Checked at SEND time
against the live config. Delegation grants WORK, never capabilities — every
existing default-deny gate on the persona is untouched.

```yaml
# in ~/.homie/profiles/<persona>/config.yaml
delegation:
  repos: [YourProduct, YourBusiness]
```

**Caps (both physical-state reads):** `COFOUNDER_MAX_ASSIGNMENTS_PER_DAY`
(default 5, counted from the send ledger's `local_date` field — the
operator-local `HEARTBEAT_TIMEZONE` day, never the UTC timestamp) and
`COFOUNDER_MAX_INFLIGHT_PER_PERSONA` (default 1, counted from un-acked
`cofounder_assignment` mailbox deliveries — WS4's ack releases the slot;
an unreadable mailbox refuses conservatively).

**Concurrency + regeneration safety (adversarial-review hardening):** one
file lock spans the whole check→send→stamp sequence, so a Telegram
double-tap serializes (the second call returns a friendly "mid-flight"
busy message, then "already delegated"). And once any line of the day is
delegated, `cofounder.agenda --force` REFUSES to regenerate the pair
(outcome `delegated-lines-exist`) — a rewrite would renumber lines and
reset statuses; tomorrow starts fresh.

**Surfaces:** the morning agenda now writes a machine-readable
`AGENDA-YYYY-MM-DD.json` sibling; `/cofounder agenda` lists the day's lines
with live status (▫️ proposed / ✅ delegated); `/cofounder run <n>`
approves line n. Every attempt — sent, refused, scope-denied, capped,
error — appends one row to `DATA_DIR/cofounder_delegation.jsonl`.

**What happens after a send:** the assignment sits in the persona's mailbox
(pending → claimed → acked). The WS4 persona work loop is the consumer —
until it ships, delegated lines are visible via
`thehomie mailbox inbox <persona>` and the convoy record.

Tests: `tests/test_cofounder_delegate.py` (18 — kill switch, scope
fail-closed ×4, both caps + conservative inbox failure, real-services happy
path with payload round-trip + ledger + artifact stamp + idempotence,
error wrap, surfaces, validator).

## Co-Founder v2 — Persona Work Loop (WS4)

Status: Shipped default-OFF (2026-07-05). The genuinely new runtime piece:
approved assignments get EXECUTED by the personas.

**What a tick does** (rides every heartbeat; `COFOUNDER_WORKLOOP_ENABLED`
default `false`, shared `cofounder_delegation` kill switch): for each
persona whose config carries a `delegation:` block, claim its
`cofounder_assignment` mailbox deliveries (typed claim — foreign message
types stay pending for their real consumers), RE-check the delegation
scope at claim against the live config (Rule 4's second half — a grant
revoked after send becomes a `refused` result, never executed work), then
execute per the OPERATOR-APPROVED `mode` carried in the payload:

- **`draft`** (default): one direct, no-tools runtime run on the background
  QUALITY tier speaking as the persona (its SOUL + the repo page's
  operating notes + the task). Output lands as a vault deliverable
  (`vault/memory/cofounder/deliverables/DELIVERABLE-<ref>-<persona>.md`,
  frontmatter `status: draft-for-review`) — recallable and reflectable. The
  subtask completes; the single-subtask convoy completes with it.
- **`code`**: one detached Archon worktree dispatch through v1's
  `engine_archon.dispatch` (archon.db receipt or the attempt failed),
  carrying v1's PR-for-review merge policy. WS4 reports `dispatched` and
  stamps the branch on the subtask; run-completion tracking is WS5's.

The mode is proposed by the agenda LLM (validator: `code` requires a repo,
else downgraded to `draft`) and shown in `/cofounder agenda`
(`[P1|draft]`) — **your `/cofounder run <n>` approval covers the mode**.

Every outcome: one typed `cofounder_result` back to the cofounder (WS5's
input), the delivery acked (releasing the in-flight cap slot — failed
drafts are acked too, so a poison assignment can't loop; re-delegate from a
fresh agenda line), one delegation-ledger audit row (`worktick-<status>`),
and one daily-log line that the shipped reflection routing carries onto the
repo page (the compounding loop).

**Dry runs never claim** (claims have no lease expiry — a `--test` claim
would strand the assignment); `--test` reads the inbox and logs
would-execute.

| Knob | Default | What it does |
|---|---|---|
| `COFOUNDER_WORKLOOP_ENABLED` | `false` | Master enable for the work loop. |
| `COFOUNDER_WORKLOOP_MAX_PER_TICK` | `2` | Assignments executed per heartbeat tick across all personas. |
| `COFOUNDER_WORKLOOP_CODE_WORKFLOW` | `archon-ralph-dag` | Archon workflow for `mode: code` dispatches. |

```powershell
cd .claude/scripts; uv run python -m cofounder.worktick --test   # read-only dry run
cd .claude/scripts; uv run python -m cofounder.worktick          # one real tick
```

Tests: `tests/test_cofounder_worktick.py` (13 — gates, typed-claim
isolation, dry-run-never-claims, Rule-4 revocation at claim, draft
round-trip incl. convoy completion + ledger + daily log + persona voice in
prompt, code dispatch-and-report + no-receipt failure, per-assignment
containment, Rule-1 config).

## Co-Founder v2 — Reporting Loop (WS5)

Status: Shipped default-OFF (2026-07-05). The last slice — the circle closes:
morning agenda → your approval → persona execution → **results back up,
statuses flipped, and you get the pulse + the end-of-day checkout**.
Fully DETERMINISTIC — zero LLM calls.

**What a pass does** (rides every heartbeat; `COFOUNDER_REPORT_ENABLED`
default `false`, shared `cofounder_delegation` kill switch):

1. **Ingest** — claims the personas' typed `cofounder_result` messages
   (typed claim + the same stale-claim recovery as the work loop), flips the
   agenda JSON line (`delegated → done | failed | refused | dispatched`,
   result summary/deliverable/run metadata stamped), fails the convoy
   subtask for failed/refused work, acks.
2. **Poll** — recent agenda lines stuck at `dispatched` (code-mode Archon
   runs) get one read-only archon.db check each; finished runs flip to
   `done`/`failed` and complete/fail their subtasks.
3. **Intraday pulse** — one batch card per tick when anything changed
   (✅/🚀/❌/🚫 lines with summaries + deliverable paths).
4. **EOD checkout** — once daily on/after `COFOUNDER_CHECKOUT_HOUR`: agenda
   lines by status, deliverables, delegations spent vs the daily cap.

`/cofounder agenda` markers now reflect the full lifecycle: ▫️ proposed,
⏳ delegated (awaiting execution), 🚀 dispatched (Archon in flight),
✅ done, ❌ failed, 🚫 refused.

| Knob | Default | What it does |
|---|---|---|
| `COFOUNDER_REPORT_ENABLED` | `false` | Master enable (dormant family). |
| `COFOUNDER_REPORT_NOTIFY` | `true` | Send pulse/checkout cards (an emptied `COFOUNDER_NOTIFY_LEVELS` still mutes everything). |
| `COFOUNDER_CHECKOUT_HOUR` | `18` | Earliest local hour for the daily checkout card. |
| `COFOUNDER_REPORT_POLL_DAYS` | `7` | Agenda days scanned for still-dispatched runs. |

```powershell
cd .claude/scripts; uv run python -m cofounder.report --test   # read-only dry run
cd .claude/scripts; uv run python -m cofounder.report          # one real pass
```

Tests: `tests/test_cofounder_report.py` (16 — gates, ingest round-trips
incl. subtask failure + garbage containment, dry-run-never-claims, poll
flip/conservative-hold, card muting contracts, checkout hour/once-daily,
Rule-1 config).

## Next Slices

- Phase 9 live validation: the operator's first-project loop (flip
  `COFOUNDER_ENABLED=true`, drive one spec new -> building -> testing ->
  done, run the kill-the-heartbeat detachment proof and the fresh-session
  acceptance script above).
- Greenfield dispatch: `repo: greenfield` currently resolves but a dispatch
  needs a local path, so greenfield projects no-op with a warning; wiring a
  scaffold step is its own slice.
- Authored-workflow adoption: an authored workflow is picked up from the
  repo's `archon workflow list` on the next pass rather than auto-stamped
  into the project's `archon_workflow:` frontmatter.
- v2 delegation (WS3-WS5) per
  `PRDs/active/PRD-cofounder-v2-persona-delegation-2026-07-05.md` and its
  2026-07-05 operator resolutions: convoy/mailbox delegation with per-line
  "run it" approval working even while `COFOUNDER_DELEGATION_ENABLED=false`,
  the persona work loop, the reporting loop (intraday awareness +
  end-of-day check-out), and WS6 persona-creation proposals
  (operator-approval-gated). WS1 prerequisite for delegation targets:
  complete the half-created `outbound` profile (no `persona:` section) and
  add example-client to `REPOSITORIES.md`.
