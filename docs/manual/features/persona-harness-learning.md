# Autonomous Persona Harness Learning

Status: Introduced in v1.8.0; qualified methods remain provisional
Owner: Framework (personas, cognition, runtime, scheduler, and dashboard)
Last updated: 2026-09-06

The `outlook_send_email` tool in the `mail_write` catalog connects a persona's
recorded expectation to an approved email. Existing `/act approve` authority
still controls the exact send. The sender records a correlation marker, sends
once, and verifies the Sent Items record before linking inbound outcomes.
Delayed receipts retry reads only. Gmail adapters remain read-only. Legacy
direct sends without trusted learning context are deliberately unattributed.

Workspace Gemini OAuth may require a project supplied explicitly by the host in
`RuntimeRequest.env`; model-only execution preserves that explicit project while
discarding unrelated inherited project/Vertex settings and keeping tools empty.
An account license failure still requires operator account repair.

Typed provider quota/auth/transport failures defer a learning checkpoint instead
of exhausting its semantic-error retry count. The worker retries after ten
minutes; unavailable outcome observers likewise remain deferred. These states
are visible in the Learning tab's background-work view.

## What It Does

Each persona carries its own learned knowledge, understanding of its capabilities,
and working methods across sessions and model changes. The framework captures
experiences, checks observable outcomes, proposes conditional improvements,
evaluates them on separate cases, adopts qualified changes automatically, and
reassesses their use in later work. Fine-tuning is not required.

This extends the [Persona Learning Loop](persona-learning-loop.md) and
[Universal Persona Cognition](universal-persona-cognition.md). Existing memory,
reflection, curriculum, amendment, and skill stores keep their responsibilities.
The harness adds evidence and qualification records; it does not replace the
persona's vault with a second competing source of applied content.

## Operator Entry Points

Open **Agents → a persona → Learning** in the dashboard. The default Homie has
the same view. Inspect the timeline, pending observations, candidates, evaluations,
active methods, and failures. Open a record to follow links to its evidence and
history. Pause/resume controls affect learning; rollback targets an activation.

```sh
thehomie profile learning summary default --json
thehomie profile learning summary sales --json
thehomie profile learning history sales --kind evaluation --limit 30 --json
thehomie profile learning show sales RECORD_ID --json
thehomie profile learning pause sales --json
thehomie profile learning resume sales --json
thehomie profile learning rollback sales ACTIVATION_ID --json
```

Use `next_cursor` from a history response with `--cursor` for the next page.
Existing profile learning enable/disable commands retain their configuration
role. Resume clears a pause and does not override an explicit configuration
disable. The harness is enabled by default for physically valid existing and new
profiles when the learning block or its enabled key is absent. Explicit `false`
remains off, and malformed configuration is an error. The older reflection
pipeline keeps its own compatibility rules for historical profiles.
Default and named profiles resolve through the canonical persona path
helpers; neither API nor CLI reads another profile's ledger by record ID.

The Python API owns behavior. Hono forwards authenticated requests and translates
the dashboard's `main` identifier to Python's `default` at the existing boundary.

| Method | Python route | Result |
|---|---|---|
| GET | `/api/agents/{id}/learning` | Summary and active methods |
| GET | `/api/agents/{id}/learning/records` | Paginated history; `kind`, `status`, `limit`, `cursor` |
| GET | `/api/agents/{id}/learning/records/{record_id}` | Record, history, and owned evidence links |
| POST | `/api/agents/{id}/learning/pause` | Pause learning |
| POST | `/api/agents/{id}/learning/resume` | Clear the pause |
| POST | `/api/agents/{id}/learning/activations/{activation_id}/rollback` | Revert future use of the activation |

Operator responses redact secret-bearing values and local file paths. They
accept opaque record IDs, not arbitrary evidence file paths.

## How Learning Works

1. **Experience:** a stable source ID identifies the logical task. Execution
   attempts remain separate across retries and model fallback. Records distinguish
   real work, study, practice, evaluation, and historical backfill.
2. **Expectation:** the persona supplies its own claim, observation deadline,
   resolution rule, and situation. A host-controlled action can record this before
   execution. Tool-less recommendations can carry a final expectation envelope,
   committed before publication; that does not imply it preceded drafting.
3. **Observation:** adapters record actual artifacts, feedback, or domain outcomes
   with provenance. Missing access and missing outcome data remain visible.
4. **Candidate:** a proposed knowledge, self-model, or procedure change includes
   applicability, evidence, counterevidence, and uncertainty. A change affecting
   behavior receives procedure evaluation regardless of its eventual file.
5. **Qualification and adoption:** baseline and candidate run on separate
   qualification cases under the same declared runtime and inference budget.
   `PERSONA_LEARNING_MODEL_BUDGET_USD` inherits `CHAT_MAX_BUDGET_USD` when unset;
   with neither set, no dollar cap is invented for subscription-backed runtimes.
   Explicit caps must be positive and finite. Stage time and case-count bounds
   still apply. Actual qualification is recorded by provider and model, not by
   comparing a requested model alias.
   Initial qualification uses 12 cases by default, including counterexamples.
   Improvement on the primary metric and no new hard-check failures are required.
   A machine receipt binds the tested content and evaluator version to promotion.
6. **Future use:** relevant method content is delivered to the next request within
   the surface's context budget. A receipt records the actual rendered content.
   New evidence, corrections, regressions, and runtime changes can trigger another
   evaluation or conflict-aware rollback.

`active_provisional` means the method passed its recorded qualification. It does
not establish months of professional improvement. Simulated practice is labelled
as practice; repetitions do not become independent real-world results. A correct
prediction, successful tool call, and improved commercial outcome are different
measurements. Model independence preserves the persona's learning when the model
changes; it does not promise identical performance from every model.

Qualification evidence is separate from proposal and selection feedback. Exposed
qualification cases cannot quietly become a reusable optimization target. A model
reviewer is one signal; deterministic checks and physical outcomes are retained.
Automatic adoption and rollback reuse the amendment and skill lifecycles and never
claim that an automated evaluation was an operator approval.

## Surface And Evidence Coverage

Framework lifecycle hooks connect each surface to the same learning service:

| Boundary | Harness responsibility |
|---|---|
| Before a turn | Retrieve applicable methods and record the rendered versions |
| Before a meaningful host action | Commit the persona's expectation and bind it to that action |
| After an execution attempt | Record the actual runtime, result, and coverage, including failure |
| When external evidence arrives | Reconcile delayed or corrected observations and schedule reassessment |
| During idle capacity | Resume observation, practice, qualification, adoption, or revision work |

Hook adapters carry lifecycle events; storage, evaluation, and adoption remain
Python-owned. Vendor-specific developer hooks are not the framework's only entry
point. Replacing a model or entering through another supported channel still
reaches the same learning lifecycle.

Interactive engine, Discord/web persona turns, Cabinet, Talk delegation, worktick
drafts and code dispatch, curriculum synthesis, and optional domain producers have
explicit adapters. They do not all pass through one chat engine. Provider-owned
internal substeps without reliable host callbacks remain labelled as uncaptured.

Worktick drafts commit a supplied expectation before writing the deliverable.
Writing a draft is not sending a customer message. Code dispatch receipts record
the actual run ID and do not claim completion. Curriculum records source URL,
transcript digest, dossier validation, actual runtime, and application proposals.
Validating a dossier does not verify every source claim or prove domain expertise.

Sales observers use read-only Gmail, personal Gmail, or Outlook access. The
`mailbox_id` is the exact provider account email and must match the account being
read. Gmail uses immutable message IDs and server `internalDate`; Outlook requests
`Prefer: IdType="ImmutableId"` on every page. A bounded or failed page scan cannot
establish absence of a reply.

An outbound message must be physically observed as sent, addressed to the named
prospect, and linked to the expectation. Provider IDs assigned after send live in
an execution receipt; the prior expectation remains immutable. A send API's
acceptance boolean is insufficient. Outbound evidence predating a claimed prior
expectation cannot be relabelled as preregistered.

The observer distinguishes `replied`, `no_reply`, `pending`, `not_sent`, and
`unavailable`. Only a completed observation window can produce `no_reply`; it says
nothing about interest or causation. Later operator messages are retained as
possible interventions. Late replies can supersede prior observations and trigger
reassessment. Booking and revenue require their own linked evidence.

Optional paper-trading adapters retain the original call and market snapshot,
record a thesis before the host applies the paper action, and reconcile actual
paper settlements, including untouched expirations. Historical imports remain
backfill and do not invent prior expectations. Corrected settlements supersede
their earlier evidence. These adapters are optional; the public core never imports
a private domain implementation.

## Storage, Scheduling, And Recovery

Each profile owns `<data>/learning/learning.db` and its immutable evidence artifacts.
Mutable job control lives in `<data>/learning/queue.db`. Status history is
append-only. Existing amendment ledgers and skill files remain authoritative for
the content actually applied. Inspect rollback conflicts instead of overwriting
unrelated newer learning.

Heartbeat, reflection, and dream entry points wake the same resumable queue.
Persistence notifications enqueue work without starting a model call. Due
observations, regressions, and corrections take priority over new practice.
Empty queues create no artificial study tasks.

There is one installation-wide learning worker lease by default. Runtime activity
leases give foreground work priority. The worker yields between stages; an
in-flight model request may finish first. Current operational defaults are:

| Setting | Default |
|---|---|
| Activity and worker lease lifetime | 90 seconds |
| Lease renewal interval | 25 seconds |
| Direct worker stage allowance | 6 stages |
| Scheduled child allowance | 1 stage per profile child |
| Stage time limit | 600 seconds |
| Parent child-process time limit | 900 seconds |
| Consecutive job errors before failure | 3 |
| Candidate revision allowance | 2 |
| Late email observation window | 30 days after the deadline |

Quota errors, lost leases, pauses, and stage timeouts retain checkpoints or visible
retry/defer states. Missing observations call for further observation, not invented
success. Learning-initiated trials and promotion require their durable records;
ordinary work continues when optional learning capture fails and reports the
coverage failure. Existing external-action permissions remain independently owned.

## Source Of Truth And Verification

The core is `personas/learning/` under the framework scripts tree. `hooks.py` owns
surface instrumentation, `observers.py` owns public evidence adapters, and
`evaluation.py`/`promotion.py` own qualification and adoption. The queue and worker
own background lifecycle; `runtime/activity.py` owns shared activity leases.

`dashboard_learning_api.py`, `cli_learning.py`, and `AgentLearning.tsx` expose the
same Python-owned state. The dashboard does not open SQLite or apply amendments.

From the framework scripts directory, run the focused isolated suites:

```sh
uv run pytest tests/test_harness_learning_core.py tests/test_persona_learning_domains.py tests/test_persona_learning_evaluation.py tests/test_persona_learning_queue_worker.py tests/test_persona_learning_surfaces.py tests/test_persona_learning_surface_integration.py tests/test_dashboard_learning_api.py -q
```

Tests use temporary persona targets and fake providers. They cover isolation,
pre-action ordering, replay, source identity, delayed/corrected evidence, held-out
evaluation, tested-content binding, activation/reversion, actual context delivery,
model change, lease recovery, and operator parity. An optional domain's tests run
with that domain installed.

Local fixture verification is not a running-service rollout, public publication,
or live Sales/Crypto performance result. The framework/manual can be sanitized for
public export; profile databases, mailbox content, source captures, and operational
learning evidence stay private.
