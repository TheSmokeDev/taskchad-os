# Polish Architecture And Execution Program

Status: execution foundation shipped; architecture target adoption in progress
Owner: architecture, Archon workflow, and subsystem owners
Last updated: 2026-07-10

## What It Does

The polish program gives The Homie one normative architecture target and a
bounded way to implement it. The
[TaskChad OS Polish Architecture Specification](../../specs/taskchad-os-polish-architecture-spec.md)
defines the destination; it does **not** assert that the repository already
conforms. The merged `implement-prp` Archon workflow turns one reviewed PRP at
a time into isolated, test-first work with deterministic gates and two human
approvals. Product behavior changes only when a resulting implementation is
separately reviewed and merged.

## Operator Entry Points

- Chat/Telegram: no dedicated polish-program command.
- CLI: `archon workflow run implement-prp --branch <branch> <prp-path>` from a
  regular shell; `archon workflow status` to observe a run.
- Dashboard/API: no polish conformance control surface is shipped.
- Documents: the canonical specification, one bounded PRP, and the resulting
  pull request are the operator's planning and review surfaces.

## Architecture And Evidence Are Separate Axes

The specification scores a named scope against cumulative architecture levels
L0-L6 using the `polish-architecture-v1` requirement map and an applicability
manifest. Evidence is reported independently as `declared`,
`structurally-probed`, `unit-proven`, `integration-proven`, `externally-live`,
or `production-exercised`.

Strong evidence for a legacy path does not prove target-architecture
conformance. Conversely, implemented controls without the evidence floor are
not a claimable level. A public label must name both axes, scope, environment,
observation time, and limitations. There is currently no repository-wide
Proof Manifest establishing a product-wide architecture level, so this manual
makes no such claim.

## Roles And Responsibilities

- **Hermes-inspired discipline** supplies useful lifecycle ideas: bounded
  context, explicit tool use, verification, and operator ergonomics. Hermes is
  neither the architecture authority nor a source of implicit product proof.
- **Archon** is the deterministic coding-workflow engine. It creates an
  isolated worktree, executes the declared gates, and publishes only after
  final operator approval. It does not decide product identity or auto-merge.
- **The Homie** remains the identity-first runtime and product. Its trusted
  domain code owns identity, policy, execution, durable state, and proof. A
  model may propose; deterministic code validates and applies.
- **The operator** approves the implementation plan and, separately, the exact
  validated package before commit/push/PR creation. Merge remains outside the
  workflow and human-controlled.

## Bounded PRP Lifecycle And Gates

One run accepts exactly one repository-relative PRP and proceeds as follows:

1. Require a clean, attached branch in an Archon-created linked worktree and
   record its baseline.
2. Preflight the PRP into bounded allowed paths plus explicit focused and
   regression commands; stop unless the decision is `proceed`.
3. Perform fresh-context reconnaissance and stop unless it is `ready`.
4. Produce a plan and wait at the first operator approval gate.
5. Implement test-first, then require recorded red/green evidence.
6. Run allowlisted focused tests and deterministic regression checks.
7. Run parallel specification, security/state, simplification, and
   documentation reviews; every review must pass.
8. Package the exact changed-file set, test evidence, risks, and rollback
   without publishing. Verify scope and bind the validated diff digest.
9. Wait at the final operator approval gate. Only the following deterministic
   node may explicitly stage, commit, push, and open a pull request.

A rejected or failed gate does not silently widen scope or declare success.
The workflow has no auto-merge node. For the command catalog and general
worktree model, see [Archon Workflows](archon-workflows.md).

## Source Of Truth Files

| Layer | Files |
|---|---|
| Normative architecture | `docs/specs/taskchad-os-polish-architecture-spec.md` |
| Source assessment | `docs/specs/taskchad-os-hermes-polish-assessment.md` |
| Execution workflow | `.archon/workflows/implement-prp.yaml` |
| Gate commands | `.archon/commands/prp-*.md` |
| Initial bounded program | `docs/prps/PRP-001-amendment-aware-rollback.md`, `docs/prps/PRP-001A-domain-rollback-service.md` through `PRP-001D-dashboard-rollback-ui.md` |
| Operator docs | `docs/manual/features/polish-architecture-execution-program.md`, [Amendment-Aware Rollback](amendment-aware-rollback.md) |

## Safety Boundaries

- A normative target is not a shipped implementation or conformance result.
- The workflow must run in a clean linked worktree with a bounded path allowlist;
  direct-checkout execution is forbidden for this program.
- Focused and regression commands are structured argv from an allowlisted set,
  not arbitrary shell strings.
- Test output, review prose, dispatch, and model assertions do not independently
  prove completion. Deterministic gates inspect authoritative artifacts and
  command exit codes.
- Plan approval does not authorize publication. Final approval binds the exact
  changed-file set and validated diff digest; merge remains a later human act.
- Artifacts and public docs must not expose secrets, local paths, profile data,
  private handoffs, or raw live state.

## How To Run It

```bash
# Run from a regular shell. Archon creates the linked worktree for --branch.
archon workflow run implement-prp --branch feat/<bounded-slice> docs/prps/<one-prp>.md
archon workflow status
```

Do not use `--no-worktree`. Before either approval, inspect the plan or package
against the PRP, allowed paths, tests, safety constraints, and backout.

## How To Test It

The execution workflow validates each PRP's own focused and regression commands.
For documentation-only changes, also run the repository's available Markdown
link/path checker and:

```bash
git diff --check
```

A workflow run is not proof of the implemented product slice until all gates
pass and the resulting pull request is reviewed and merged.

## Latest Live Proof

- Date: 2026-07-10
- Surface: execution-foundation repository source and merge history; **not an
  external or product-behavior live proof**
- Result: the canonical specification, bounded PRP set, gate command contracts,
  and `implement-prp` workflow are merged in foundation PR #9. This proves the
  execution foundation is present in the repository; it does not prove a
  product-wide architecture level or completion of PRP-001.
- Proof docs/artifacts: the source-of-truth files above and merge commit
  `5088d23`. Workflow run artifacts remain run-scoped and are not copied into
  this public manual.

## Source/Test/Proof Traceability

| Claim | Source anchor | Validation/proof boundary |
|---|---|---|
| Architecture target and scoring rules exist | specification §§1, 8, 11, 13-15 | Merged source; architecture is currently a declared target, not repository-wide conformance proof |
| PRPs execute in a bounded linked worktree | `implement-prp.yaml` worktree, preflight, and package gates | Workflow source is merged; each run must produce its own baseline, scope, test, review, and package artifacts |
| Two operator approvals and no auto-merge | `plan-approval`, `final-approval`, `publish-pr` nodes | Source inspection; publication follows final approval, while merge is not a workflow node |
| Foundation is shipped | foundation PR #9 / commit `5088d23` | Merged files listed above; no product behavior claim follows from this merge |
| Amendment rollback is the first implementation program | PRP-001 epic and A-D slices | PRP-001A is ready for bounded implementation; see the rollback manual for the unshipped status and acceptance gates |

## Public Export Status

Public-framework safe. It contains portable repository-relative anchors and no
run artifact, account detail, credential, private handoff, or machine-specific
path.

## Next Slices

- Complete and independently prove PRP-001A before beginning B-D.
- Generate scoped applicability and Proof Manifests before claiming an
  architecture level.
- Continue the specification's dependency order without creating parallel
  domain authorities.
