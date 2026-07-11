# Archon Workflows

Status: Active baseline; autonomous pipeline live-proven (overnight push, 2026-06)
Owner: `.archon/` (config, workflows, commands) + an internal Archon reference doc in the memory vault (private)
Last updated: 2026-06-19

## What It Does

Archon is the deterministic coding-workflow engine for The Homie. The Homie is
the brain — the always-on runtime, memory, and reasoning. Archon is the hands —
deterministic, multi-step coding work (feature development, PR review, vault
repair) run inside isolated git worktrees so the main branch is never touched
mid-run.

Each workflow is a YAML-declared DAG of nodes that runs in its own worktree
branched off `master` (the base branch is set in `.archon/config.yaml`). A run
gets a clean, throwaway environment: if something goes wrong, you delete the
worktree and `master` is exactly where it was. Workflows are picked by intent —
autonomous build, human-gated build, automated review, or vault repair — and the
catalog below maps each intent to the workflow that owns it.

Archon is a coding-workflow surface that an operator invokes. It is not a
runtime task scheduler (that is Convoy/Mailbox — see the comparison below) and
it does not auto-dispatch, auto-merge, or run unattended against tracked repos.
The merged `implement-prp` workflow is a stricter bounded implementation
program with deterministic artifacts and two operator approvals; see
[Polish Architecture And Execution Program](polish-architecture-execution-program.md).
Its current PRP-001A pilot does not make amendment rollback a shipped feature.

## When To Use Which Workflow

These are the operator-facing workflows. The live list (`archon workflow list`)
is always authoritative and may include more than this table.

| Trigger | Workflow | What it does |
|---|---|---|
| "build this feature", "implement this idea", "run ralph" | `archon-ralph-dag` | Autonomous. Turns a PRD or idea into a story list (`prd.json`), then implements ONE story per iteration with fresh context, validates (type-check / lint / tests), commits, and repeats until every story passes. |
| "let's build X together", "guided dev", "PIV loop" | `archon-piv-loop` | Human-gated. Explore → Plan (you approve) → Implement → Validate (you approve). Use it when the approach matters and you want to steer. |
| "review this PR", "code review" | `archon-smart-pr-review` | Automated PR review against codebase standards. |
| "fix vault health", "vault A-grade" | `archon-vault-a-grade` | Fixes the tracked Obsidian vaults to a health target in parallel, then runs a verification pass. |
| "run the clutch gates on this PRD" | `archon-clutch` | The multi-gate review→implement pipeline: adversarial review → fix → parallel judges → synthesize → reality-check gate → execute → validate. See `intent-prd-and-clutch.md` for the full gate model. |
| "implement this one bounded PRP" | `implement-prp` | Linked-worktree-only, test-first PRP delivery with preflight/reconnaissance gates, focused and regression tests, four reviews, exact-diff packaging, and separate plan/publish approvals. It opens a PR only after final approval and never auto-merges. |

Rule of thumb: reach for `archon-ralph-dag` when you trust the spec and want it
built end-to-end; reach for `archon-piv-loop` when you want to approve the plan
and the result; reach for `archon-clutch` when a PRD needs adversarial review
before any code lands.

## Operator Entry Points / CLI

Run these from a regular shell (see the CLAUDECODE caveat under Safety
Boundaries):

```bash
archon workflow list                                 # see all available workflows
archon workflow list --json                          # machine-readable
archon workflow run <name> "<idea or PRD path>"      # start a workflow
archon workflow run <name> --branch <branch> "<msg>" # start with an explicit worktree branch
archon workflow status                               # check running workflows
archon workflow abandon <run-id>                     # stop a run
archon isolation list                                # show active worktrees
archon isolation cleanup --merged                    # remove worktrees merged into base
```

Worktree isolation is the default and the recommended mode — pass `--branch
<name>` so the run lands on an isolated branch instead of your working tree.
`--no-worktree` (direct checkout) exists but should only be used when you
explicitly want no isolation.

The framework also exposes a thin CLI wrapper for repo-aware dispatch
(`thehomie archon ...`); see `archon-repo-dispatch.md` for the
choose-the-repo-first operator pattern that precedes a run.

## The Ralph State Pattern

`archon-ralph-dag` is autonomous because its state lives on disk, not in the
model's context window. Two files per run carry everything forward:

| File | Holds |
|---|---|
| `.archon/ralph/{slug}/prd.json` | The story list, with `passes: true/false` per story. |
| `.archon/ralph/{slug}/progress.txt` | Learnings, patterns, and gotchas accumulated across iterations. |

Each iteration is a fresh-context loop step:

1. Read the state from disk (`prd.json` + `progress.txt`).
2. Implement exactly ONE not-yet-passing story.
3. Validate it (type-check + lint + tests).
4. Commit.
5. Update `prd.json` (flip that story's `passes` to `true`) and append learnings
   to `progress.txt`.
6. Exit.

The loop engine then starts the next iteration with a clean context window. Fresh
context every iteration is the point: it keeps each step focused on one story and
stops context bloat from degrading later work. The run ends when every story in
`prd.json` reports `passes: true`. You can start from a written PRD file or from a
one-line idea — given an idea, the first iteration generates the `prd.json` story
list itself.

## The Autonomous Pipeline (build → review → fix → merge)

How a feature actually ships when it runs unattended overnight:

1. **Build.** `archon-ralph-dag` works the story list in an isolated worktree,
   one story per fresh-context iteration, committing as each story passes its
   validation gate.
2. **Adversarial review.** The branch goes through cross-vendor review — a
   reviewer challenges the implementation against codebase standards and hunts
   for the class-level bugs that instance-level fixes miss (the same anti-pattern
   discipline the framework grep-enforces).
3. **Fix.** Review findings come back as concrete change instructions; a fix
   pass applies them.
4. **Re-review.** The fixed branch is reviewed again until it clears the gate.
5. **Merge.** Merge happens only after the review gate approves — it is the one
   step that stays human-confirmed (review-gated merge, never auto-merge).

The `archon-clutch` workflow is the in-engine, deterministic form of this same
build→review→fix→merge loop, with explicit gate nodes (adversarial review →
parallel judges → reality-check verdict → execute → validate). Use the loose
pipeline for overnight feature pushes; use `archon-clutch` when you want every
gate declared and enforced in one workflow run. Full gate detail lives in
`intent-prd-and-clutch.md`.

## Archon vs Convoy/Mailbox

These are different layers, not competitors. Don't reach for one when you mean
the other.

| | Archon | Convoy/Mailbox |
|---|---|---|
| Purpose | Coding workflows (feature dev, PR review, vault repair) | Runtime agent task coordination |
| Trigger | Developer invokes manually | Agent triggers programmatically |
| Runs as | Coding-agent workflow runs in git worktrees | Python service (local API on port 4322) |
| State | `prd.json` + `progress.txt` on disk | SQLite via the convoy service |

In short: Archon is the developer-invoked factory for writing code; Convoy is the
runtime's own task-coordination spine. (See `convoy-work-mailbox.md` for the
Convoy/Mailbox side.)

## Source Of Truth Files

| Layer | Files |
|---|---|
| Config | `.archon/config.yaml` (base branch / worktree settings) |
| Workflows | `.archon/workflows/*.yaml` (one DAG per workflow) |
| Commands | `.archon/commands/*.md` (prompt templates referenced by command nodes) |
| Ralph state | `.archon/ralph/{slug}/prd.json`, `.archon/ralph/{slug}/progress.txt` |
| Repo-dispatch CLI | `.claude/scripts/personas/archon.py`, `.claude/scripts/thehomie_cli.py` |
| Internal reference | `MEMORY_DIR/docs/ARCHON.md` — internal Archon reference (PRIVATE, not exported) |
| Public docs | `docs/manual/features/archon-workflows.md` |

## Safety Boundaries

- **Worktree isolation by default.** Every workflow runs on an isolated branch
  off the base branch; `master` is never modified mid-run. A failed run is
  discarded by deleting the worktree.
- **CLAUDECODE nesting caveat.** Running `archon workflow run` from inside a
  Claude Code session warns about `CLAUDECODE=1` nesting (a coding agent
  launching another coding agent). Run workflows from a regular shell to avoid
  the nested-session warning.
- **Review-gated merge.** The autonomous pipeline builds and reviews on its own,
  but merge stays a human-confirmed step gated on review approval. No auto-merge.
- **Developer-invoked only.** Archon does not run unattended against tracked
  repos on its own schedule. The operator starts each run, and repo selection
  stays explicit (see `archon-repo-dispatch.md`).
- **Long-running, one per shell.** Workflows are long-running and each blocks its
  shell; run multiple as separate background tasks, never combined into one
  invocation.

## How To Run It

```bash
# 1. See what's available
archon workflow list

# 2. Autonomous build from an idea (isolated worktree branch)
archon workflow run archon-ralph-dag --branch feat/<feature> "<feature idea>"

# 3. Autonomous build from an existing PRD file
archon workflow run archon-ralph-dag --branch feat/<feature> ".archon/ralph/<slug>/prd.md"

# 4. Guided, human-gated build
archon workflow run archon-piv-loop --branch piv/<feature> "<feature or issue>"

# 5. Check progress / clean up
archon workflow status
archon isolation cleanup --merged
```

## How To Test It

There is no unit-test suite for Archon itself — it is an external CLI. Validate a
run by its own output and the worktree it produces:

```bash
# Confirm the CLI is installed and the catalog loads
archon workflow list

# Inspect a run's on-disk state (autonomous runs)
cat .archon/ralph/<slug>/prd.json        # per-story passes: true/false
cat .archon/ralph/<slug>/progress.txt    # accumulated learnings

# Verify the produced branch the normal way before merging
git diff master..<run-branch> --stat
```

A workflow is "done" when its validation node passes (type-check + lint + tests
for build workflows) and, for `archon-ralph-dag`, every story in `prd.json`
reports `passes: true`.

## Public Export Status

This page is a public-safe, generic catalog of the Archon operating model — it
paraphrases the mechanism and carries no real repo slugs, dispatch history, or
operator context. The internal Archon reference doc in the memory vault is
PRIVATE and is never exported.

To ship this page publicly it must be added to `scripts/sanitize.py`
`INCLUDE_FILES` (the public mirror is produced only through the sanitizer; the
private repo stays the source of truth). Before publishing, inspect the
categorized public diff and confirm no runtime state, local paths, personal repo
names, or private workflow artifacts are present.

## Next Slices

- A convoy `WorkflowRunnerExecutor` that can dispatch an Archon workflow run as a
  runtime subtask, bridging the two layers.
- Public example workflow YAMLs (generic, secret-free) shipped alongside this
  catalog.
- An opt-in profile-owned repo-dispatch config so the choose-the-repo-first
  pattern (`archon-repo-dispatch.md`) can be validated by CLI.
