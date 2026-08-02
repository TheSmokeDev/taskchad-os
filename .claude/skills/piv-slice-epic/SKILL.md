---
name: piv-slice-epic
description: >
  Slice an epic (its intent PRD + its architecture decisions) into PIV-sized tickets and
  create them as GitHub Issues on the PRIVATE tracker (thehomie-framework/thehomie) with the
  dependency graph wired into the epic issue as a task list plus execution waves. The bridge
  from a strategic doc to the per-issue PIV loop. Use when the user says "slice this epic",
  "slice the PRP into issues", "cut this into tickets", "make the issues for this",
  "dependency graph the epic", or after /plan-architecture. Adapted technique from Cole
  Medin's TheHomie workshop (see NOTICE.md); private skill — excluded from public export.
---

# piv-slice-epic — Epic → PIV-Sized Issues on the Private Tracker

The epic is the destination; the PIV loop is the unit of motion; tickets are the bridge.
This skill cuts the epic into loop-sized tickets and files them as GitHub Issues — no
external tracker, just the repo, `gh`, and Issues. The epic issue's task list IS the live
dependency graph: `Closes #N` in each PR ticks its box on merge, so the graph maintains
itself.

## Step 0 — Repo guard (HARD GATE, run before anything else)

Issues from this skill go to the PRIVATE tracker ONLY. Verify before any `gh issue` call:

```bash
gh repo view --json nameWithOwner --jq .nameWithOwner
```

**Proceed only if the output is exactly `thehomie-framework/thehomie`.** Anything else (especially
the public `taskchad-os` export or a worktree with a different origin) → STOP and tell the
operator. Epic content routinely references private paths, personas, and business context —
it must never land on a public repo.

## Input

- `$1` — the intent doc: `PRDs/active/PRD-<slug>.md`, or a PRP that carries its own
  architecture decisions inline (e.g. `PRPs/active/PRP-pi-port-self-extension-and-lane.md`).
- `$2` (optional) — the architecture doc: `PRDs/active/PRD-<slug>.architecture.md`. This is
  load-bearing when it exists: it names the seams, the data shape, and the missing pieces
  the slices must respect. If the PRD links one and `$2` was omitted, follow the link.
- `--epic-issue <n>` (optional) — reuse an existing epic issue instead of creating one.

## Step 1 — Read the destination

From the intent doc: goal, acceptance criteria, non-goals. From the architecture doc (or
the inline architecture/decisions section): the approach, the named seams, missing pieces,
spikes. Slices have to land on those seams — that's what makes them independent.

## Step 2 — Orient on the code surface

If the session isn't already primed on the owning slice, explore just enough to judge
independence: which files each candidate ticket touches, which seams are shared. The
`/prime-*` commands help but aren't required; targeted reads from the architecture doc's
named seams are usually enough. In this repo, respect vertical-slice ownership — a ticket
that smears across `.claude/chat/`, `runtime/`, and `orchestration/` at once is usually two
tickets.

## Step 3 — Cut PIV-sized slices

**Size for an agent loop, not a human sprint board.** A well-cut ticket:

- Is ONE testable concern — provable on its own, reviewable in one honest pass.
- Is a vertical slice of behavior, never a horizontal layer.
- Carries its own acceptance criteria.
- Fits one focused loop without context rot: roughly a small-to-medium implementation
  phase, ~8-10 subtasks, ~500-1500 changed lines with 20-50% of that tests.

Split by dependency, by concern, or as a slim end-to-end slice. Too big to test honestly in
one pass → split again. A small epic can legitimately be a single ticket. Planning detail
stays high; it's the scope that's bigger than a classic ticket.

## Step 4 — Map dependencies into waves

Tickets that share no files and consume nothing from each other are parallel. Chains wait.
Mark every ticket `(no deps)` or `(depends on #x)` and group into execution waves —
independent waves can run as parallel Archon worktree dispatches per the repo dispatch
rules. **Plan just-in-time:** a dependent ticket gets its implementation plan only after its
dependency is IMPLEMENTED, not merely sliced — the build teaches the plan.

## Step 5 — Ensure the epic issue exists

With `--epic-issue <n>`, reuse it. Otherwise:

```bash
gh label create epic --color 5319E7 --description "Tracks an epic and its child tickets" 2>/dev/null || true
gh issue create --title "Epic: <name>" --label epic --body-file <path-to-intent-doc>
```

Capture the printed issue number as `$EPIC`. The epic body is the intent; the architecture
doc stays linked from inside it (two clean sources).

## Step 6 — Create one issue per slice

Independent slices FIRST, so their real numbers exist for the `Depends on` lines of the
dependent ones. Each issue carries its own context — a later loop must be able to pick it
up cold without re-reading the whole epic:

```bash
gh issue create --title "<slice title>" --body "$(cat <<'EOF'
Part of epic #<EPIC>.

## Scope (one testable concern)
<what this ticket delivers + its acceptance criteria>

## Per-ticket context
- Architecture decisions this inherits (inherit, don't re-decide — flag conflicts, never diverge silently)
- The seams/files it plugs into; the epic acceptance criteria it satisfies

## Size estimate
<~N lines incl. tests; the ~500-1500 band>

## Depends on
<none | #<issue>>
EOF
)"
```

## Step 7 — Wire the graph into the epic issue

Append the task list + waves to the epic body (`gh issue edit $EPIC --body "..."`):

```markdown
## Tickets
- [ ] #a — <title>   (no deps)
- [ ] #b — <title>   (no deps)
- [ ] #c — <title>   (depends on #a)

## Execution order
- Wave 1 (parallel): #a, #b
- Wave 2: #c   (after #a is implemented)
```

The task list is the live tracker: PRs that close with `Closes #<n>` tick boxes on merge.

## Step 8 — TRACKER pointer

`PRPs/active/TRACKER.md` stays the session source of truth. Add ONE line to the epic's
existing tracker entry (or a new short entry): epic issue URL + wave summary. The issue owns
the graph; TRACKER owns the pointer. Do not duplicate the ticket list into TRACKER.

## Then: each ticket runs its own loop

`core_piv_loop:plan-feature` (reads the issue, inherits the epic architecture) →
`core_piv_loop:execute` → `validation:validate` → `validation:code-review` → `/commit` →
PR with `Closes #<n>` in the body. PRs merge through the house gate — cross-vendor review,
orchestrator merges; never blind `gh pr merge`. Bug-shaped tickets take the RCA lane
(`github_bug_fix:rca` → `github_bug_fix:implement-fix`) instead.

## Output summary (print at the end)

Epic issue URL · each child `#n — title` · the waves · the TRACKER line added.
