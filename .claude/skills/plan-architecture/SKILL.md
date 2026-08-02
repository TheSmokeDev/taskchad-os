---
name: plan-architecture
description: >
  Interactive CTO-session skill: take an intent (a PRD in PRDs/active/, a PRP, a brief, or a
  free-form idea) and decide the HOW the intent left open — approach, stack, data shape,
  boundaries, and what to de-risk with a spike — producing an architecture decision doc at
  PRDs/active/PRD-<slug>.architecture.md linked both ways to its PRD. Decisions only, never a
  task-by-task build plan. Use when the user says "architecture session", "how should we
  approach this", "explore the approach", "architecture doc", "decide the stack", "what are
  our options here", or after /create-prd and before /piv-slice-epic. Adapted technique from
  Cole Medin's TheHomie workshop (see NOTICE.md); private skill — excluded from public export.
---

# plan-architecture — Decide the How, Out Loud

The intent doc says WHAT and WHY. This skill settles the HOW at the eng-lead altitude: the
approach, the stack, the data shape, the boundaries, and the risks worth a spike. It stops
above the task list on purpose — per-ticket implementation planning happens later
(`core_piv_loop:plan-feature` or `/core:prp`), after `/piv-slice-epic` cuts the tickets.

## Input

`$ARGUMENTS` = the intent: a path to a PRD (`PRDs/active/PRD-<slug>.md`), an existing PRP, a
brief, or a free-form idea. Read it FIRST. If reference docs were passed alongside (research,
API docs, prior handoffs, a competitor teardown), read those too before exploring — and if
none were passed, ask whether any exist. In this repo the answer is usually yes: check
`PRPs/active/TRACKER.md`, `.claude/handoffs/`, and the vault before inventing context.

## The contract: this is a conversation, not a one-shot

Never silently converge on one answer. Run the loop with the operator:

```
investigate → surface 2-3 genuinely different options with trade-offs
            → recommend one, with the reasoning
            → ask → WAIT for the call → go deeper on what they picked
```

You are a pragmatic staff-engineer advisor. You propose; the operator decides. Weigh every
option against:

- **The operator's actual goal** — pull every trade-off back to it.
- **Familiarity** — a stack we already run beats a "better" one we don't.
- **Leanness** — decide only what's needed to move; no premature abstraction (house rule).
- **Reversibility** — cheap-to-undo calls get decided fast; spend the deliberation on the
  expensive one-way doors.

## Mode check: greenfield vs brownfield

Figure out which you're in before exploring (ask if unclear — one short inline question):

- **Greenfield** — explore the solution space: approaches, current best practice, first
  principles. Research asks follow the house research stack (Exa + Firecrawl + /last30days,
  never bare WebSearch) when the decision gates on fresh external facts.
- **Brownfield** (the default in thehomie) — explore how the work LANDS in the existing
  system first: which vertical slice owns it, what it reuses, what it must not break. Read
  the owning slice's code and its CLAUDE.md section doc; a `/prime-*` command is optional
  grounding, not a prerequisite. Respect the standing invariants without relitigating them:
  slice ownership, lane-first routing, default-deny mutations, Rules 1-4.

## What to work through (skip what doesn't apply — say so, don't silently omit)

- **Approaches** — 2-3 real alternatives from different angles, each with trade-offs.
- **Stack & libraries** — what and why, with the rejected alternatives named.
- **Data shape** — entities, relationships, where they're stored. Shape-level, not columns.
- **Boundaries & contracts** — auth posture, secrets, external services, which existing gate
  or slice seam the new work crosses. Flag these explicitly; they're where reviews bite.
- **Missing pieces** — what the chosen approach needs that doesn't exist yet.
- **Spikes** — for any uncertain or expensive-to-reverse call, propose the smallest test
  that settles it, with a decision rule:

  ```
  Question:      what we're unsure about
  Spike:         smallest build/test that answers it, timeboxed
  Decision rule: X if <signal> / Y if <counter-signal>
  ```

## The output doc

Only after the calls are made. Default home: **`PRDs/active/PRD-<slug>.architecture.md`**,
linked both ways — the doc opens with `Intent: [PRD-<slug>.md](./PRD-<slug>.md)` and the PRD
gains an `Architecture: [PRD-<slug>.architecture.md](./PRD-<slug>.architecture.md)` line.
Two clean sources: intent stays pure, decisions stay findable. (Small solo effort with no
slicing ahead → folding an `## Architecture` section into the PRD/PRP is acceptable; the
pi-port PRP is an example of the folded shape.)

Template:

```markdown
# Architecture — <name>

Intent: [PRD-<slug>.md](./PRD-<slug>.md)

## Reference material
The ACTUAL URLs and paths, not names. Repo (with branch), docs site AND the
specific pages that mattered, maintainer/architecture doc, local checkouts, the
file:line source map for code that DECIDED something, the endpoints/APIs this
design consumes, in-repo prior art, and any wrong-turn warnings (archived
branches, same-named unrelated packages, stale doc pages).
(Skip only when the work touches nothing external — say so explicitly.)

## Problem & goals
One paragraph — the goal every decision below is judged against.

## Approaches considered
The 2-3 directions weighed, trade-offs each, and which one we picked and why.

## Recommended approach
The shape of the solution in a few sentences. Brownfield: where it plugs in and what it
reuses.

## Key decisions
- Stack & libraries — what/why + alternatives considered
- Data shape — entities/relationships/storage at the shape level
- Boundaries & contracts — auth posture, secrets, gates crossed, slice seams
- Other eng-lead calls worth recording
(skip N/A items with a note)

## Missing pieces
What has to exist that doesn't yet.

## Spikes
Each uncertain/one-way call, with its decision rule.

## Open questions
Deferred decisions — named, not hidden — and what would settle each.
```

## After the doc lands

Confirm the path, confirm both links, summarize the recommendation in a few lines, then
offer the next moves and let the operator pick — don't force the pipeline:

- **Slice it**: feed the PRD + this doc to `/piv-slice-epic` → PIV-sized issues on the
  private tracker.
- **Small epic**: skip slicing, go straight to `core_piv_loop:plan-feature` for one
  implementation plan.
- **Spike first**: if an open risk blocks everything else, build the spike now.
- **Keep refining here.**

## Write the references INTO the doc

`/piv-slice-epic` and every downstream implementation session read the PRD and
this architecture doc. They never see the chat transcript where the research
happened. If the repo URL, the docs pages, and the source map live only in your
final message, the slicing session re-derives all of it — re-finding the repo,
re-reading the docs, re-grepping for the function that already decided the
architecture. That is the most wasteful re-do there is: the work was done and
simply wasn't written down.

So: URLs verbatim (not "their GitHub"), the specific pages that mattered (not
just the docs root), file:line for code you reasoned from, and the wrong turns
flagged (archived branches that look current, unrelated same-named packages,
stale doc pages). Cross-link rather than duplicate — the handoff points here.

## Success criteria

- Ran as a conversation — operator weighed in before anything was written.
- More than one approach genuinely explored; recommendation carried reasoning.
- High-level throughout — zero file-by-file edit lists.
- Doc linked both ways to its PRD.
- Every one-way call got a spike or an explicit "decided anyway because…".
- Reference material section carries real URLs/paths — a fresh session never
  has to re-derive what this session already looked up.
