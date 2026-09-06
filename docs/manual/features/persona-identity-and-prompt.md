# Persona Identity And Prompt Architecture

Status: Shipped baseline — documents current runtime behavior
Owner: `.claude/chat/cognition/` (identity payload, regions, working memory)
Last updated: 2026-08-16

## What It Does

Every persona turn assembles one prompt from a fixed set of identity files plus
per-turn context, inside a hard ~27K character ceiling. This page states which
files are actually loaded, how much room each gets, what order they occupy, and
— the part that causes real bugs — **where standing behavior belongs versus what
goes in a prompt**.

Read this before authoring a persona's files, adding a region, or writing any
scheduled job that prompts a persona.

## The Decision Rule

> **Standing behavior that is true on every turn goes in an identity file.
> Only what is true about THIS turn goes in the prompt.**

A persona's job description, output format, and hard rules are identity. The
scheduled job that wakes it up supplies only the per-turn facts: that it is
morning, who it is for, that the run is unattended.

Getting this backwards is the common failure and it is expensive in three ways:
the rules apply only when that one caller runs, you pay prompt budget for
content the runtime already loads for free, and a long prompt has far more
surface to trip the router's action gate (below).

## Which Files Actually Load

A profile is seeded with 15 files under `<profile>/memory/`. **Seven are
loaded.** The rest are inert — writing rules into them does nothing.

`DEFAULT_INCLUDE` in `.claude/chat/cognition/identity_payload.py` is the
authority:

| Loaded | Region | Carries |
|---|---|---|
| `SOUL.md` | `identity` | personality, job description, standing behavior |
| `SAFETY.md` | `safety` | hard boundaries, spend ceilings, default-deny surfaces |
| `SELF.md` | `self_model` | self-model, beliefs |
| `USER.md` | `user_model` | who it serves, communication preferences |
| `MEMORY.md` | `durable_memory` | durable facts and lessons |
| `WORKING.md` | `working_memory` | open threads, live scratchpad |
| `GOALS.md` | — | read by pipelines, not a chat region |

**Inert (seeded, never loaded):** `BACKLOG.md`, `HABITS.md`, `HEARTBEAT.md`,
`INDEX.md`, `LOG.md`, `MOC-Concepts.md`, `MOC-Connections.md`, `SCHEMA.md`.

Anything else in the vault — doctrine files, plans, reference — reaches a turn
only through relevance-ranked recall, which means it is present when the query
matches it and absent otherwise. **Recall is retrieval, not enforcement.** A
rule that must hold on every turn cannot live there.

## Budgets And The Ceiling

`region_file_map` and `DEFAULT_REGION_BUDGETS` live in
`.claude/chat/cognition/regions.py`; the live values the engine reads are
`REGION_BUDGETS` in `.claude/scripts/config.py`, each with a
`REGION_BUDGET_*` env override resolved at call time.

Budgets are in tokens; multiply by 4 for characters. The assembled prompt is
clamped near **27,000 characters** (a Windows `CreateProcess` argv limit, not a
design choice), so region budgets are zero-sum against each other.

### Budgets are ceilings, not an additive pool

**The append-riding regions are deliberately oversubscribed.** Measured
2026-08-16: they sum to **10,750 tokens ≈ 43,000 characters against a 27,000
character clamp — 59% over.** The system works because regions never all fill at
once, and the engine's own comment records real appends of 30,807–31,307 chars
being cut to 27,000.

This is the single most important thing on this page, because **any reasoning
that treats budgets as an additive pool is wrong before it is written.** Three
consecutive reviews of one change each re-derived a variant of this mistake:

- A "net-zero reallocation" claim proved by `700 + 1300 == 2000` — arithmetic on
  a dict literal that never rendered a prompt, green while false in two of three
  process modes.
- A proposed config validator rejecting any persona budget set whose sum exceeds
  the envelope — a rule that rejects the shipped defaults and even a pure
  decrease.

If you need a budget invariant, express it as a **delta against the defaults**
(a persona may redistribute the envelope, never grow it), or name an explicit
budget-envelope constant distinct from the physical clamp. Never sum budgets and
compare to 27,000.

Three further consequences that bite:

- **A file larger than its budget is silently trimmed.** Its tail simply never
  reaches the model. Content that exists on disk but never reaches the prompt is
  indistinguishable from content nobody wrote. Check file size against budget
  when a persona ignores a rule it demonstrably has. (Real instance: a persona's
  communication preferences sat 53% past its `user_model` budget, so it asked
  questions and offered menus while the file telling it not to was never
  delivered.)
- **Freeing budget from an empty region frees nothing.** The clamp measures
  actual assembled characters. Reallocating from a region that renders empty on
  the turn in question buys paper, not room.
- **Budgets are scaled per mental process before rendering.**
  `PROCESS_WEIGHTS` (`.claude/chat/cognition/processes.py`) multiplies some
  regions — `durable_memory` ×1.5 in PLANNING — so a relationship that holds at
  base budgets is false under weights. `PROCESS_WEIGHT_EXEMPT_REGIONS` pins
  hard-constraint regions like `safety` at base so no present or future weight
  row can scale them.

Budgets are currently **global across all personas** — see the caveat at the end
of this page.

## Order Decides What Survives Truncation

**`WorkingMemory.region_order` in `.claude/chat/cognition/working_memory.py` is
the ordering authority — not `regions.py`.** `region_file_map` maps files to
regions; it does not order them.

`prompt_regions_from_working_memory` calls `order_regions()`, which sorts any
region **not named in `region_order` to the END** — which is the head-keep
truncation zone. A region added to the file map but not to `region_order` is
therefore the first content silently sheared on a heavy turn.

Two worked examples are commented in that file: `portfolio` sits mid-prompt so a
co-founder turn cannot go blind under load, and `safety` sits immediately after
`identity` so hard boundaries are dropped last.

When adding a region, a test asserting its index in the tuple is **not**
sufficient. Assemble a prompt large enough to force truncation and assert the
region survives while lower-priority regions are lost.

## The Action-Gate Trap

`requires_external_action_confirmation` in `.claude/chat/extension_manager.py`
inspects the **incoming message text** and refuses the turn before it reaches
the engine when it looks like an external action without explicit approval.

It is **keyword-matched and cannot read negation.** A prompt saying "do not post
this" trips the same pattern as "post this". It also fires on idioms — a real
incident traced to the word *contact* inside the phrase "plans survive contact
with a Tuesday".

Practical rules for anything that prompts a persona programmatically:

- Describe only what to WRITE, never what happens to the text afterward.
- Keep outbound verbs (send, post, message, contact, deliver, DM, email, reach
  out) out of the prompt entirely — including in disclaimers.
- Prefer short prompts. Surface area is risk.
- **Identity files are not subject to this gate at all** — it never inspects the
  system prompt. Moving standing content into `SOUL.md` removes it from the
  gate's reach as a side effect of putting it in the right place.

## Safety Boundaries

- Identity files are first-party operator content; recalled vault content is
  untrusted and fenced separately at composition.
- `SAFETY.md` is loaded on the interactive path only. Scheduled cognition does
  not read it yet — see the caveats below.
- Nothing here grants capability. Identity is memory; every external-mutation
  gate is independent of it.

## How To Test It

```powershell
cd .claude\scripts

# Which identity files does a profile actually resolve?
uv run python -c "import sys; sys.path.insert(0, '../chat'); from cognition.identity_payload import build_identity_payload; from pathlib import Path; print(sorted(build_identity_payload(Path(r'<profile>/memory')).keys()))"

# Focused suites
uv run pytest tests/test_persona_safety_wiring.py tests/test_identity_payload.py `
  tests/test_cognition_regions.py tests/test_engine_wm_parity.py -q
```

The honest behavioral test for any identity rule is a query that **does not
mention it**. A rule only proven by a prompt that names it has proven nothing.

## Known Caveats

| Issue | Gap |
|---|---|
| #485 | `SAFETY.md` does not propagate to scheduled cognition (reflect / weekly / dream) |
| #487 | `SAFETY.md` vanishes when the optional cognition import is unavailable, while tools stay enabled |
| #488 | Region budgets are global; every persona-specific need is a zero-sum fight across the whole roster |

## Public Export Status

Public-exported.
