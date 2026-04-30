# CLUTCH v3 — Adversarial Review Cycle

This reference doc defines the **pre-build** and **post-build** adversarial review gates added in CLUTCH v3. The cycle is invoked between PRP generation (Step 1) and execution strategy choice (Step 2), and again between debug loop (Step 5) and team shutdown (Step 6).

## Why this exists

CLUTCH v2 generates PRPs via a fresh research agent, then trusts the PRP and executes against it. This works for low-stakes phases but lets design flaws slip through to committed code. The W17 ship rhythm (`vault/memory/MEMORY.md` Recent Decisions) validated post-build adversarial review as a class-bug-catcher; CLUTCH v3 makes adversarial review a first-class CLUTCH gate, both pre-build and post-build.

The pattern was proven manually on PRP-7 (Apr 2026): a 1640-line monolithic PRP went through R1 → R2 → R3 review cycles and surfaced 18 unique blockers — design flaws that would have become committed bugs without pre-build review. CLUTCH v3 codifies that pattern.

## When to use which mode

CLUTCH accepts an `--adversarial` flag:

| Mode | Pre-build | Post-build | When to use |
|---|---|---|---|
| `--adversarial=no` | skip | skip | Trivial phases (typo fixes, doc updates, single-line config). Legacy CLUTCH v2 behavior. |
| `--adversarial=yes` (default) | R1 + revise + R2 + greenlight gate | one round of diff review | Standard phases. |
| `--adversarial=aggressive` | R1 + revise + R2; if R2 = REJECT, R3 + revise + R4 | up to 2 rounds of diff review with fix loops | High-stakes phases (security, schema migrations, cross-repo contracts, framework foundations). |

Default `--adversarial=yes` for any new phase. Drop to `=no` for clearly-trivial work, escalate to `=aggressive` when phase scope justifies it.

## Pre-build cycle (Step 1c)

Runs after PRP generation (Step 1) and budget gate (Step 1b), before strategy choice (Step 2).

### Step 1c.i — R1 Adversarial Review

The orchestrator constructs an R1 prompt by reading `{CLUTCH_DIR}/skills/clutch/assets/r1-adversarial-template.md` and substituting:

- `{PRP_PATH}` — path to the just-generated PRP
- `{PRD_PATH}` — path to the parent PRD
- `{PHASE_N}` — phase number
- `{PROJECT_PATH}` — project root for repo verification

Then the orchestrator fires codex via Bash:

```bash
codex exec --skip-git-repo-check < /tmp/clutch-r1-prompt-{PHASE_N}.txt \
  > {PROJECT_PATH}/PRPs/planning/{PRD_NAME}-phase-{PHASE_N}-adversarial-r1.md 2>&1
```

R1 output is structured:

- `## Verdict` — one of `ADOPT`, `ADOPT-WITH-FIXES`, `REJECT`
- `## 🔴 Blockers` — class-of-bug findings that must be fixed
- `## 🟡 Majors` — significant concerns worth addressing
- `## 🟢 Minors` — nice-to-haves

If R1 verdict = `ADOPT` (rare on first pass, but possible for trivial phases), skip Step 1c.ii (revise) and Step 1c.iii (R2). Proceed to Step 2.

If R1 verdict = `ADOPT-WITH-FIXES` or `REJECT`, proceed to Step 1c.ii.

### Step 1c.ii — Revise Per R1

Spawn a fresh `general-purpose` sub-agent via Task tool with prompt constructed from `{CLUTCH_DIR}/skills/clutch/assets/revise-template.md`:

```
REVISION MISSION — Phase {N}, R1 → R2

PRP Path: {PRP_PATH}
R1 Review: {R1_PATH}

Read both files. Apply every R1 blocker fix. Apply R1 majors that improve safety
or correctness. Drop R1 minors that are stylistic.

For each blocker, edit the PRP in place. After all fixes, append a "## R1 Disposition"
section listing each blocker with: addressed (proof) | deferred (reason) |
not-applicable (reason).

Stamp `Revised: <date>` at top. Keep total length under 1.5x original.
```

Wait for sub-agent completion. Verify via shape gate:

```bash
grep -q "Revised:" {PRP_PATH} && grep -q "R1 Disposition" {PRP_PATH} || echo "REVISION FAILED"
```

If revision shape gate fails, escalate to user.

### Step 1c.iii — R2 Adversarial Review

Same shape as Step 1c.i but the prompt constructed from `{CLUTCH_DIR}/skills/clutch/assets/r2-adversarial-template.md` instructs Codex to **verify R1 closure** AND **find new issues introduced by the revision**.

Output saved to `{PROJECT_PATH}/PRPs/planning/{PRD_NAME}-phase-{PHASE_N}-adversarial-r2.md`.

### Step 1c.iv — Greenlight Gate

Read R2 verdict.

| R2 Verdict | Action |
|---|---|
| `ADOPT` | Auto-proceed to Step 2. |
| `ADOPT-WITH-FIXES` | List majors to user as informational, auto-proceed to Step 2 unless user interrupts. |
| `REJECT` (`--adversarial=yes`) | **Escalate to user**. Present R1 + R2 verdict summaries. Ask: "Revise again? Split phase? Accept with explicit debt list?" |
| `REJECT` (`--adversarial=aggressive`) | Auto-proceed to R3 + revise + R4 cycle (Step 1c.v through 1c.viii). If R4 still REJECT, escalate to user. |

If user picks "split phase" at the escalation gate, CLUTCH writes the current PRP to `archive/` and asks the user to update the PRD with sub-phases, then re-runs Step 1 from the beginning. If user picks "accept with explicit debt", the unresolved blockers go into the PRP as `## R3 Disposition: deferred to follow-up` and execution proceeds.

### Step 1c.v-1c.viii (aggressive mode only)

R3 review → revise per R3 → R4 review → final gate. Same shape as 1c.i-1c.iii but parameterized for round 3 and round 4.

## Post-build cycle (Step 5a)

Runs after debug loop converges (Step 5 returns PASS) and before team shutdown (Step 6).

### Step 5a.i — Diff Adversarial Review

The orchestrator captures the diff for this phase:

```bash
DIFF_RANGE=$(git rev-parse HEAD~$WORKSTREAM_COUNT)..HEAD
git diff $DIFF_RANGE > /tmp/clutch-phase-{N}-diff.patch
```

Then constructs a post-build adversarial prompt from `{CLUTCH_DIR}/skills/clutch/assets/post-build-adversarial-template.md` and fires:

```bash
codex exec --skip-git-repo-check < /tmp/clutch-post-build-prompt-{N}.txt \
  > {PROJECT_PATH}/PRPs/planning/{PRD_NAME}-phase-{N}-adversarial-post-build.md 2>&1
```

The post-build prompt asks Codex to:

- Verify the diff actually implements the PRP (data-flow inspection, not just file-presence check)
- Check for class-of-bug patterns (Rule 1 / Rule 2 / Rule 3 violations from MEMORY.md global rules)
- Check for security regressions (secrets in commits, broken auth flows)
- Check for test gas-stations (tests that pass without exercising the target)
- Verify the PRP's acceptance criteria pass with the actual implementation

Output verdict: `PASS`, `FIX-REQUIRED`, or `BLOCKER`.

### Step 5a.ii — Fix Loop (if FIX-REQUIRED)

If verdict = `FIX-REQUIRED`, spawn `piv-debugger` sub-agent with the post-build review as input:

```
POST-BUILD FIX MISSION — Phase {N}

PRP Path: {PRP_PATH}
Post-build adversarial review: {POST_BUILD_PATH}

Apply every fix the review demands. Re-run the validator after fixes.
Output FIX REPORT with status, files changed, tests added.
```

After fixes, re-run Step 5a.i. Max 2 fix iterations. If still FIX-REQUIRED after 2 iterations, escalate to user.

### Step 5a.iii — Final Gate

| Verdict | Action |
|---|---|
| `PASS` | Proceed to Step 6 (shutdown team), Step 7 (workstream enforcement), Step 8 (commit). |
| `BLOCKER` | Escalate to user. The diff has a fundamental problem the fix loop can't address. User picks: revert phase, split into smaller phase, accept with explicit debt. |

## Adversarial state location

All adversarial artifacts live under `{PROJECT_PATH}/PRPs/planning/`:

```
PRPs/planning/
├── {PRD_NAME}-phase-{N}-analysis.md              (existing — codebase analysis)
├── {PRD_NAME}-phase-{N}-adversarial-r1.md        (NEW — R1 review)
├── {PRD_NAME}-phase-{N}-adversarial-r2.md        (NEW — R2 review)
├── {PRD_NAME}-phase-{N}-adversarial-post-build.md (NEW — post-build review)
└── {PRD_NAME}-phase-{N}-r1-disposition.md         (optional — extracted from PRP for quick reference)
```

These are generated artifacts; not committed by default but visible to follow-up sessions and to humans reviewing the workflow.

## Codex CLI requirements

CLUTCH v3 assumes `codex` is on PATH. If `command -v codex` returns non-zero, CLUTCH falls back to `--adversarial=no` for the affected phase with a warning to the user. Do not error out — let the build proceed degraded.

On Windows specifically, the canonical invocation is `codex exec --skip-git-repo-check < {prompt-file}` (per `feedback_codex_companion_enobufs.md`). The `codex:adversarial-review` skill has known ENOBUFS issues on Windows; CLUTCH v3 calls `codex exec` directly via Bash to avoid that.

## What this does NOT do

- **Does not replace the validator agent.** The validator (Step 4) checks "does the code match the PRP?" The adversarial reviewer checks "does the design have flaws independent of the PRP?" Both run.
- **Does not run during Discovery Mode.** PRD generation is collaborative; adversarial review at that stage is premature.
- **Does not run R3 by default.** Two rounds is the standard. R3 only fires under `--adversarial=aggressive`.
- **Does not auto-commit on REJECT.** REJECT always escalates to user — CLUTCH won't push code with known unresolved blockers.

## Failure modes

| Failure | Action |
|---|---|
| Codex CLI not installed | Warn user; fall back to `--adversarial=no` for this phase only |
| R1 output empty (Codex error) | Retry once; if still empty, escalate |
| Revision sub-agent fails to update PRP | Read sub-agent output for context; escalate |
| R2 finds new blockers introduced by revision | Continue per gate logic (REJECT → escalate or R3) |
| Post-build review identifies test gas-station | FIX-REQUIRED → fix loop |
| Post-build verdict still BLOCKER after 2 fix iterations | Escalate; do not commit |
