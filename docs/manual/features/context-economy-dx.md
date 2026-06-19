# Context-Economy DX (Prime + Onboarding)

Status: Shipped (#66), merged to master
Owner: `.claude/commands/` prime-* commands + `.claude/skills/{brownfield-day-1,vertical-slice-audit}`
Last updated: 2026-06-19

## What It Does

Coding agents pay for context. Loading "the whole codebase" before a task is
slow, expensive, and dilutes the signal the agent actually needs. This bundle
is three developer-experience tools that buy back that context budget:

1. **The `/prime-*` family** — context-priming commands. Each loads one focused,
   fresh vertical slice (the files that matter for that slice, plus its
   architecture doc and invariants) instead of the entire tree. You run a prime
   command at the start of a turn so the agent enters a Ralph DAG or a PIV loop
   already oriented on exactly the slice it will touch.
2. **`brownfield-day-1`** — a skill that onboards a repo with **no AI layer**.
   It explores the codebase from source with parallel sub-agents, then derives a
   `CLAUDE.md` plus a customized `.claude/commands/` suite from scratch. One run,
   end-to-end.
3. **`vertical-slice-audit`** — a skill that scores one feature against the
   Vertical Slice Architecture (VSA) checklist, classifies it into a tier, and —
   if it falls short — hands back a concrete migration plan.

The throughline: **prime** makes an already-onboarded repo cheap to work in,
**brownfield-day-1** produces the priming/onboarding layer for a repo that has
none, and **vertical-slice-audit** measures how prime-able a given slice already
is and tells you how to close the gap. They feed each other.

## Operator Entry Points

| Entry point | Type | Invoke with |
|---|---|---|
| `/prime-memory` | command | `/prime-memory` |
| `/prime-orchestration` | command | `/prime-orchestration` |
| `/prime-signal` | command | `/prime-signal` |
| `/prime-workflows` | command | `/prime-workflows` |
| `brownfield-day-1` | skill | "onboard this repo", "derive a CLAUDE.md", "set up the AI layer" (optionally pass a template path) |
| `vertical-slice-audit` | skill | "audit this feature", "score this slice against VSA" (pass the feature folder) |

The `/prime-*` commands are slash commands in `.claude/commands/`. The two
onboarding tools are skills — describe the goal in natural language (or invoke
by name) and the agent runs the flow. Both skills take one argument: a template
path / feature path respectively.

## The /prime-* Family

Each prime command reads its slice's architecture doc first, then the slice's
key source files in order, then prints a scannable overview (slice boundaries,
invariants, current state). Nothing is mutated — priming is read + summarize.

| Command | What it primes | Run it before |
|---|---|---|
| `/prime-memory` | The memory-pipelines slice: heartbeat, daily reflection, weekly synthesis, dream consolidation, session flush, search/indexing, working memory, episodes, entity compilation. Reads `.claude/sections/03_memory_pipelines.md` + the `memory_*.py` / `living_memory.py` / `episodes.py` / `entity_extractor.py` files. | Any task that touches a scheduled memory pipeline, the search/index layer, or working-memory / episode behavior. |
| `/prime-orchestration` | The convoy/mailbox + team slice: the frozen contract, data model, persistence, convoy/mailbox services, executor adapters, team services, the local API on port 4322. Reads `.claude/sections/07_orchestration.md` + the `orchestration/` files. | Any task that touches convoy/subtask state, the executor boundary, team coordination, or the orchestration HTTP API. |
| `/prime-signal` | The cognition + "Living Self" slice: recall pipeline, region assembly, the cognitive pass, operator-belief extraction, the contradiction engine, the evidence gate + amendments, and the self-evolution loop. Reads `.claude/sections/03_memory_pipelines.md` (Living Self section) + the `cognition/` and `evolve/` files + the Living Self manual. | Any task that touches recall, belief formation, the cognitive pass, or evidence-gated identity-file amendments. |
| `/prime-workflows` | The Archon workflows slice: workflow YAML DAGs, the PIV command suite (prime → plan → implement → validate → commit), validation/review commands, the Ralph state pattern. Reads `.archon/config.yaml` + the workflow YAMLs + the PIV command files. | Launching a Ralph DAG or a PIV loop, authoring a new workflow, or wiring a new command. |

Why this is the context economy in practice: a prime command costs one focused
read pass instead of an open-ended exploration of the whole repo, and the agent
that follows starts a Ralph iteration or a PIV plan already holding the right
slice — not re-deriving it on every fresh-context iteration.

### Note on command locations

The canonical prime commands live under `.claude/commands/core_piv_loop/`
(alongside the PIV `prime.md`, `plan-feature.md`, `execute.md`). Near-duplicate
top-level copies also exist at `.claude/commands/prime-*.md` for direct
invocation. If you edit one, reconcile the other — they describe the same slices
and should not drift in their file lists.

## brownfield-day-1

**Purpose:** take a repo with no AI layer (no `CLAUDE.md`, no `.claude/`) and
produce a working AI layer — global rules plus a customized command suite — in a
single agent invocation. Doubles as a client-onboarding deliverable: drop an AI
layer onto a brownfield repo in one run.

**Precondition (hard gate):** if `CLAUDE.md` already exists, the skill **stops**
and tells you the repo is already onboarded — it points you at editing the
existing files directly, or at `vertical-slice-audit` to score a specific slice.
It only proceeds when no AI layer is present (Type A brownfield).

**How it's invoked:** run the skill and (optionally) pass a `template_path`
pointing at a reference `CLAUDE.md` + `.claude/commands/` to copy and customize
from. If no path is given, it falls back to a built-in per-repo onboarding
template. You send one prompt; the skill orchestrates everything — you do not
dispatch the sub-agents yourself.

**What it does, in order:**

| Step | Action |
|---|---|
| 0 — Initial scan | Reads `README.md`, walks the top-level folder tree, reads root config files (`package.json` / `pyproject.toml` / `Cargo.toml` / `go.mod` / etc.), glances at 1-2 high-traffic files. |
| 1 — Plan the slicing | Decides how many Explorer sub-agents to dispatch — **dynamic, not fixed**: ~3 for a small repo (1-5K LOC), 4-6 for medium (5-50K), 6-10 for a large monorepo (50K+), sliced by subsystem or package cluster. |
| 2 — Dispatch Explorers | Launches all N Explorer sub-agents in parallel (one message), one per slice (backend, frontend, infra, tests, domain, CLI, DB, …). Each writes a ~500-700 word report describing **what IS** (no recommendations), citing real file paths. |
| 3 — Converge | Merges the reports into a single `codebase-analysis.md` (elevator pitch, domain concepts, stack table, annotated folder tree, conventions, most-substantial files, integrations, gotchas, seams, schema, key-files cheat sheet). |
| 4 — Dispatch Builders | Launches 3 Builder sub-agents in parallel: **A** writes `CLAUDE.md` (up to 10 sections, real paths, 200-400 lines); **B** copies + customizes the prime command family (a generic `prime.md` + 2-4 codebase-specific `prime-<slice>.md` variants, each listing that slice's key files); **C** copies + customizes the PIV + workflow commands (`plan` / `implement` / `commit` edited with the repo's real validation/test/lint commands, plus a fresh `validate.md` wrapping the full validation chain). |
| 5 — Final report | Prints the generated layer (CLAUDE.md line count, each command, totals) and recommended next steps. |

**What it produces:** one `CLAUDE.md` + a `.claude/commands/` suite — a generic
`prime.md`, per-slice `prime-<slice>.md` variants, `plan.md`, `implement.md`,
`validate.md`, and `commit.md`.

**Design rules it follows:** capture what IS (descriptive, never a refactor
proposal); cite real paths (no placeholders); copy + customize template commands
rather than regenerating them, but generate fresh for `CLAUDE.md`, `validate.md`,
and the codebase-specific prime variants.

**Flat-import caveat:** if the target uses flat `sys.path` imports (launchers
put a directory on `sys.path` instead of packaging it), the skill records that
in the generated `CLAUDE.md` — static import resolvers will report false orphans
in such directories, so "dead code" verdicts there require grep confirmation,
not resolver output. This is a deliberate design choice, not a defect.

**Out of scope:** refactoring decisions, new-skill creation, team rollout, and
Type B brownfield (a repo that already has an AI layer — update those files
directly, or score a slice with `vertical-slice-audit`).

## vertical-slice-audit

**Purpose:** score one feature/folder against the Vertical Slice Architecture
checklist, classify it, and — if it falls short — produce a step-by-step
migration plan. VSA is the codebase-level optimization that lets an agent load
one slice end-to-end and ship, instead of chasing cross-folder context across a
layered architecture. (This is the same property the `/prime-*` commands exploit
— a high-scoring slice is one a single `/prime-<feature>` can load in one pass.)

**How it's invoked:** run the skill and pass the feature folder to audit. If no
path is given, it asks which feature to audit before proceeding. It audits **one
feature at a time** — not the whole codebase at once.

**The 10-row checklist** — each row scored PASS or FAIL with the actual file
path cited as evidence:

| # | Dimension | PASS condition |
|---|---|---|
| 1 | Own folder | All the feature's code lives under one top-level folder |
| 2 | Models local | `<feature>/models.{py,ts}` exists (not a shared top-level folder) |
| 3 | Schemas local | `<feature>/schemas.{py,ts}` exists |
| 4 | Routes local | `<feature>/routes.{py,ts}` (or routes registered from here) |
| 5 | Business logic local | `<feature>/service.{py,ts}` holds the substance (not scattered in helpers/utils/lib) |
| 6 | Errors local | `<feature>/errors.{py,ts}` defines feature-specific exceptions |
| 7 | Tests colocated | `<feature>/tests/` sits next to the source |
| 8 | Public API explicit | An `index.{ts}` / `__init__.py` controls what's exported |
| 9 | Internal helpers stay internal | Private functions are not exposed via the public API |
| 10 | Cross-feature deps minimal | Imports from fewer than 3 other features (excluding shared utils) |

**Scoring tiers:**

| Tier | Score | Meaning |
|---|---|---|
| AI-friendly | 9-10 PASS | The reference shape. Other features should migrate toward it. |
| Partially migrated | 6-8 PASS | Usually missing colocated tests, an explicit public API, or local errors. Quick wins available. |
| Horizontally organized | ≤5 PASS | Migration is meaningful but incremental. |

**Flat-import caveat (rows 8-9):** in a codebase that deliberately uses flat
`sys.path` imports, rows 8-9 (explicit public API, internal helpers stay
internal) FAIL by design. When the skill detects flat-import conventions
(launchers manipulating `sys.path`, absent `__init__.py`, or documentation
noting flat imports), it scores those two rows as **CAVEAT** instead of FAIL and
does not count them against the total — a deliberate architecture choice, not a
defect.

**Output:** a result block with the score and tier, the per-row checklist table
(result + file-path evidence each), a one-paragraph verdict, and — only when the
score is below 9/10 — a migration plan. The migration plan walks creating the
slice folder, moving code in least-risky-first (models → schemas → errors →
service → routes → tests, validating + committing after each step), defining the
public API, updating imports across the codebase (the "blast radius" step), and
updating the AI layer. It also names what **not** to migrate (auth middleware,
logging, request correlation, DB connections/session management, HTTP framework
setup, domain-free utilities — all stay shared) and estimates effort
(lines moved, files touched, risk level).

**Out of scope:** it does not refactor code itself (plan only — execution is a
separate session), does not audit the whole codebase at once, and is not the
right metric for codebases that use hexagonal/clean architecture by design.

## Source Of Truth Files

| File | Role |
|---|---|
| `.claude/commands/core_piv_loop/prime-memory.md` | Canonical `/prime-memory` definition (memory-pipelines slice) |
| `.claude/commands/core_piv_loop/prime-orchestration.md` | Canonical `/prime-orchestration` definition (orchestration slice) |
| `.claude/commands/core_piv_loop/prime-signal.md` | Canonical `/prime-signal` definition (cognition / Living Self slice) |
| `.claude/commands/core_piv_loop/prime-workflows.md` | Canonical `/prime-workflows` definition (Archon workflows slice) |
| `.claude/commands/prime-*.md` | Top-level near-duplicate copies for direct invocation |
| `.claude/skills/brownfield-day-1/SKILL.md` | The brownfield onboarding flow (explore → converge → build the AI layer) |
| `.claude/skills/vertical-slice-audit/SKILL.md` | The VSA scoring + migration-plan skill |
| `.claude/sections/03_memory_pipelines.md` | Architecture doc read by `/prime-memory` and `/prime-signal` |
| `.claude/sections/07_orchestration.md` | Architecture doc read by `/prime-orchestration` |
| `.archon/config.yaml` | Project config read by `/prime-workflows` |

## Safety Boundaries

These tools are **read / context-only** with one bounded exception:

- The `/prime-*` commands only **read source and summarize**. They mutate
  nothing — no files, no external state, no commands run against the world.
- `vertical-slice-audit` only **reads and scores**. It produces a plan; it never
  refactors code on its own.
- `brownfield-day-1` is the one tool that **writes** — but only inside the target
  repo's working tree (`CLAUDE.md` + `.claude/commands/`, plus scratch files
  under a `tmp/` analysis folder). It touches no external service, sends no
  network mutation, and is hard-gated: it refuses to run if a `CLAUDE.md` already
  exists, so it can't clobber an existing AI layer. Its output is meant to be
  reviewed before you keep it — the agent can get a path wrong.

None of the three is a default-deny external-mutation surface (no posting,
sending, DMing, or account writes). They load context or generate local docs.

## How To Run It

Prime a slice before a coding turn:

```
/prime-orchestration
```

Then launch the actual work (e.g. a Ralph DAG or a PIV loop) in the now-oriented
session.

Onboard a brownfield repo (run from inside the target repo):

```
Invoke the brownfield-day-1 skill, optionally passing a template path:
  brownfield-day-1 <path-to-reference-template>
```

Audit one feature and get a migration plan if it falls short:

```
Invoke the vertical-slice-audit skill, passing the feature folder:
  vertical-slice-audit <path-to-feature-folder>
```

## How To Test It

- **Prime commands** — run each in a fresh session and confirm the output
  overview names the right slice boundaries, invariants, and current state, and
  that the files it pulled are the slice's real key files (cross-check against
  the `@`-referenced file list in the command). A prime command that prints a
  summary citing files that do not exist is a regression.
- **brownfield-day-1** — point it at a repo (or a copy) that has no `CLAUDE.md`
  and confirm: (1) it refuses when `CLAUDE.md` already exists; (2) on a clean
  repo it produces a `CLAUDE.md` + the prime/PIV command suite; (3) every path
  cited in the generated files actually resolves (no placeholders). Spot-check
  that the generated `validate.md` names the repo's real test/lint commands.
- **vertical-slice-audit** — run it against a known-good slice (expect a 9-10
  / AI-friendly score) and a known-horizontal feature (expect ≤5 with a
  migration plan), and verify each checklist row cites a real file path or notes
  its absence. On a flat-`sys.path` codebase, confirm rows 8-9 are scored CAVEAT
  rather than FAIL and excluded from the total.

## Public Export Status

Public-safe. The prime commands and both skills are mechanism-only developer
tooling — no personal data, tenant names, account IDs, or house paths in the
canonical files. (The brownfield skill references a per-repo onboarding
template; the template mechanism is generic — any reference template path can be
supplied.)

This manual page is **not yet wired into the public export**. `docs/` is in the
sanitizer's `DENY_DIRS`, so each manual page ships only via a surgical per-file
lift. To export this page, add it to `INCLUDE_FILES` in `scripts/sanitize.py`:

```
"docs/manual/features/context-economy-dx.md",
```

and add a born-clean regression assertion to `scripts/sanitize_test.py` (the
same pattern used for `episodes.md` and `session-opening-brief.md`). Until then
the page exists privately and does not appear in the public framework repo.

## Next Slices

- **Reconcile the duplicate prime copies** — collapse the top-level
  `.claude/commands/prime-*.md` into the canonical `core_piv_loop/` versions (or
  make one a thin pointer) so the two cannot drift in their file lists.
- **Add the missing prime variants** — there are slices without a dedicated
  prime command (e.g. the chat/runtime slice, the integrations slice, the
  dashboard slice). Add `prime-*` commands as those slices stabilize.
- **Wire this page into `INCLUDE_FILES`** plus a born-clean test so the
  context-economy DX bundle is documented in the public framework manual.
