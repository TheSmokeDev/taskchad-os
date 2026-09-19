# Autonomous Persona Harness Learning

Status: Harness introduced in v1.8.0; continuous cognition introduced in v1.9.0
Owner: Framework (personas, cognition, runtime, scheduler, and dashboard)
Last updated: 2026-09-13

Each persona can develop its knowledge, understanding of its capabilities, and
working methods through experience and study. The harness records expectations,
checks observable outcomes, evaluates proposed improvements on separate cases,
and adopts or revises methods automatically. Learning belongs to the persona and
persists across model changes. Fine-tuning is not required.

Continuous cognition makes understanding and unfinished investigations durable,
even when no trade, external action, or new procedure occurs. The host invokes
real model reasoning at meaningful lifecycle boundaries; code schedules and
records the reasoning rather than deciding what the persona must conclude.

The function hooks belong to the Homie framework. Switching the same persona
between Codex, Kimi, Claude, or another configured runtime keeps its understanding,
questions, and learning history. Claude Mods are an optional event adapter. When
a provider is unavailable, pending reasoning remains visible and resumes through
the configured runtime policy; a model switch does not create a new learner.

This chapter covers daily operation. Use the
[developer guide](persona-harness-learning-developer.md) to connect another
surface or domain, and [Universal Persona Cognition](universal-persona-cognition.md)
for the wider memory, reflection, and dream system.

## Unified Reflection, Dreaming, And Recall Tuning

The unified implementation uses the existing persona journal and queue for
reflection, dream consolidation, method qualification, and retrieval tuning.
Inspect the running installation's version before expecting these additive
surfaces; this contract is not a claim of deployment or provider success.

```sh
thehomie profile learning lifecycle default --json
thehomie profile learning history default --kind synthesis_cycle --json
thehomie profile learning history default --kind synthesis_request --json
thehomie evolve status --persona default --json
thehomie evolve tune --persona default --json
thehomie evolve rollback --persona default --json
```

The Learning tab shows each synthesis admission, deferred queue stage, input
source revision and character range, output IDs, and recorded model calls.
`context_only` reorientation means context assembly, not a thinking call.
Completed stages do not establish model usage without an execution receipt.
Pending consumers cover admitted cycles; viewing the page does not scan source
files, call a model, or create missing profile state.

An execution with `output_status: invalid_contract` records a completed model
response that could not be retained. Its cognitive cycle includes a bounded
`cognitive_output_rejected` diagnostic. A later attempt receives the correction
and the exact permitted citation IDs; the invalid response does not become a
belief. Count retained understanding separately from completed model calls.

Follow-ups prefer the observer belonging to the host-captured activity domain.
They can also use a newer revision already captured in the persona's own journal,
even when the question uses a descriptive domain label. Missing revisions remain
pending. Used revisions and older observations cannot become fresh evidence on a
later poll. This does not authorize a platform action or create missing outcomes.

Reflection no longer treats routine status changes or another interpretation of
already-consumed root evidence as fresh experience. Identical active questions
with the same evidence and trigger share an investigation; distinct questions
remain separate. Dreaming can still examine substantively changed understanding,
and omitted source excerpts remain eligible for later consolidation.

Reflection and dreams use changed understanding, unfinished questions,
counterevidence, episodes, and original observations. Generated conclusions are
derived context, not additional independent observations. Consumption advances
only after successful retention of the exact source revision and excerpt.
Partial and omitted episode portions remain eligible. An inference completed
before an interruption is reused by the next retention attempt.

Session-end capture has a durable v3 outbox. It retains the accepted redacted
source before relying on the learning database, then replays bounded excerpt
batches after a restart or outage. A `source_fully_retained` receipt proves the
source excerpts and queued cycles exist; it does not claim the cycles finished.
Long sessions are not considered consumed merely because a prefix was reviewed.
Message-origin references survive both supported session backends, keeping the
same message from becoming new independent proof in another collection window.

For foreground work, inspect `context_delivery` and the actual runtime receipt:
selected context starts `prepared`; verified reasoning with the matching retained
context hash can establish `executed` inclusion. The final response has its own
execution receipt. Context-only reorientation never increases model-call counts.

Recall tuning has separate case, corpus, run, evaluation, and policy records.
It requires at least 60 validated relevance cases, split by source family into
at least 30 development and 12 held-out cases. Unlabeled diagnostic goldens do not qualify.
The worker selects at most six deterministic neighbors on development data and
evaluates the chosen winner once on held-out data with the same context budget.
Adoption requires a positive paired 95% relevance-gain bound, no added errors or
protected/isolation regressions, and candidate p95 latency at most 1.25 times
baseline. No eligible improvement is a valid no-change result. Batches are
limited to one per persona per day with new validated material.
Frozen regression checks monitor accepted policies and restore the recorded
predecessor when their gates fail; inspect the policy history for that receipt.

Use **Request tuning run** for explicit queue admission and **Roll back recall
policy** to restore a predecessor policy after confirmation. Both share Python
controls with the CLI. Ordinary reads remain GET-only. Explicit operator recall
settings take precedence over learned policy. Tuning never changes models,
permissions, persona scope, embedding models, backends, or context budgets.

Observations, understanding changes, method qualification, and recall tuning
remain separate report counts. A policy activation is not a learned idea or an
adopted method. Neither a held-out win nor context inclusion proves live benefit.

## What Each Homie Can Learn

The main Homie and specialists share the same lifecycle. Their identity, actual
work, and available evidence determine the subject. These are examples of what
the framework can retain, not claims that every persona has learned them already.

| Persona | Possible retained understanding | Evidence to revisit |
|---|---|---|
| Main Homie | An operator preference, a recurring task failure, a mistaken assumption about its own capabilities | Later corrections, task results, or observed service behavior |
| Sales | When a particular objection or diagnostic question matters | Comparable conversations, replies, and verified outcomes |
| Market specialist | A bounded chart interpretation, a source's observed claim, or an unresolved price-level question | Fresh closed candles, indicator changes, or subsequent source revisions |
| Other specialists | Domain concepts, successful examples, mistakes, uncertainty, and gaps discovered during work | Their own conversations, research sources, tools, and feedback |

Studying can introduce a tentative interpretation; later evidence can strengthen,
qualify, or overturn it. A persona can also conclude that nothing changed. Idle
time does not require unrelated study, and saving a conclusion is not proof of
improved professional performance. Knowledge and questions can persist before a
new standing procedure is qualified.

## Start With The Running Installation

Continuous cognition ships in
[v1.9.0](https://github.com/TheSmokeDev/taskchad-os/releases/tag/v1.9.0).
Publishing a release does not upgrade or restart an existing installation.
Check the CLI you are using and the running service separately through
[Runtime Status And Model Control](runtime-status-model-control.md).

```sh
thehomie --version
thehomie status --json
thehomie profile learning summary default --json
```

`default` is the main Homie. Substitute an existing persona name for a specialist;
inspection does not create a missing profile. An empty learning history can be
normal before the first captured experience. A service or storage error must be
shown as an error, not interpreted as an empty history.

## Verify One Complete Learning Cycle

Use **Agents → the persona → Learning**, or the read-only commands below. Replace
`default` with an existing specialist name and `RECORD_ID` with an ID returned by
the history view.

```sh
thehomie profile learning summary default --json
thehomie profile learning list default --kind cognitive_cycle --limit 10 --json
thehomie profile learning list default --kind understanding --limit 10 --json
thehomie profile learning list default --kind investigation --limit 10 --json
thehomie profile learning show default RECORD_ID --json
thehomie profile learning report default --json
```

1. Start with a real observation and its source, revision, and timestamp.
2. Follow it to a cognitive cycle with an actual execution/model receipt and a
   concise conclusion. A queued cycle alone has not reasoned yet.
3. Inspect the resulting understanding or investigation. Follow the question's
   trigger, later evidence, and reassessment; no trade or adopted method is required.
4. After ordinary work resumes, inspect a delivered context record whose phase is
   `executed`. Its included record IDs and hashes show which retained versions
   reached that completed request, on which model/provider.

For a restart check, preserve the original IDs, restart through the installation's
normal lifecycle, then verify a new observation and actual reassessment afterward.
Processing evidence collected before restart proves checkpoint recovery; a new
collection afterward proves that follow-up acquisition resumed too. An enabled
flag, a healthy process, or a growing record count cannot substitute for this chain.

## Daily Operator Flow

1. Open **Agents → a persona → Learning**, including the default Homie. Check
   whether learning is enabled or paused, then inspect waiting outcomes, current
   methods, recorded failures, and background work.
2. Open a current method or history record. Follow its links to the candidate,
   qualification, supporting observations, and counterevidence. A method is
   provisional even when its qualification passed.
3. Check subsequent context and execution records to see which method versions
   reached actual requests, on which provider and model. Follow later outcomes
   before concluding the method helped.
4. For a stalled item, read its background-work stage, status, and error. Use the
   troubleshooting table below instead of repeatedly starting new work.
5. Pause the harness when you need to inspect it without further harness work.
   Roll back a particular activation when its applied method should be retired.

## Understanding And Investigations

The cycle is **reorient → interpret → investigate → revisit → reflect and retain
→ carry forward**. Work starts with relevant identity, current understanding,
recent observations, open questions, and qualified methods. Meaningful tool or
source batches and work completion wake interpretation and debrief work. A
durable investigation returns when its time or evidence condition is due. A
restart preserves the question and pending work.

Use the Learning history filters **Understanding**, **Investigations**, and
**Cognitive cycles**. A concept, interpretation, belief, self-assessment, or source
assessment can be retained as **tentative** immediately. Supported knowledge has
an evidence check; a standing procedure still requires qualification. A failed
procedure test does not erase an observation or an unresolved investigation.

Investigations show their question, why it matters, original evidence, requested
next observation, due condition, current status, and conclusion. Available
triggers cover a deadline, closed candles, a price/indicator crossing, and new
source/thread material. **Blocked** means the required evidence or capability
is missing; it does not mean the hypothesis was disproved. Pause preserves this
history and pending work. Resume makes overdue work eligible once.

The dispatcher checks every 60 seconds, gives foreground work priority, and
rotates across eligible personas. A check is not necessarily a model call. It
uses tokens when evidence, unfinished work, or a due investigation warrants
reasoning. Existing heartbeat/reflection/dream jobs also wake the same queue.
Inspect **Cognitive lifecycle** for the last successful check, failures, and hook
coverage. An enabled flag alone does not establish that a cycle completed.

For a market persona, a no-call or risk block can still produce understanding.
Chart interpretation must use the frozen candles and matching indicator/image
evidence supplied to that request. A numeric-only receipt is not proof the model
saw an image. A Discord claim retains its source identity and revisions so later
edits do not silently replace what was originally observed. These are evidence
connections, not a guarantee of market prediction accuracy.

## Ask What Changed

You can also ask directly, “What did you learn this week?” A capable runtime
can read its own `learning_report` tool; the shared hook supplies the same host
report to a matching direct question when tools are unavailable. “This week”
means the current deployment-local calendar week. Asking how to implement a
learning report, or quoting that question as an example, does not trigger it.

The **What changed** report selects 24 hours, seven days, or 30 days. Counts come
from stored records, not the model's estimate. **Distinct conclusions** separates
repeated statements from understanding revisions; investigation, observation,
qualification, adoption, and later-context counts remain separate. Raw
evaluation trials and manifests do not count as learned ideas.

**Explain these changes** explicitly invokes a model to explain the report with
record references and uncertainty. Reading or refreshing a report does not invoke
inference. Reports with no recorded changes do not manufacture learning.

```sh
thehomie profile learning report default --json
thehomie profile learning report crypto --since 2026-09-01T00:00:00Z --until 2026-09-08T00:00:00Z --explain --json
thehomie profile learning list default --kind understanding --json
thehomie profile learning list crypto --kind investigation --status blocked --json
thehomie profile learning list crypto --kind cognitive_cycle --json
```

Report periods are inclusive at `--since`, exclusive at `--until`, require
timezone-aware timestamps, and may span up to 366 days. With no dates the period
is the preceding seven days. Dates above are examples, not a prescribed window.

The combined daily recap becomes due at 18:00 in the deployment timezone and
queues at the first eligible wake respecting existing quiet hours. If quiet
hours or downtime defer it, the next eligible morning can deliver it. Important
changed conclusions and requests for input use the existing proactive-action
queue. Dedupe survives delivery and restart; queueing is not a delivery receipt.
The existing notification channel policy still controls actual delivery.

To verify actual continuity, follow a cycle into retained understanding or an
investigation, then its reassessment, then a later **executed** context receipt
linking the exact retained version. An informed no-change conclusion is valid.
More candidates or more reasoning calls alone are not proof of improvement.

The dashboard refreshes automatically and also has a **Refresh** button. CLI
inspection reaches the same Python-owned state:

```sh
thehomie profile learning summary default --json
thehomie profile learning history default --kind activation --limit 30 --json
thehomie profile learning history default --kind evaluation --limit 30 --json
thehomie profile learning history default --kind context --limit 30 --json
thehomie profile learning show default RECORD_ID --json
```

Replace `RECORD_ID` with an ID returned by history. `show` and `rollback` require
both the persona name and record ID. Other commands default to `default` when
its name is omitted. Use `next_cursor` from a history response with `--cursor`
for the next page; `--status` narrows results to an exact stored status.

## Read The Evidence Chain

| What you find | What it establishes |
|---|---|
| Experience and expectation | The task, circumstances, and testable prediction; check whether the phase is before action or before publication |
| Execution | What the host actually attempted or generated, including actual runtime metadata; a generated draft does not establish a send |
| Observation | Available evidence and its provenance; missing or partial evidence is not a negative outcome |
| Candidate and evaluation | A conditional proposed change and its recorded comparison against the baseline on separate cases |
| `active_provisional` method | The method passed its recorded qualification and was applied through the existing skill/amendment lifecycle |
| Prepared or submitted context | Content was assembled or submitted; neither phase alone proves an execution completed |
| Executed context receipt | The exact method versions included in an executed request; this alone does not prove the model followed them or improved an outcome |
| Later observations | Evidence for reassessment, revision, or rollback; comparable observations still do not automatically establish causation |

A useful inspection follows observation → candidate → evaluation → activation →
executed context → later observation. A model saying “I used the lesson,” a growing
note count, or successful tool execution is not enough to prove improvement.
Qualification is recorded against the actual model/provider. Changing models
preserves learning and can queue requalification; it does not imply every model
will perform identically.

Initial qualification uses 12 cases by default, including applicable situations
and counterexamples. Proposal/selection feedback is separated from qualification
cases, with a frozen baseline and candidate under matching budgets. Evaluator
confidence alone cannot authorize adoption. Applied skill files and amendment
ledgers remain authoritative for the actual content.

## Pause, Disable, Resume, And Roll Back

**Pause is not rollback.** It stops harness capture, the harness's learned-context
bundle, and background learner activity while preserving stored history and
applied methods. Skills and amendments already installed can still affect ordinary
memory/skill paths outside that bundle. Pause does not undo external actions or
necessarily interrupt a model request already in flight.

```sh
thehomie profile learning pause default --json
thehomie profile learning resume default --json
thehomie profile learning rollback default ACTIVATION_ID --json
```

Use an activation ID from the methods/history view, not a candidate or evaluation
ID. Rollback selectively reverts that activation's applied content and preserves
its evidence and history. A still-supported predecessor may be restored; inspect
current methods afterward. If newer content conflicts with the rollback, inspect
the reported conflict rather than overwriting files manually.

| Control or condition | Current behavior |
|---|---|
| Valid default/named profile with no `learning` block or no `enabled` key | Harness enabled by default |
| Explicit `learning.enabled: false` | Harness disabled; resume does not override it |
| Malformed learning configuration | Error; it is not treated as permission to enable learning |
| Pause | Suspends shared cognition, synthesis, qualification, and tuning admission/work while preserving history and applied content |
| Resume | Clears pause; explicit configuration or environment disables still apply |
| `PERSONA_LEARNING_ENABLED=false` | Disables the harness and the existing reflection tick; other producers retain their documented controls |
| `HOMIE_KILLSWITCH_HARNESS_LEARNING=disabled` | Stops shared lifecycle eligibility without rewriting persona configuration |

Existing `profile learning enable`/`disable` commands manage persona configuration.
The [scheduled reflection entry points](persona-learning-loop.md) admit work to
the shared lifecycle. Their old direct automatic-write path is historical;
shared pause and explicit configuration disables govern the unified worker.

## When Learning Appears Stuck

| Symptom | Inspect and respond |
|---|---|
| Empty history | Confirm the persona, effective enable/pause state, running version, and whether work passed through a supported surface |
| Waiting for an outcome | Inspect the deadline and source evidence; absent access or an unfinished observation window cannot establish failure |
| No background progress | Inspect dispatcher health and last successful check, queue status, pause/disable state, foreground activity, and recovery wake execution |
| Main Homie has a different history in the bot and CLI | Compare `HOMIE_DEFAULT_PROFILE_ROOT` in the actual process launchers; inventory and reconcile split stores before changing the root, never silently choose one history |
| Investigation blocked | Inspect the requested evidence source and next-check reason; source access, missing candles, or stale data must not become a false conclusion |
| Quota, auth, or transport error | Read the visible error and repair the existing provider configuration as appropriate; infrastructure failures retain checkpoints and defer rather than consuming semantic-failure retries |
| Codex chat works but a background or tool request fails | Inspect the specific transport/capability error. Strict reasoning and caller tools use the verified app-server bridge; ordinary `codex exec` can use a different binary. See the [runtime guide](runtime-status-model-control.md#learning-when-you-change-models). |
| A chart cycle cannot run on the selected model | Inspect actual image capability and configured fallback. Numeric-only evidence is a valid input when explicitly supplied; an image request is not silently relabeled as a successful visual analysis. |
| Observer unavailable | Restore access to the exact evidence source; retain the pending or partial observation instead of inventing an outcome |
| Qualification failed | Inspect applicable cases, counterexamples, baseline, and hard checks; the candidate is not entitled to adoption |
| Method no longer appears in the learned bundle | Check applicability, context budget, actual applied content, pause/disable state, and reassessment history |
| Rollback conflict | Inspect the activation and newer physical content; do not force a manual overwrite |
| Missing/corrupt profile storage | Report the error and use existing profile/storage recovery; do not initialize a replacement over unreadable data |

Recorded failures include coverage gaps and states needing attention; not every
entry means the whole persona stopped working. Ordinary work can continue after
optional capture fails and records an honest coverage failure. Learning-initiated
trials and adoption require durable records.

Retry eligibility depends on the recorded failure class. Shared control and
learning-unavailable deferrals normally wait 60 seconds; runtime/network/storage
errors caught by the worker's infrastructure branch wait 600 seconds. Provider
cooldowns, foreground activity, and persona rotation can delay the actual next
attempt. Inspect the job's `available_at`, stage, and error rather than assuming
a retry occurs exactly on the next minute. Semantic job failures have a separate
retry limit. Current operational defaults are:

| Setting | Default |
|---|---|
| Worker and foreground lease lifetime / renewal | 90 seconds / 25 seconds |
| Direct worker / scheduled child stage allowance | 6 stages / 1 stage per profile child |
| Stage / parent child-process timeout | 600 seconds / 900 seconds |
| Consecutive semantic job errors before failure | 3 |
| Candidate revision allowance | 2 |
| Late email observation window | 30 days after the deadline |

`PERSONA_LEARNING_MODEL_BUDGET_USD` inherits `CHAT_MAX_BUDGET_USD` when unset.
With neither set, no dollar cap is invented. Explicit caps must be positive and
finite; time and case-count bounds still apply. Provider/model support and account
availability remain prerequisites for qualification.

## Scheduling And Surface Coverage

Persistence notifications enqueue work without calling a model. The chat service
supervises one elected dispatcher that checks every 60 seconds. Existing
heartbeat, reflection, and dream entry points are recovery wakes for the same
queue; no additional cron is required. Due reassessments precede fresh cognition;
historical recovery follows current work. At equal priority, ready-time ordering
lets other waiting jobs progress when an older job is deferred. Empty queues
create no artificial study tasks. Shared activity
leases give foreground work priority; the learner yields between stages, so an
in-flight request may finish first.

Framework lifecycle hooks cover interactive engine turns, Discord/web persona
turns, Cabinet, Talk delegation, worktick drafts and code dispatch, curriculum
synthesis, and optional domain producers. Vendor-specific developer hooks are
not the sole entry point. Provider-owned internal substeps without reliable host
callbacks remain explicitly uncaptured. See the [developer lifecycle
map](persona-harness-learning-developer.md#lifecycle-and-ownership).

Each persona owns `<data>/learning/learning.db`; mutable queue control lives in
`<data>/learning/queue.db`. Records and evidence preserve provenance, with
append-only status history. Existing memory, reflection, curriculum, amendment,
and skill stores retain their responsibilities.

## Domain Outcomes And Their Limits

**Sales:** Gmail, personal Gmail, and Outlook observers read the exact configured
mailbox and distinguish `replied`, `no_reply`, `pending`, `not_sent`, and
`unavailable`. A full observation window is required for `no_reply`, which does
not establish lack of interest. Later operator interventions and late replies
remain part of the evidence. Booking or revenue needs separately linked proof.

An outbound message must be physically observed as sent and tied to the prior
expectation. A successful send API response is insufficient. The optional
`outlook_send_email` tool in `mail_write` uses the existing exact `/act approve`
authority, sends once, and verifies Sent Items before linking outcomes. Delayed
receipt recovery retries reads only. Gmail remains read-only. Legacy sends without
trusted learning context are unattributed; learning does not grant send authority.

**Study:** curriculum supplies bounded literal source excerpts separately from
generated dossier excerpts, with timestamps, offsets, hashes, and synthesis
provenance. Validating dossier structure does not verify every source claim or
prove practical effectiveness. Worktick drafts are not customer sends, and code
dispatch is not completed work.

**Optional paper trading:** adapters retain the original claim and market
snapshot before the host applies a paper action, then reconcile settlements and
corrections. Historical imports remain backfill and never invent a prior
expectation. Private domain implementations and operational evidence are not
required by or included in the public learning core.

The v1.9.0 deployment acceptance verified real persona reasoning on Kimi and
later use of that retained understanding in a Codex conversation. A specialist
also completed a Codex-only numeric-evidence trace through retention, restart,
fresh observation, reassessment, and later ordinary context inclusion. A separate
vision trace verified actual chart-image delivery. Minute-level dispatch and
deduplicated daily recap delivery were observed on that installation.

These are dated lifecycle and runtime proofs, not a claim that every configured
account is available or that domain performance has improved. The earlier v1.8.0
synthetic method-use checks remain historical evidence; assess current readiness
with your own running installation and records.

## API And Further Reference

The authenticated Python API owns behavior; Hono stays thin and translates the
dashboard's `main` ID to canonical `default` at its existing boundary. Operator
responses redact secret-bearing values and local file paths. Record lookups are
persona-scoped and accept opaque IDs, not arbitrary evidence file paths.

| Method | Python route | Result |
|---|---|---|
| GET | `/api/agents/{id}/learning` | Summary and active methods |
| GET | `/api/agents/{id}/learning/lifecycle` | Synthesis stages, exact provenance, persisted consumers and skips |
| GET | `/api/agents/{id}/learning/tuning` | Validated-corpus readiness, split counts, comparisons and policy history |
| POST | `/api/agents/{id}/learning/tuning/run` | Explicit tuning queue admission, under normal eligibility controls |
| POST | `/api/agents/{id}/learning/tuning/rollback` | Restore the predecessor recall policy with a durable receipt |
| GET | `/api/agents/{id}/learning/records` | History with `kind`, `status`, `limit`, `cursor` |
| GET | `/api/agents/{id}/learning/records/{record_id}` | Record, history, and linked evidence |
| GET | `/api/agents/{id}/learning/report?since=...&until=...` | Read-only host counts and recorded changes |
| POST | `/api/agents/{id}/learning/report?since=...&until=...` | Explicit bounded model explanation with a persisted receipt |
| POST | `/api/agents/{id}/learning/pause` | Suspend harness learning |
| POST | `/api/agents/{id}/learning/resume` | Clear pause |
| POST | `/api/agents/{id}/learning/activations/{activation_id}/rollback` | Revert that activation's future influence |

- [Developer guide](persona-harness-learning-developer.md): interfaces, hooks,
  domain evidence, executable temporary-storage examples, and verification.
- [Persona Learning Loop](persona-learning-loop.md): legacy reflection producer
  and configuration management.
- [Universal Persona Cognition](universal-persona-cognition.md): the wider persona
  memory, experience, reflection, and dream map.
- [Persona Curriculum Engine](persona-curriculum-engine.md): source study and
  evidence handoff.
