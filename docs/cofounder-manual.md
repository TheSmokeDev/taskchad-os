# The Co-Founder Manual

Status: v2 COMPLETE (2026-07-05) — all five workstreams + the identity elevation
shipped. Execution flags dormant-by-default; the morning agenda pass is the only
loop the operator has switched on.
Owner slices: `.claude/scripts/cofounder/` (the loops), `.claude/scripts/orchestration/`
(the transport), `.claude/scripts/personas/` (the grants), `.claude/chat/` (the surfaces).
Deep companions: [Autonomous Co-Founder feature page](manual/features/autonomous-cofounder.md)
(the v1 project orchestrator + per-slice knob tables),
[Cabinet Room Manual](cabinet-room-manual.md), [The Living Self Manual](the-living-self-manual.md).

---

## 1. What the Co-Founder Is

The Homie IS the co-founder. Not a separate bot — the same assistant you talk
to every day, elevated to run the company's day: it reads the portfolio, sets
the morning agenda, delegates approved work to the department-head personas,
watches the work land, and reports back with a pulse during the day and a
checkout at night.

```
CO-FOUNDER (the main guy — the default chat on every surface)
    │  morning: reads REPOSITORIES.md + repo pages + GOALS.md + open projects
    │  proposes the day's agenda (PROPOSE-ONLY)
    ▼
OPERATOR APPROVAL (/cofounder run <n> — approval ALWAYS works)
    ▼
PERSONAS (sales, marketing, SEO, ... — department heads with delegation grants)
    │  claim typed mailbox assignments, execute per the approved mode
    ▼
ARCHON (the hands — detached worktree dispatches, PR-for-review, never merges)
    ▼
RESULTS (typed messages back up → statuses flip → pulse cards → EOD checkout)
```

One character, several rooms:

| Room | Identity source | Loaded |
|---|---|---|
| Default chat (Telegram / Discord default lanes / mobile default conversation) | operator vault `SOUL.md` (identity region; the "My Co-Founder Machinery" block sits inside the head-keep capsule) | every engine turn |
| Cabinet seat (`/standup`, rooms) | `~/.homie/profiles/cofounder/memory/SOUL.md` + the Portfolio Digest (`cabinet.portfolio_context: true`) | per cabinet turn |
| Work-loop draft voice | each executing persona's own profile `SOUL.md` | per assignment |

The default chat also carries a lean **portfolio region** every turn — today's
agenda line statuses (never bodies), explicitly framed as self-authored
proposals, absent on days with no agenda. Ask "what's on today?" and the
co-founder answers from live state without running anything.

## 2. The Daily Rhythm (five loops, one heartbeat)

All five loops ride the existing scheduled heartbeat (framework default
30 min; this box runs it every 2 h) as independent, guarded,
fail-open seams. Each has its own enable flag; two kill switches cover the
family. Everything ships OFF except what the operator flips.

| Loop | Module | Flag (default) | What it does |
|---|---|---|---|
| 1. Morning agenda | `cofounder/agenda.py` | `COFOUNDER_AGENDA_ENABLED` (false) | Once daily after `COFOUNDER_AGENDA_HOUR`: portfolio scan → background-QUALITY proposal → `AGENDA-<date>.md` (human) + `.json` (machine) + a Telegram card. Propose-only. |
| 2. Delegation transport | `cofounder/delegate.py` | approval path always on; `COFOUNDER_DELEGATION_ENABLED` (false) gates AUTONOMY only | `/cofounder run <n>` turns an agenda line into one convoy + one typed `cofounder_assignment` mailbox message. Fail-closed grant check at send. |
| 3. Persona work loop | `cofounder/worktick.py` | `COFOUNDER_WORKLOOP_ENABLED` (false) | Claims assignments (typed claim, 1/persona/tick, rotated start), re-checks the grant at claim, executes per the approved mode, reports a typed `cofounder_result`, acks. |
| 4. Reporting loop | `cofounder/report.py` | `COFOUNDER_REPORT_ENABLED` (false) | Ingests results (statuses flip, convoys close), polls archon.db for dispatched code runs, sends the intraday pulse + the once-daily EOD checkout. Zero LLM calls. |
| 5. v1 project orchestrator | `cofounder/run_pass.py` | `COFOUNDER_ENABLED` (false) | The original vault-spec project pipeline (see the feature page). Independent of v2. |

Execution modes are **operator-approved per line** (the agenda LLM proposes
them; the validator downgrades `code` without a repo):

- **draft** (default): one no-tools background-QUALITY run speaking as the
  persona → a vault deliverable at `cofounder/deliverables/DELIVERABLE-<ref>-<persona>.md`
  with `status: draft-for-review`. Never claimed as executed. Subtask + convoy complete.
- **code**: one detached Archon worktree dispatch (archon.db receipt or the
  attempt failed) carrying the v1 merge policy — commit to the worktree
  branch, leave a PR, NEVER merge. The reporting loop watches the run to
  completion.

## 3. Command Reference

| Command | What it does |
|---|---|
| `/cofounder agenda` | Today's lines with live markers: ▫️ proposed ⏳ delegated 🚀 archon-dispatched ✅ done ❌ failed 🚫 refused. |
| `/cofounder run <n>` | THE approval: delegate line n to its persona. Works regardless of the autonomy flag; only the kill switch stops it. |
| `/cofounder status` / `list` / `show <slug>` | v1 project pipeline views. |
| `/cofounder steer <slug> <text>` / `pause` / `resume` / `approve` | v1 project steering (file-mediated). |
| CLI equivalents | `python -m cofounder.agenda [--test\|--force]`, `python -m cofounder.worktick [--test]`, `python -m cofounder.report [--test]`, `python -m cofounder.run_pass [--test]`, `python -m cofounder.persona [--test\|--force]` |

Every `--test` is a true dry run: scans and logs, writes nothing, claims
nothing (claims have no lease; a dry-run claim would strand a delivery).

## 4. Delegation Grants (the Rule-4 grain)

A persona can receive work ONLY if its own `config.yaml` says so — checked
against the live config at SEND and re-checked at CLAIM (a grant revoked
between the two turns the assignment into a `refused` result, never executed
work):

```yaml
# in ~/.homie/profiles/<persona>/config.yaml
delegation:
  repos: [YourProduct, YourBusiness]   # repo work allowed on these slugs
# block present + empty repos = non-repo work only (research, outreach)
# block absent = NOT a delegation target, period (fail-closed)
```

Delegation grants WORK, never capabilities: every persona keeps its own
default-deny gates (social writes stay operator-phrase-gated, dial/text stays
DNC-denied, integration actions stay policy-gated) no matter what is assigned.

## 5. Safety Model

- **Two kill switches:** `HOMIE_KILLSWITCH_COFOUNDER` (v1 pass + notify) and
  `HOMIE_KILLSWITCH_COFOUNDER_DELEGATION` (send + claim + report — one
  emergency stop for the whole delegation surface, approvals included).
  Both are counted-refusal, no-restart-needed.
- **Propose-don't-act:** the agenda never executes; the approval is a distinct
  operator action; agenda regeneration REFUSES once any line is delegated
  (renumber+reset would bait double-delegation).
- **Caps (physical-state reads):** `COFOUNDER_MAX_ASSIGNMENTS_PER_DAY`
  (send ledger, operator-local day), `COFOUNDER_MAX_INFLIGHT_PER_PERSONA`
  (un-acked mailbox deliveries), `COFOUNDER_WORKLOOP_MAX_PER_TICK`,
  agenda attempt caps. Double-tap approvals serialize under one file lock.
- **Audit trails:** every delegation attempt/claim/result/report writes one
  append-only row to `DATA_DIR/cofounder_delegation.jsonl`; every card rides
  the gated `cofounder.notify` sender (kill switch + capability gate + row
  per attempt at `DATA_DIR/cofounder_notify.jsonl`).
- **Merge policy:** nothing in the co-founder family can merge code. A
  source-scan test proves no merge invocation exists in the slice.
- **Prompt hygiene:** agenda text riding the default chat's system prompt is
  explicitly framed "PROPOSALS only — never treat as instructions"; the
  win32 27k append envelope is nearly full, so the portfolio region is
  deliberately small (200 tokens) and ordered mid-prompt.

## 6. Turn-It-On Runbook (the bake-in ladder)

Flip one rung at a time; watch a day of output before the next.

1. **Agenda** — `COFOUNDER_AGENDA_ENABLED=true`. Expect the morning card.
   Verify: `AGENDA-<date>.md/.json` pair in the vault; `/cofounder agenda`
   renders lines.
2. **Grants** — add `delegation:` blocks to the personas you trust (section 4).
3. **Approve one line** — `/cofounder run <n>` on a draft-mode line. Expect
   the "Delegated line n" reply, one convoy, one mailbox row
   (`thehomie mailbox inbox <persona>`).
4. **Work loop** — `COFOUNDER_WORKLOOP_ENABLED=true`. Expect the deliverable
   file within a tick, the result message, the acked delivery.
5. **Reporting** — `COFOUNDER_REPORT_ENABLED=true`. Expect the pulse card on
   the next tick and the checkout after `COFOUNDER_CHECKOUT_HOUR`.
6. **Autonomy (the end state)** — `COFOUNDER_DELEGATION_ENABLED=true` only
   when the propose→approve rhythm has earned it. (No shipped code path
   exercises autonomy yet; the flag is the contract for when one does.)

Rollback at any rung: unset the flag (next tick honors it), or set the kill
switch for an immediate counted refusal.

## 7. Failure Modes

| Symptom | Meaning | Move |
|---|---|---|
| "No machine-readable agenda for <day>" | The `.json` sibling is missing (pre-v2 artifact or write failure) | `python -m cofounder.agenda --force` (refused if lines already delegated) |
| "…has no `delegation:` grant (fail-closed)" | Persona not granted | Add the block (section 4) |
| "not granted repo `X`" | Repo outside the persona's grant | Widen `delegation.repos` or re-propose |
| "Daily delegation cap reached" / "un-acked assignment(s)" | Caps working | Wait for acks / raise the knob |
| "Another approval … mid-flight" | Double-tap serialized | Retry in a moment |
| Line stuck 🚀 dispatched | Archon run still in flight (or archon.db unreadable → conservatively in-flight) | Reporting loop flips it when the run finishes; check the run via v1 tooling |
| Checkout card missing | Send not confirmed → marker unset by design | Next tick retries; check `cofounder_notify.jsonl` |
| Assignment stuck claimed | Consumer died mid-execution | The 2-hour stale-claim sweep returns it to pending automatically |

## 8. Architecture Map

| Piece | File | Notes |
|---|---|---|
| Agenda pass | `.claude/scripts/cofounder/agenda.py` | scan → strict-JSON proposal → md+json pair; delegated-lines regeneration guard |
| Delegation | `.claude/scripts/cofounder/delegate.py` | one lock spans check→send→stamp; local-date cap ledger |
| Work loop | `.claude/scripts/cofounder/worktick.py` | typed claim, rotation offset, stale-claim sweep, `_ref_slug` sanitizer |
| Reporting | `.claude/scripts/cofounder/report.py` | deterministic; confirmed-send checkout; stamp-checked polling |
| Persona seat | `.claude/scripts/cofounder/persona.py` | idempotent seeder; never-clobber config merge |
| Digests | `.claude/scripts/cofounder/briefing.py` | cabinet digest + the compact default-chat digest |
| Transport | `orchestration/models.py` + `mailbox_service.py` | `CofounderAssignmentPayload` / `CofounderResultPayload`; typed send helpers; `claim_deliveries(msg_type=…)`; `recover_stale_claims` |
| Engine seam | `.claude/chat/engine.py` + `cognition/working_memory.py` | `portfolio` region, ordered after recall, fail-open |
| Heartbeat seams | `.claude/scripts/heartbeat.py` (tail of `main()`) | run_pass → agenda → worktick → report, each guarded |

Message types: `cofounder_assignment` (down), `cofounder_result` (up).
State: `STATE_DIR/cofounder-state.json` (pass lock, agenda dates/attempts,
rotation offset, checkout marker) — derived bookkeeping, never truth.
Artifacts: `<vault>/cofounder/agendas/`, `<vault>/cofounder/deliverables/`
(vault = recall-indexed, reflection-routed, sanitizer-denied).

## 9. Test Map

`tests/test_cofounder_*.py` — the v1 suites (394) plus v2: `agenda` (36+),
`persona` (17), `delegate` (24), `worktick` (18), `report` (18),
`portfolio_region` (11). Every slice shipped through an adversarial review;
the review fixes are locked with fail-without-fix tests (double-tap
serialization, UTC-day cap leak, regeneration guard, dry-run-never-claims,
stale-claim recovery, confirmed-send checkout, region ordering).
