# Heartbeat Runtime

Status: active baseline, runtime contract corrected, blocker escalation live, ambient observations live
Owner: scheduled cognition/runtime layers
Last updated: 2026-06-12

## What It Does

Heartbeat is the proactive scheduled check loop. It gathers compact context from
direct Python integrations, memory state, drafts, habits, and `HEARTBEAT.md`,
then sends one assembled prompt through the Homie runtime so the selected model
can decide whether anything needs attention.

The heartbeat is not a script-only classifier. Even when gathered context looks
quiet, the expected contract is: gather deterministic context first, run runtime
reasoning second, and write `HEARTBEAT_OK` only after the runtime pass returns
that result.

## Operator Entry Points

- Manual run: `uv run python heartbeat.py --test`
- Validation probe: `uv run python heartbeat.py --json`
- Runtime status: `uv run thehomie chat -q "/provider" -Q`
- Checklist: `vault/memory/HEARTBEAT.md`
- State: `.claude/data/state/heartbeat-state.json`
- Daily log output: `vault/memory/daily/YYYY-MM-DD.md`

## Source Of Truth Files

| Layer | Files |
|---|---|
| Heartbeat loop | `.claude/scripts/heartbeat.py` |
| Blocker escalation | `.claude/scripts/heartbeat.py` (classify/count/promote), `.claude/scripts/living_memory.py` (the only WORKING.md writer) |
| Ambient observations | `.claude/scripts/heartbeat.py` (sense facts, predicates, pipeline), `.claude/scripts/living_memory.py` (`append_heartbeat_observation` — the only WORKING.md writer), `.claude/scripts/config.py` (`get_heartbeat_observation_settings`) |
| Credential-failure surfacing | `.claude/scripts/integrations/asana_api.py`, `.claude/scripts/integrations/slack_api.py` (`raise_on_error` keyword) |
| Runtime routing | `.claude/scripts/runtime/lane_router.py`, `.claude/scripts/runtime/routing.py` |
| Provider adapters | `.claude/scripts/runtime/openai_codex.py`, `.claude/scripts/runtime/gemini_cli.py`, `.claude/scripts/runtime/claude_sdk.py` |
| Runtime profiles | `.claude/scripts/runtime/profiles.py`, `.claude/scripts/runtime/selection.py` |
| Tests | `.claude/scripts/tests/test_heartbeat_preflight.py`, `.claude/scripts/tests/test_heartbeat_blockers.py`, `.claude/scripts/tests/test_heartbeat_observations.py`, `.claude/scripts/tests/test_living_memory.py`, `.claude/scripts/tests/test_openai_codex_runtime.py` |

## Token And Model Contract

- Context gathering is deterministic and token-efficient. Python calls direct
  integrations and local helpers for email, calendar, tasks, finance, drafts,
  habits, recall, alert history, and the heartbeat checklist before the model
  is invoked.
- The model receives preloaded context. It should not browse raw inboxes,
  calendars, or task systems from scratch during the normal heartbeat path.
- Main heartbeat reasoning uses
  `RuntimeRequest(task_name="heartbeat", capability=TOOL_REASONING)`.
- The Codex path uses `HEARTBEAT_CODEX_MODEL`, defaulting to
  `gpt-5.4-mini`, as a heartbeat-only model override. This does not mutate
  `SECOND_BRAIN_CODEX_MODEL`, so normal chat/model control remains unchanged.
- The Gemini path ignores the Codex-specific override and uses the configured
  Gemini profile model or fallback ladder.
- The Claude-native path uses the configured Claude profile model. It does not
  automatically switch to Haiku unless the Claude runtime model is explicitly
  configured that way.
- The lightweight alert formatter may use `model="haiku"` with an OpenAI
  fallback, but that formatter is separate from the main heartbeat reasoning
  pass.

## Runtime Selection Behavior

In the current generic lane, heartbeat is a tool-capable task. The generic tool
route prefers Codex and can fall back to Gemini. If the operator pins Gemini,
heartbeat runs through Gemini. If the operator pins Claude native, heartbeat
runs through the Claude SDK lane.

> Canonical doc: Lane-First Routing in `.claude/sections/01_architecture.md`
> § Runtime And Auth Boundary — this page keeps only the heartbeat-specific
> lane behavior above.

## Blocker Escalation Into Working Memory

When an integration call fails during context gathering, the heartbeat
classifies the failure Python-side into a stable blocker signature. Detection
is zero-LLM: candidates come only from the gather step's exception handlers,
and the runtime's response text is never parsed for blockers. Every candidate
is counted in heartbeat state under `blocker_observations`; signatures on the
promotion allowlist escalate into a `WORKING.md` Open Thread once they keep
recurring — so the next session inherits the problem and its fix instead of
the heartbeat re-discovering it every cycle.

- **Counting**: one observation per local calendar day per signature (set
  semantics — repeated same-day failures count once). Days age inside a
  rolling window; a signature with no observation inside the window is pruned
  from state (the problem stopped happening; durable history lives in
  `WORKING.md` and the daily logs, not in the counter).
- **Known signatures**: `google:oauth_invalid_grant` covers any Google-backed
  integration failing with `invalid_grant`/`RefreshError` and carries the fix
  hint `uv run python setup_auth.py`. Anything else falls back to the generic
  `{integration}:error` class (counted, no fix hint, not promotable by
  default).
- **Promotion trigger** (all conditions required): signature is on the
  allowlist, has reached the distinct-day threshold inside the window, was
  not promoted within the re-promotion cooldown, and fewer than the guardrail
  maximum of active `[heartbeat]` threads exist. Promotion runs BEFORE the
  runtime turn (and in `--test` mode — it is a memory write, not a
  notification), and state is saved immediately afterward so a runtime
  failure cannot drop the counters.
- **Bullet shape** (written via `living_memory.append_open_thread`, the only
  WORKING.md writer):
  `- [YYYY-MM-DD] [heartbeat] <summary> — fix: <command> — open`
  A signature without a fix hint gets no `— fix:` segment.
- **Re-promotion**: blocked for `HEARTBEAT_BLOCKER_REPROMOTE_DAYS` calendar
  days after a promotion (eligible at the exact boundary; the standard 3-day
  thread dedup still prevents duplicate bullets). Resolving the thread with
  `/working resolve <N>` archives it; a still-firing blocker re-promotes only
  after the cooldown. A fixed blocker stops being observed, ages out of the
  window, and never re-promotes.
- **Guardrail**: at most `HEARTBEAT_BLOCKER_MAX_ACTIVE` active `[heartbeat]`
  Open Threads. At the cap the promotion is logged and skipped while the
  counters persist, so the blocker promotes when a slot frees.
- **Failure isolation**: every step is fail-open. A classification, counting,
  or promotion failure logs and skips; it never crashes or delays the
  heartbeat run.

| Knob (env) | Default | Meaning |
|---|---|---|
| `HEARTBEAT_BLOCKER_PROMOTE_DAYS` | `3` | distinct days required to promote |
| `HEARTBEAT_BLOCKER_WINDOW_DAYS` | `7` | rolling window for day counting and pruning |
| `HEARTBEAT_BLOCKER_REPROMOTE_DAYS` | `3` | cooldown before re-promotion |
| `HEARTBEAT_BLOCKER_MAX_ACTIVE` | `3` | max active `[heartbeat]` Open Threads |
| `HEARTBEAT_BLOCKER_PROMOTE_ALLOWLIST` | `google:oauth_invalid_grant,asana:auth_failed,slack:auth_failed` | comma-separated signatures allowed to promote |

All five knobs resolve at call time through
`config.get_heartbeat_blocker_settings()` — an env override applies on the
next heartbeat run with no restart and no module reload. Promoted threads
surface automatically through `/working`, the session-start briefing, and the
engine's working-memory region; observed signatures, day counts, promotions,
guardrail skips, and prunes are printed in the heartbeat console output.

## Ambient Observations Into Working Memory

Every heartbeat run also remembers what it WATCHED, not just what broke
(Living Mind Act 2). Python derives deterministic, non-blocker observations
from the sense facts the gather step already produced and writes them into
the `WORKING.md` "Heartbeat Observations" section through
`living_memory.append_heartbeat_observation` — the only WORKING.md writer.
Zero LLM calls anywhere in the path; the heartbeat fires ~48×/day, so the
section ships with its own caps, dedup, and aging in the same write path.

**No-external-text rule (security invariant):** observation bullets carry
NO external free text. Email subjects, calendar event titles, Slack/Asana
message text, and HARO query text are externally influenced and never enter
WORKING.md — it is injected as trusted context into every chat session and
every scheduled prompt, so persisted attacker-reachable text would be a
durable prompt-injection vector. Allowed content: counts, dates,
code-generated blocker signatures, and operator-owned labels (finance
account names / categories / loan collateral from the operator's own data;
habit pillar names from the operator's own HABITS.md). Everything still
passes a deterministic sanitizer (strips control chars, backticks, HTML
comments; trims at word boundaries) as defense in depth.

**Bullet shape:** `- [YYYY-MM-DD] [group] <subject>` or
`- [YYYY-MM-DD] [group] <subject> — <detail>`. The dedup key is
`[group] <subject>` (stable, template-fixed); volatile numbers live only in
the detail. Examples:

```
- [2026-06-12] [calendar] meeting within 4h — 1 upcoming, 3 today
- [2026-06-12] [finance] low balance: Checking — $312
- [2026-06-12] [blockers] slack:token_missing keeps failing — 2 day(s) in 7d window
```

**Groups and predicates** (default groups, all ON — locked operator decision
2026-06-12: `calendar,email,finance,tasks,community,blockers`):

| Group | Fires when | Subject |
|---|---|---|
| calendar | a meeting is inside the existing 4h upcoming window | `meeting within 4h` |
| calendar | today's event count ≥ busy-day threshold (5) | `busy calendar day` |
| email | urgent count ≥ threshold (1) | `urgent email waiting` |
| email | unread count ≥ threshold (50) | `unread backlog high` |
| finance | each low-balance account | `low balance: {name}` |
| finance | bills due within 3 days | `bills due within 3 days` |
| finance | each expiring loan | `loan expiring: {label}` |
| finance | each category at ≥80% of budget | `category overspend: {category}` |
| tasks | overdue task count ≥ 1 | `overdue Asana tasks` |
| community | flagged Slack message count ≥ 1 | `Slack messages flagged` |
| community | HARO query matches ≥ 1 | `HARO queries matched` |
| habits (opt-in) | evening hour reached AND unchecked pillars ≥ 1 | `habit pillars unchecked by evening` |
| blockers | a NON-allowlisted blocker signature at ≥ 2 distinct days in the window | `{signature} keeps failing` |

`habits` is implemented but opt-in (fail-open when HABITS.md is absent). The
`blockers` group reads Act 1's blocker counters and writes nothing to state —
allowlisted signatures are excluded because the promotion path owns them (no
double-reporting). Facts come from the gather contract's 4th element
(`sense_facts`), set only when an integration block succeeded — never parsed
from the formatted prompt or the runtime's response text.

**Knobs** (all call-time resolved via
`config.get_heartbeat_observation_settings()` or body-resolved inside
`living_memory` — env overrides apply on the next run, no restart):

| Knob (env) | Default | Meaning |
|---|---|---|
| `HEARTBEAT_OBSERVATION_GROUPS` | `calendar,email,finance,tasks,community,blockers` | ordered group list; order = candidate priority under the per-run cap; empty string disables ambient observations entirely (kill switch); unknown names log a warning and are ignored |
| `HEARTBEAT_OBSERVATION_MAX_PER_RUN` | `3` | max bullets WRITTEN per heartbeat run (dedup/sanitize skips don't consume the budget) |
| `HEARTBEAT_OBSERVATION_BUSY_DAY_MIN` | `5` | today-event count for `busy calendar day` |
| `HEARTBEAT_OBSERVATION_URGENT_EMAIL_MIN` | `1` | urgent-email count threshold |
| `HEARTBEAT_OBSERVATION_UNREAD_MIN` | `50` | unread-backlog threshold |
| `HEARTBEAT_OBSERVATION_EVENING_HOUR` | `18` | hour gate for the habits nudge |
| `HEARTBEAT_OBSERVATION_BLOCKER_MIN_DAYS` | `2` | distinct days before a non-allowlisted blocker becomes ambient-visible |
| `HEARTBEAT_OBSERVATION_CAP` | `10` | section cap (overflow → archive, insert-only) |
| `HEARTBEAT_OBSERVATION_DEDUP_DAYS` | `3` | dedup window — same subject re-observed inside it writes nothing |
| `HEARTBEAT_OBSERVATION_AGE_DAYS` | `7` | aging window — shared by the in-write ager and the dream cycle |

**Lifecycle (dedup → cap → age):** an unchanged world writes once — the same
subject re-observed inside the dedup window is skipped even when the volatile
detail changed. The section caps at 10 bullets (oldest overflow archives,
insert-only). Bullets older than the age window move to "Archived (Cold)" in
two places sharing the one env knob: in-write on every observation append,
and during the dream cycle's `archive_stale_working_items` pass. Observations
are NOT `/working resolve`-able (that stays Open-Threads-only) — they age out.
The pipeline runs before the runtime turn (and in `--test` mode), so bullets
exist on disk even when the runtime fails; every step is fail-open. Each run
prints an audit summary with the exact written count, every written subject,
and deduped/skipped/dropped counts.

**Surfacing:** zero new wiring — observations flow through the existing
WORKING.md wires: `/working` (new "Heartbeat Observations" block), the
session-start briefing (`Heartbeat observations:` line), the engine's
working-memory region (600-token region budget trims when oversized), the
scheduled cognition payload, and the proactive brief.

## Credential-Failure Visibility (raise_on_error)

Asana/Slack read helpers historically swallowed API auth errors into empty
results — a rejected token looked like "no tasks / no messages" and never
reached the heartbeat's blocker detection. Root-cause fix: the read helpers
(`search_tasks`, `get_my_tasks`, `get_project_tasks`, `get_overdue_tasks`,
`get_due_soon_tasks`; `check_for_important_messages`, `get_channel_id`,
`get_recent_messages`) accept keyword-only `raise_on_error: bool = False`.

- `False` (default) is byte-identical to the old behavior — every other
  consumer (direct-integrations CLI, module CLIs) keeps graceful degradation.
- `True` re-raises from the helpers' own except branches; only the heartbeat
  gather passes it, so failures travel the real except branches into blocker
  candidates.
- Designed degradation is preserved under both states: the Asana 402
  premium-fallback still falls back (its own failure then propagates), and a
  Slack channel that resolves to nothing stays a warning (data absence, not
  an error). Write paths (`send_notification`) are untouched.

Two credential classes, treated differently:

| Class | Signatures | Default behavior |
|---|---|---|
| Token MISSING (env unset) | `asana:token_missing`, `slack:token_missing` | counted in state + ambient-visible via the `blockers` group; NOT promoted (deliberate non-configuration is a config state, not a regression) |
| Token INVALID (present, rejected) | `asana:auth_failed`, `slack:auth_failed` | counted AND on the default promotion allowlist — escalates to an Open Thread with a rotate-token fix hint, same class as Google `invalid_grant` |

The default promotion allowlist is now
`google:oauth_invalid_grant,asana:auth_failed,slack:auth_failed`. Registry
entries are integration-scoped: a Gmail error string containing `401` cannot
cross-classify as `asana:auth_failed`. Pre-existing generic
`asana:error`/`slack:error` state entries are NOT migrated — once candidates
classify to the new signatures, the old generic entries stop updating and age
out of the rolling window on their own (their counts do not transfer).

## Safety Boundaries

- Do not reintroduce a deterministic quiet-context skip before runtime
  reasoning.
- Do not make heartbeat change the global chat model.
- Do not expose secrets from `.env`, OAuth files, vault user files, or provider
  token state in logs or manual pages.
- Keep scheduler cadence changes separate from runtime/model changes unless the
  slice explicitly includes scheduling.

## How To Test It

```powershell
cd <repo>\.claude\scripts
uv run pytest tests/test_heartbeat_preflight.py tests/test_heartbeat_blockers.py tests/test_heartbeat_observations.py tests/test_living_memory.py -q
uv run python -m py_compile heartbeat.py
uv run pytest tests/test_openai_codex_runtime.py -q
uv run thehomie chat -q "/provider" -Q
```

Scoped ambient-observation smoke (writes NO live WORKING.md — redirects the
vault for a fresh process). Preferred deterministic form: a probe process
that sets `HOMIE_VAULT_DIR` to a tmp vault, leaves `HOMIE_HOME` unset (the
override applies to the DEFAULT profile only), deletes every
`HEARTBEAT_OBSERVATION_*` env var, seeds a `blocker_observations` entry at
the `blocker_min_days` threshold plus fixed sense facts, and calls
`heartbeat.process_heartbeat_observations(state, facts, MEMORY_DIR)` with no
knob args — proving the locked default groups fire with zero env setup.

A full `heartbeat.py --test` under `HOMIE_VAULT_DIR` also works but is
non-deterministic (depends on live integration data) and lets the in-process
index sync treat live vault files as stale against the still-live
`memory.db` (derived state — the next live run re-indexes, but prefer the
probe).

Proof either way: the console prints an `Ambient observations:` summary,
bullets land in the tmp vault's WORKING.md, and the live vault file is
byte-identical before/after.

## Latest Proof

On 2026-06-07, focused tests proved that a quiet heartbeat still invokes
`run_with_runtime_lanes`, the heartbeat request keeps `task_name="heartbeat"`
and `capability=TOOL_REASONING`, the heartbeat Codex override defaults to
`gpt-5.4-mini`, and normal Codex chat model configuration remains `gpt-5.5`.

On 2026-06-12, the blocker-escalation suite proved the full observe-to-remember
path: real except-branch candidates flow into the third gather return value,
distinct-day counting and the rolling window honor exact boundaries, promotion
and the pre-runtime state save happen before the runtime call (including when
the runtime raises), the allowlist keeps generic transients counted but never
promoted, and a live `--test` run printed observed signatures with day counts
while leaving `WORKING.md` untouched below the threshold.

Also on 2026-06-12, the ambient-observation suite proved Act 2 end-to-end:
sense facts populate exactly for succeeding gather blocks (absent on failure,
never both facts and candidate), every predicate fires and stays silent at
exact thresholds under a fixed clock, label collisions are dedup-safe, the
per-run budget is consumed only by real writes, observations are on disk
before the runtime call and survive a runtime failure, a same-day re-run
writes zero (the 48×/day flood proof), `raise_on_error` is byte-identical
when off and propagates real auth failures when on (both states, all nine
helpers, full chains), and `token_missing` stays ambient-only while
`auth_failed` promotes with a rotate-token fix.

## Public Export Status

Manual page updated in the private repo. Public export requires the normal
`scripts/sanitize.py` private-to-public flow.
