# Intent-PRD and Clutch Review

Status: Shipped (#78), merged to master
Owner: `.claude/commands/create-prd.md` (PRD gate) + `.archon/workflows/archon-clutch.yaml` (review DAG)
Last updated: 2026-06-19

## What It Does

Intent-PRD and Clutch are two halves of one discipline: write a specification
that can be proven wrong, then run it through a deterministic review pipeline
that refuses to implement until the plan is evidence-backed.

The PRD half is a gate. `create-prd` does not just write a document — before it
writes anything to disk, it validates the spec against a set of falsifiability
checks. A specification that cannot state how it would fail is rejected with a
named error, not silently accepted. The point is to catch a spec-in-disguise (a
wishlist with no falsifiable conditions) before it enters a workflow and burns
review and build cycles.

The Clutch half is a workflow. `archon-clutch` is a multi-gate
review-then-implement DAG: cross-vendor adversarial review, a three-judge
parallel panel, a structured reality check that emits a machine-readable
verdict, then a conditional execute node that only fires on `APPROVED`, followed
by an engine-owned validation node that runs the test suite. Every reasoning
node references a reusable `.archon/commands/*.md` command file, so the workflow
YAML stays an interface — the engine owns worktree creation, parallelism, retry,
and validation.

Together they enforce: a falsifiable bet goes in, a gated review decides if it
holds, and only an approved plan gets built.

## Operator Entry Points

| Surface | Entry point | What it does |
|---|---|---|
| PRD authoring | `/create-prd "<intent>"` (skill/command) | Writes an intent-PRD, but only after the validation gates pass |
| PRD authoring | `/create-prd <output-filename>` | Same gate; writes to the named file instead of the default `PRD.md` |
| Review + build | `archon workflow run archon-clutch <path-to-prd-or-plan>` | Runs the full review DAG in an isolated worktree; produces a PR if R3 approves |
| Review + build | `archon workflow list` | Confirms `archon-clutch` is loaded — the discovery loader is the validator (verify `errorCount: 0`) |

The two are designed to chain: author the PRD through the gate, then hand the
resulting file straight to `archon-clutch` as `$ARGUMENTS`.

## How It Works

### The intent-PRD gate

A traditional spec describes what to build. An intent-PRD additionally describes
what WRONG looks like — making acceptance binary instead of subjective. Five
sections carry the discipline:

| Section | What it asserts | Why it is mandatory |
|---|---|---|
| Success Hypothesis (RIGHT) | An assertive, testable outcome when the solution works | Forces a measurable claim, not a feeling |
| WRONG-Condition | At least 3 observable, binary failure modes; at least one silent | Defines failure so acceptance is not a judgment call |
| Non-Goals | Explicit anti-scope, each with a one-line rationale | Stops scope creep at authoring time |
| Scope | Numbered, verifiable deliverables (artifacts or behaviors) | Gives the build a concrete target list |
| Success Criteria | Step-by-step proof scripts that reference the deliverables | Maps each deliverable to a way to verify it |

The WRONG-Condition section is the crux. It must be falsifiable: each entry is
observable (there is evidence — a log line, a test result, a screenshot, a
metric) and binary (it happened or it did not — no gradients). At least one
entry must describe a silent failure — something that could ship without anyone
noticing — because loud failures (crashes, errors) are necessary but not
sufficient.

### The Clutch review DAG

`archon-clutch` runs the standard review ladder as an Archon DAG. R1 attacks the
plan from a different vendor than the one that wrote it (cross-vendor review
eliminates self-bias). R1-fix addresses the findings. R2 runs three independent
judges in parallel — each stays in a single lane — and a synthesize node folds
their findings into one review. R3 is the evidence gate: it emits a structured
verdict that the engine reads to decide whether execute runs at all. Execute is
conditional on that verdict. Validate is engine-owned and runs the test suite
after the build. Every reasoning node uses `context: fresh` so no prior turn
biases an independent reviewer.

## The Review DAG

`archon-clutch` declares `provider: claude`, `model: sonnet` at the workflow
level; individual nodes override as noted.

| Node | depends_on | What it does | Command file / engine |
|---|---|---|---|
| `r1-adversarial` | (none) | Adversarial review — attacks the plan for fatal flaws, missing edge cases, false assumptions. Runs cross-vendor (`provider: codex`). Emits JSON findings; default verdict `NEEDS_WORK`. | `archon-codex-adversarial-review` |
| `r1-fix` | `r1-adversarial` | Revises the plan to address every critical/major finding; writes a revised plan + a change log. Fixes substance, not style. | `archon-fix-from-review` |
| `r2-judge-correctness` | `r1-fix` | Correctness lens only — internal consistency, logical completeness, technical accuracy, contract satisfaction, state-machine validity. | `archon-judge-correctness` |
| `r2-judge-pragmatism` | `r1-fix` | Pragmatism lens only — is it practical to build and ship. Runs in parallel with the other two judges. | `archon-judge-pragmatism` |
| `r2-judge-edge-cases` | `r1-fix` | Edge-case lens only — what breaks on unusual input or boundary conditions. Runs in parallel. | `archon-judge-edge-cases` |
| `r2-synthesize` | all three R2 judges | Fan-in: folds the three judge outputs into one consolidated review. | `archon-synthesize-review` |
| `r3-reality-check` | `r2-synthesize` | Evidence gate — extracts every testable claim, classifies the evidence each needs, and emits a STRUCTURED verdict (see schema below). Aspirational language in acceptance criteria is an automatic `NEEDS_WORK`. | `archon-reality-check` |
| `execute` | `r3-reality-check` | Blessed implementation — reads the plan, follows existing code patterns, commits per logical unit, collects evidence, produces a PR. Gated `when: $r3-reality-check.output.verdict == 'APPROVED'`. | `archon-execute-plan` |
| `validate` | `execute` | Engine-owned bash node — runs the test suite (`pytest tests/ -x --tb=short`) and reports unstaged changes (`git diff --stat`). `timeout: 600000`. | inline bash (engine-owned) |

The R3 node is a structured-output node. Its `output_format` is the contract the
engine evaluates at the execute gate:

```yaml
output_format:
  type: object
  properties:
    verdict:        { type: string, enum: ["APPROVED", "NEEDS_WORK"] }
    confidence:     { type: number, minimum: 0.0, maximum: 1.0 }
    claims_verified:     { type: integer }
    claims_unverifiable: { type: integer }
    evidence_requirements: { type: array, items: { ... claim, evidence_type, verification_command, status } }
    blockers:              { type: array, items: { ... id, claim, reason, fix } }
    summary:        { type: string }
  required: [verdict, confidence, summary]
```

Because `verdict` is a required enum, the execute gate
(`when: $r3-reality-check.output.verdict == 'APPROVED'`) is a clean machine
decision — a `NEEDS_WORK` verdict means execute never runs.

## Source Of Truth Files

| Layer | Files |
|---|---|
| PRD gate | `.claude/commands/create-prd.md` |
| PRD spec/reference | `.claude/research/06-intent-prd.md` |
| Workflow DAG | `.archon/workflows/archon-clutch.yaml` |
| Workflow authoring reference | `.claude/research/15-authoring-archon-workflows.md` |
| R1 command | `.archon/commands/archon-codex-adversarial-review.md` |
| R1-fix command | `.archon/commands/archon-fix-from-review.md` |
| R2 judge commands | `.archon/commands/archon-judge-correctness.md`, `.archon/commands/archon-judge-pragmatism.md`, `.archon/commands/archon-judge-edge-cases.md` |
| R2 synthesize command | `.archon/commands/archon-synthesize-review.md` |
| R3 command | `.archon/commands/archon-reality-check.md` |
| Execute command | `.archon/commands/archon-execute-plan.md` |
| Public docs | `docs/manual/features/intent-prd-and-clutch.md` |

## Safety Boundaries

Three gates, each refusing to proceed on a defined condition:

1. **Reject-before-write (PRD gate).** `create-prd` validates the
   WRONG-Condition, Non-Goals, and Success Hypothesis sections BEFORE writing the
   PRD file. If any check fails it does NOT write the file — it outputs every
   error message (not just the first), names the failing entry number and the
   matched pattern, and asks the operator to revise. Concretely, the gate
   rejects a PRD that:
   - has no WRONG-Condition section, or fewer than 3 entries
   - uses aspirational adjectives (`reliably`, `properly`, `correctly`, …) or
     gradient/frequency words (`too slow`, `sometimes`, `mostly`, …) in a
     WRONG-Condition entry
   - has no silent-failure entry (every condition is a loud crash/error)
   - prescribes an implementation in a WRONG-Condition entry (`use X instead`,
     `implement Y`) — that is a spec-in-disguise, not a failure
   - has no Non-Goals section, or a non-goal with no rationale
   - uses hedging verbs (`may`, `might`, `should`, `hopefully`, …) in the
     Success Hypothesis, or a hypothesis with no concrete artifact to verify
   - (Warning only, overridable) a Scope/Success-Criteria coverage ratio below
     0.8 — flagged, not blocked.

2. **APPROVED-before-execute (R3 gate).** The `execute` node is conditional on
   `$r3-reality-check.output.verdict == 'APPROVED'`. The execute command also
   re-checks the verdict as a hard gate and outputs a `BLOCKED` status if R3 is
   `NEEDS_WORK`. Aspirational language in acceptance criteria forces
   `NEEDS_WORK`, so an unverifiable plan cannot reach implementation.

3. **Engine-owned validation.** The `validate` node is plain bash owned by the
   engine, not the model — it runs the test suite and surfaces unstaged changes.
   The build cannot self-certify; the tests run regardless of what the execute
   node claims.

This is the same family as the framework's other default-deny gates: the bet
must be falsifiable to be written, and the plan must be evidence-backed to be
built.

## How To Run It

Author a PRD through the gate, then hand it to Clutch:

```powershell
# 1. Author an intent-PRD (the gate rejects specs-in-disguise before writing)
#    Invoke the create-prd skill/command with your intent text.

# 2. Run the Clutch review + implement DAG against the resulting PRD or plan file
archon workflow run archon-clutch .archon/ralph/<your-feature>/prd.md

# Or against any plan file
archon workflow run archon-clutch <path-to-prd-or-plan>

# Confirm the workflow is loaded with no errors (the loader validates it)
archon workflow list
```

Bash equivalent:

```bash
archon workflow run archon-clutch ".archon/ralph/<your-feature>/prd.md"
archon workflow list
```

Archon creates a git worktree per run, runs the gates, and (if R3 approves)
produces a PR. Do not hand-roll branch/checkout/cleanup — worktree isolation is
automatic.

## How To Test It

- **PRD gate behavior** — the gate's own validation test cases live in
  `.claude/commands/create-prd.md` ("Validation Test Cases (Edge Cases)"). They
  cover: a good PRD passes all gates; a bad WRONG-Condition is rejected; a
  missing Non-Goals section is rejected; an aspirational hypothesis is rejected;
  a misaligned Scope/Acceptance ratio warns (not errors); a non-goal without a
  rationale is rejected; a WRONG-Condition with an implementation prescription is
  rejected. To exercise the gate, run `create-prd` against an intentionally
  malformed spec and confirm it emits the named error and does NOT write a file.

- **Workflow loads cleanly** — `archon workflow list` must show `archon-clutch`
  with `errorCount: 0` (the discovery loader is the validator — there is no
  separate `validate` subcommand).

- **Gate ordering** — confirm the `execute` node carries
  `when: $r3-reality-check.output.verdict == 'APPROVED'` and that
  `r3-reality-check` declares the required `verdict` enum in its
  `output_format`. A workflow where execute does not depend on the R3 verdict is
  a broken gate.

## Latest Live Proof

- Date: 2026-06-19
- Surface: Archon workflow loader
- Result: `archon workflow list` loads `archon-clutch` with `errorCount: 0`; the
  full DAG (R1 → R1-fix → R2 ×3 → synthesize → R3 → execute → validate) resolves
  with the R3-gated execute node intact.
- Proof artifact: merged commit `1100c53f` (PR #78); this manual page; the PRD
  gate's in-file validation test cases.

## Public Export Status

This page is public-safe by construction: it documents mechanism only — the PRD
section gates, the workflow node DAG, and repo-relative command-file paths. It
contains no personal repository names, account handles, emails, absolute user
paths, secret values, or operator/tenant context.

`docs/` is denied globally by the sanitizer, so this file must be added to the
`INCLUDE_FILES` allowlist in `scripts/sanitize.py` (each entry is per-file, no
globs — every addition gets adversarial public-export review) before it will
ship to the public mirror. `INCLUDE_FILES` overrides only `DENY_DIRS`; it never
bypasses `DENY_FILES`, `DENY_EXTENSIONS`, or `DENY_PATTERNS`. The public mirror
is produced through `scripts/sanitize.py`, never by copying files by hand.

Verify before publishing:

```powershell
uv run pytest scripts\sanitize_test.py -q
uv run python scripts\sanitize.py --dry-run
```

## Next Slices

- Add `docs/manual/features/intent-prd-and-clutch.md` to `INCLUDE_FILES` in
  `scripts/sanitize.py` with a born-clean regression test, then re-export.
- Add an optional structured `prd.json` emitter so the PRD gate's parsed sections
  feed straight into the Clutch DAG without a re-parse.
- Extend the R3 `output_format` consumers so `blockers` and
  `evidence_requirements` surface in the PR body as a verification table.
- Continue to defer any auto-merge: code review and merge remain operator
  actions even after a `validate` pass.

---

See also:
[Archon workflows](archon-workflows.md) — the broader catalog of Archon
workflow primitives and node types. ·
[Archon repo dispatch](archon-repo-dispatch.md) — choosing the right repository
context before starting a workflow.
