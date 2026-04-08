---
name: clutch
description: "CLUTCH v2 — Claude Layered Unified Team Coordination Hub. Resilient multi-phase orchestration with fail-fast guards, context budgeting, structured checkpoints, and workstream completion enforcement. Parallel execution with Agent Teams when appropriate, sequential fallback when not. Triggers on: clutch, parallel execution, team execution, multi-agent."
disable-model-invocation: false
allowed-tools: Task, TaskCreate, TaskUpdate, TaskList, TeamCreate, TeamDelete, SendMessage, Read, Write, Bash, Glob, Grep
argument-hint: "[PRD_PATH|PROJECT_PATH] [START_PHASE] [END_PHASE]"
---

# CLUTCH v2 Orchestrator

> Claude Layered Unified Team Coordination Hub — Resilient Multi-Phase Orchestration

## Arguments: $ARGUMENTS

Parse arguments using this logic:

### PRD Path Mode (first argument ends with `.md`)

If the first argument ends with `.md`, it's a direct path to a PRD file:
- `PRD_PATH` - Direct path to the PRD file
- `PROJECT_PATH` - Derived by going up from PRDs/ folder
- `START_PHASE` - Second argument (default: 1)
- `END_PHASE` - Third argument (default: auto-detect from PRD)

### Project Path Mode

If the first argument does NOT end with `.md`:
- `PROJECT_PATH` - Absolute path to project (default: current working directory)
- `START_PHASE` - Second argument (default: 1)
- `END_PHASE` - Third argument (default: 4)
- `PRD_PATH` - Auto-discover from `PROJECT_PATH/PRDs/` folder

### Detection Logic

```
If $ARGUMENTS[0] ends with ".md":
  PRD_PATH = $ARGUMENTS[0]
  PROJECT_PATH = dirname(dirname(PRD_PATH))
  START_PHASE = $ARGUMENTS[1] or 1
  END_PHASE = $ARGUMENTS[2] or auto-detect from PRD
  PRD_NAME = basename without extension
Else:
  PROJECT_PATH = $ARGUMENTS[0] or current working directory
  START_PHASE = $ARGUMENTS[1] or 1
  END_PHASE = $ARGUMENTS[2] or 4
  PRD_PATH = auto-discover from PROJECT_PATH/PRDs/
  PRD_NAME = discovered PRD basename
```

### Mode Detection

After parsing arguments:
- If PRD_PATH was provided or auto-discovered → **MODE = "execute"**
- If no PRD found → **MODE = "discovery"**

### Auto-Detect Phases from PRD

When PRD_PATH is specified, scan the PRD for phase sections:
1. Look for: `## Phase N:`, `### Phase N:`, `**Phase N:**`, `Phase N:`
2. Set END_PHASE to highest phase found (if not specified)

---

## Resolve CLUTCH Plugin Directory

Before proceeding, determine the absolute path to the CLUTCH plugin:
```bash
# Find the CLUTCH plugin root (parent of .claude-plugin/)
find /home -maxdepth 4 -name "plugin.json" -path "*/clutch/.claude-plugin/*" 2>/dev/null | head -1 | sed 's|/.claude-plugin/plugin.json||'
```
Store the result as `CLUTCH_DIR`. All reference docs and templates are under this directory.

Verify: `CLUTCH_DIR/skills/clutch/assets/prp_base.md` should exist.

---

## Required Reading by Role

**CRITICAL: Each role MUST read their instruction files before acting.**

| Role | Instructions |
|------|-------------|
| Discovery (no PRD) | Read references/piv-discovery.md |
| PRD Creation | Read references/create-prd.md |
| PRP Generation | Read references/generate-prp.md |
| Team Orchestration | Read references/team-orchestration.md |
| Executor | Read references/execute-prp.md |

**DO NOT wing it. Follow the established processes.**

**Prerequisite:** A PRD must exist before entering the Phase Workflow. If no PRD exists, enter Discovery Mode.

---

## Discovery Mode (No PRD Found)

When MODE = "discovery":

1. Read references/piv-discovery.md for the discovery process
2. Present discovery questions to the user in a friendly, conversational tone
   - Target audience is vibe coders — keep it approachable
   - Skip questions the user already answered
3. Wait for user answers
4. Fill gaps with your expertise:
   - If user doesn't know tech stack → research and PROPOSE one
   - If user can't define phases → propose 3-4 phases based on scope
   - Always propose-and-confirm: "Here's what I'd suggest — does this sound right?"
5. Run project setup:
   - Create directories: PRDs/, PRPs/templates/, PRPs/planning/
   - Copy PRP template: `cp {CLUTCH_DIR}/skills/clutch/assets/prp_base.md {PROJECT_PATH}/PRPs/templates/prp_base.md`
6. Generate PRD: Read references/create-prd.md, write to PROJECT_PATH/PRDs/PRD-{project-name}.md
7. Set PRD_PATH to the generated PRD, auto-detect phases → continue to Phase Workflow

The orchestrator handles discovery and PRD generation directly (no sub-agent needed).

---

## Orchestrator Philosophy

> "Context budget: ~15% orchestrator, 100% fresh per teammate"

You are the **orchestrator**. You stay lean and manage the team. You DO NOT execute PRPs yourself — you spawn specialized teammates with fresh context.

**Before starting any phase**, read references/team-orchestration.md for team lifecycle details.

---

## Session State (v2)

Track these flags throughout the session. They persist across phases within a single session:

```
TEAM_FAILED_THIS_SESSION = false   # Set to true if any team fail-fast triggers
PHASES_COMPLETED = 0               # Count of successfully committed phases
ESTIMATED_CONTEXT_USED = 15        # Start at ~15% for orchestrator overhead
```

These flags drive the **Decision Model** (Step 2) and **Budget Gate** (Step 1b).

---

## Phase Workflow (v2)

For each phase from START_PHASE to END_PHASE:

### Step 1: Check/Generate PRP

#### Step 1a: Check for existing PRP
```bash
ls -la PROJECT_PATH/PRPs/ 2>/dev/null | grep -i "phase.*N\|pN\|p-N"
```
If a PRP already exists for this phase, skip to Step 2.

#### Step 1b: Spawn Fresh Research Agent for PRP Generation

**CRITICAL: Do NOT generate the PRP yourself. Spawn a FRESH sub-agent.**

Before spawning, the orchestrator must:
1. Read the PRD at PRD_PATH
2. Find the Phase N section
3. Extract the phase scope (title, deliverables, validation criteria)
4. Pass this extracted scope to the fresh agent

Spawn a `general-purpose` sub-agent with this prompt:

```
RESEARCH & PRP GENERATION MISSION - Phase {N}
==============================================

You are generating a PRP for Phase {N}. You have fresh context — use it wisely.

Project root: {PROJECT_PATH}
PRD Path: {PRD_PATH}

## Phase {N} Scope (from PRD)
{paste phase title, deliverables, and validation criteria}

## Step 1: Codebase Analysis
Read the codebase analysis process doc at: {CLUTCH_DIR}/skills/clutch/references/codebase-analysis.md
Follow it fully. Run deep codebase analysis for: {phase feature description}
Save analysis to: {PROJECT_PATH}/PRPs/planning/{PRD_NAME}-phase-{N}-analysis.md

## Step 2: Generate PRP (analysis context still loaded)
Read the PRP generation process doc at: {CLUTCH_DIR}/skills/clutch/references/generate-prp.md
Read the PRP template at: {CLUTCH_DIR}/skills/clutch/assets/prp_base.md
Follow the process doc fully. Use the template structure.
You already have the codebase analysis in your context — use it directly.
DO NOT spawn a sub-agent for this. You do it yourself.
Output PRP to: {PROJECT_PATH}/PRPs/PRP-{PRD_NAME}-phase-{N}.md

IMPORTANT: The PRP MUST include a ## Workstreams section that defines how
work can be split across parallel executor teammates. Each workstream should
own exclusive files. Aim for 2-4 workstreams. See the template for format.

## Critical Rules
- Do BOTH steps yourself in sequence
- Your analysis context feeds directly into PRP quality
- Follow the full generate-prp process (template, quality gates, info density)
- The PRP template is at: {CLUTCH_DIR}/skills/clutch/assets/prp_base.md
- DO NOT spawn sub-agents for either step
- The Workstreams section is REQUIRED — this PRP will be used for team execution
```

**Wait for the research agent to complete** before proceeding.

### Step 1b: Context Budget Gate (v2)

**Check if there's enough context to complete this phase.**

| Workstreams | Estimated Cost | Min Remaining to Start |
|-------------|---------------|----------------------|
| 1 (small) | ~15% | 30% remaining |
| 2 (medium) | ~25% | 50% remaining |
| 3+ (large) | ~40% | 80% remaining |

`remaining = 100 - ESTIMATED_CONTEXT_USED`. If `remaining < 2× estimated_cost`:
- Write checkpoint to WORKFLOW.md with resume instructions
- Inform user: "Context budget insufficient for Phase N. Start fresh: `/clutch {PRD_PATH} {N}`"
- **STOP execution.** Always err on the side of checkpointing early.

### Step 2: Choose Execution Strategy (v2 Decision Model)

Read the generated PRP and find the `## Workstreams` section. Count workstreams and evaluate conditions.

**Decision Matrix:**

| # | Condition | Strategy |
|---|-----------|----------|
| 1 | 1 workstream | Single agent (always) |
| 2 | 2+ workstreams, same repo, no data deps | Team (with fail-fast) |
| 3 | 2+ workstreams, cross-repo | Sequential single agents |
| 4 | 2+ workstreams, data dependencies | Sequential (contract-first) |
| 5 | `TEAM_FAILED_THIS_SESSION = true` | Sequential (always — no team retry) |
| 6 | Remaining context < 50% | Sequential (cheaper overhead) |

**Evaluate in order — first matching condition wins.**

- Condition 5 overrides 2: if a team already failed this session, always sequential
- Condition 6 overrides 2: if context is tight, sequential is cheaper
- 5+ workstreams: Merge smallest to get 4, then apply matrix

#### Step 2a: Solo Execution (1 workstream)

Use the Task tool with `subagent_type: "piv-executor"`:

```
EXECUTOR MISSION - Phase {N}
============================

PRP Path: {PRP_PATH}
Project: {PROJECT_PATH}

Read the PRP execution process doc at references/execute-prp.md, then execute the PRP.
Follow: Load PRP → Plan Thoroughly → Execute → Validate → Verify
Output EXECUTION SUMMARY with Status, Files, Tests, Issues.
```

Then skip to Step 4 (spawn validator as solo sub-agent).

#### Step 2b: Sequential Execution (v2 — conditions 3, 4, 5, or 6)

For each workstream in PRP (dependency order), spawn one `general-purpose` agent via Task:

```
SEQUENTIAL EXECUTOR - Phase {N}, Workstream: {name}
==================================================
PRP Path: {PRP_PATH}
Project: {PROJECT_PATH}

## Your Scope
{paste workstream section: files owned, tasks}

## Instructions
1. Read the PRP — absorb full context
2. Implement ONLY this workstream's files and tasks
3. Dependencies from prior workstreams are already on disk
4. Run validation commands for your scope
5. Output EXECUTION SUMMARY: Status, Files, Tests, Issues
```

Wait for completion before spawning next workstream. Collect summaries for validator.
Then skip to Step 4 (spawn validator as solo sub-agent).

#### Step 2c: Team Execution (2+ workstreams, parallel — condition 2)

1. **Create team**: `TeamCreate` with name `{project}-phase-{N}`

2. **Read team-orchestration.md** for full lifecycle details (including fail-fast guard).

3. **For each workstream**, spawn a teammate via Task:
   - `name`: `executor-{workstream-name}`
   - `subagent_type`: `general-purpose`
   - `team_name`: `{project}-phase-{N}`
   - Prompt:

```
EXECUTOR TEAMMATE MISSION - Phase {N}, Workstream: {workstream-name}
====================================================================

PRP Path: {PRP_PATH}
Project: {PROJECT_PATH}

## Your Workstream Scope
{paste the workstream section from the PRP: files owned, dependencies, tasks}

## Instructions
1. Read the PRP at the path above — absorb full context
2. Focus ONLY on your workstream's files and tasks
3. Do NOT touch files owned by other workstreams
4. If you need something from another workstream, message that teammate
5. Follow the execute-prp process: Load → Plan → Execute → Validate
6. When done, output an EXECUTION SUMMARY:
   - Status: COMPLETE / BLOCKED / PARTIAL
   - Files created/modified (list each)
   - Tests written and results
   - Issues encountered
   - Decisions made that affect other workstreams
```

4. **Create tasks** in the shared task list (one per workstream), assign to teammates.
5. **Set up blockedBy** if any workstreams have dependencies.

### Step 2d: Contract-First Protocol (TEAM MODE with dependencies)

If workstreams have `depends_on` relationships, use **staggered spawn** instead of fully parallel spawn:

1. **Map the contract chain** from the PRP's Workstreams section:
   - Workstreams with `depends_on: none` = **upstream** (spawn first)
   - Workstreams with `depends_on: [other]` = **downstream** (spawn after contracts received)

2. **Identify cross-cutting concerns** before spawning ANY executors:
   - URL/path conventions (trailing slashes, query params)
   - Response envelope format (flat vs nested)
   - Error shape (status codes, error body format)
   - Shared constants, config values, data storage semantics
   - Assign each concern to ONE upstream executor. Include in their spawn prompt:
     `"You own the cross-cutting concern: [X]. Define it in your contract."`

3. **Spawn upstream executors first.** Add to their prompt:
   ```
   ## Mandatory: Publish Interface Contract FIRST

   Before writing ANY implementation code, you MUST:
   1. Define your interface contract (exact function signatures, API URLs, response JSON shapes, error formats)
   2. Send it to the lead via SendMessage
   3. WAIT for lead confirmation before proceeding to implementation

   Your contract must include:
   - Exact function signatures or API endpoint URLs (with trailing slashes if applicable)
   - Exact request/response JSON shapes (field names, types, nesting)
   - All status codes for success and error cases
   - Error body format
   - Any streaming/event types or envelope wrappers
   ```

4. **Lead receives and verifies each contract:**
   - Are interfaces explicit? (exact URLs, exact JSON shapes — not "returns user data")
   - Are all status codes specified (200, 400, 404, 500)?
   - Is the error body format specified?
   - Any ambiguities that would cause downstream divergence?
   - If unclear → message executor for clarification before forwarding

5. **Forward verified contracts to downstream executors** in their spawn prompt:
   ```
   ## Contract You Must Conform To

   The following interface contract was published by executor-{upstream} and verified by the lead.
   Build to this contract EXACTLY. Do NOT deviate without asking the lead first.

   {paste contract verbatim}
   ```

6. **If ALL workstreams are independent (no `depends_on`):** skip this step entirely — spawn all in parallel as in Step 2b.

### Step 3: Monitor Execution (with Fail-Fast Guard)

**TEAM MODE:** Follow the **Fail-Fast Execution Guard** in references/team-orchestration.md:
- Check `git diff --stat HEAD` after 2 agent turns
- Files changed = 0 → kill team, set `TEAM_FAILED_THIS_SESSION = true`, fall back to Step 2b
- Files changed > 0 → team is productive, continue normal monitoring

**All modes:** Answer teammate questions promptly. If BLOCKED, provide guidance or reassign.

### Step 3b: Pre-Integration Contract Diff (TEAM MODE with dependencies)

Before spawning the validator, run a contract diff if workstreams had `depends_on` relationships:

1. For each upstream-downstream pair, ask both executors via SendMessage:
   - Upstream: "What exact interface did you implement? Paste your final contract."
   - Downstream: "What exact interface are you consuming? Paste the contract you built against."
2. Compare the two responses:
   - URL mismatches (trailing slashes, path params, query string format)
   - Response shape mismatches (flat vs nested, missing fields, extra fields)
   - Status code disagreements
   - Error format divergence
3. If mismatches found: send correction to the wrong side, let them fix, then proceed to validator.
4. If no mismatches: proceed directly to validator.

**Skip this step entirely if all workstreams were independent (no `depends_on`).**

### Step 4: Spawn Validator

**Team mode**: Spawn validator as a teammate in the same team:
- `name`: `validator`
- `subagent_type`: `general-purpose`
- `team_name`: `{project}-phase-{N}`

**Solo mode**: Use Task tool with `subagent_type: "piv-validator"`.

Prompt (both modes):

```
VALIDATOR MISSION - Phase {N}
=============================

PRP Path: {PRP_PATH}
Project: {PROJECT_PATH}
Executor Summaries:
{concatenate all executor summaries here}

Verify ALL requirements independently. Don't trust executor claims.
Check every file, every test, every requirement in the PRP.
Output VERIFICATION REPORT with Grade (PASS/GAPS_FOUND/HUMAN_NEEDED), Checks, Gaps.
```

**Process validator result:**
- `PASS` → Proceed to Step 6 (commit)
- `GAPS_FOUND` → Proceed to Step 5 (debug)
- `HUMAN_NEEDED` → Ask user for guidance

### Step 5: Debug Loop (Max 3 iterations)

**Team mode**: Assign gaps to responsible executors via SendMessage:
1. Read the validator's gap list
2. For each gap, identify which executor owns the affected files
3. Send gaps to the responsible executor: "Validator found these issues in your files: {gaps}. Please fix and confirm when done."
4. Wait for executors to confirm fixes
5. Re-run validator (message or re-spawn)

**Solo mode**: Spawn debugger sub-agent using `subagent_type: "piv-debugger"`:

```
DEBUGGER MISSION - Phase {N} - Iteration {I}
============================================

Project: {PROJECT_PATH}
PRP Path: {PRP_PATH}
Gaps: {GAPS}
Errors: {ERRORS}

Fix root causes, not symptoms. Run tests after each fix.
Output FIX REPORT with Status, Fixes Applied, Test Results.
```

After fixes:
- Re-run validator
- If PASS → proceed to commit
- If GAPS_FOUND again → debug again (up to 3 total)
- After 3 iterations → escalate to user

### Step 6: Shutdown Team (Team mode only)

1. Send `shutdown_request` to each teammate via SendMessage
2. Wait for shutdown confirmations
3. Call TeamDelete to clean up

### Step 7: Workstream Completion Enforcement (v2)

**Before committing, verify EVERY workstream has output.**

1. List all workstreams from the PRP's `## Workstreams` section
2. For each workstream, check `git diff --stat HEAD` for its expected files:
   ```bash
   cd {PROJECT_PATH} && git diff --stat HEAD -- {file1} {file2} ...
   ```
3. Classify each workstream:
   - **completed**: Has file changes matching expected output
   - **skipped**: No changes, but has documented reason (e.g., "not needed for MVP")
   - **deferred**: No changes, needs future work (e.g., "context budget exhausted")
4. **BLOCKING RULE**: If any workstream has no output AND no documented reason:
   - Option A: Execute it now (if context allows) — use sequential single-agent
   - Option B: Mark as `deferred` with explicit reason
   - **NEVER commit with an undocumented missing workstream**

### Step 8: Smart Commit (Orchestrator does this)

After workstream verification passes:
```bash
cd PROJECT_PATH
git status
git diff --stat
```

Create semantic commit:
- Format: `feat/fix/refactor(scope): description`
- Add: `Built with CLUTCH v2 - https://github.com/your-github-user/clutch`

### Step 9: Checkpoint to WORKFLOW.md (v2)

**Write structured checkpoint** using the schema from `{CLUTCH_DIR}/skills/clutch/assets/workflow-template.md`.

Each checkpoint includes: status, commit SHA, execution mode, team failure flag, workstream status list (completed/skipped/deferred with reasons), test count, files changed, validation cycles, context budget estimate, next phase description, and resume instructions.

**Key requirement:** A cold-start session with ONLY the PRD + WORKFLOW.md must be able to continue Phase N+1.

Also update the Execution Summary table at the top of WORKFLOW.md.

Update session state: `PHASES_COMPLETED += 1`, `ESTIMATED_CONTEXT_USED += phase_cost`

### Step 10: Next Phase

Increment phase counter. If more phases remain, loop back to Step 1 (including Budget Gate at Step 1b).

---

## Error Handling

| Error | Action |
|-------|--------|
| No PRD found | Enter Discovery Mode |
| Team fail-fast (v2) | Kill team → sequential fallback → `TEAM_FAILED_THIS_SESSION = true` |
| Context budget exceeded (v2) | Checkpoint to WORKFLOW.md → inform user → STOP |
| Workstream missing output (v2) | Execute it or mark deferred — never silent drop |
| Executor BLOCKED | Message for details → reassign or escalate |
| Validator HUMAN_NEEDED | Ask user for guidance |
| 3 debug cycles exhausted | Escalate to user with all context |
| Teammate timeout/failure | Check partial work → reassign → if multiple fail, go sequential |

---

## Completion

When all phases are complete, output:
```
## CLUTCH v2 COMPLETE

Phases Completed: START to END
Execution Modes Used: Team / Sequential / Solo (per phase)
Team Failures: N (sessions where fail-fast triggered)
Total Commits: N
Validation Cycles: M
Context Used: ~X%

### Phase Summary:
- Phase 1: [feature] - [solo/team/sequential] - validated in N cycles
- Phase 2: [feature] - [solo/team/sequential] - validated in N cycles
...

### Workstream Coverage:
- Total workstreams: N
- Completed: N
- Deferred: N (reasons documented in WORKFLOW.md)
- Skipped: N (reasons documented in WORKFLOW.md)

All phases successfully implemented and validated.
WORKFLOW.md checkpoint is current — any future session can continue from here.
```

---

## Visual Workflow (v2)

```
┌──────────────────────────────────────────────────────────────────┐
│                  CLUTCH v2 ORCHESTRATOR                           │
│   Claude Layered Unified Team Coordination Hub                   │
│   Resilient Multi-Phase Orchestration                            │
├──────────────────────────────────────────────────────────────────┤
│ SESSION STATE: team_failed=false, context_used=15%, phases=0     │
│                                                                  │
│ IF NO PRD FOUND:                                                 │
│   a. Ask discovery questions (piv-discovery.md)                  │
│   b. Generate PRD from answers (create-prd.md)                   │
│   c. Set PRD_PATH, auto-detect phases                            │
│                                                                  │
│ FOR EACH PHASE (START_PHASE to END_PHASE):                       │
│   1a. Check/Generate PRP (fresh sub-agent)                       │
│   1b. BUDGET GATE: Can we afford this phase?                     │
│       NO  → checkpoint + STOP                                    │
│       YES → continue                                             │
│   2.  DECISION MODEL: Choose strategy                            │
│       1 workstream        → Solo (2a)                            │
│       2+ no deps, no fail → Team with fail-fast (2c)             │
│       2+ with deps/fail   → Sequential (2b)                      │
│       Low context         → Sequential (2b)                      │
│   3.  EXECUTE with FAIL-FAST GUARD (team mode):                  │
│       After 2 turns: git diff → files? continue : kill → seq     │
│   4.  Spawn VALIDATOR → PASS / GAPS_FOUND / HUMAN_NEEDED         │
│   5.  If GAPS_FOUND → debug loop (max 3x)                        │
│   6.  Shutdown team (if team mode)                                │
│   7.  WORKSTREAM ENFORCEMENT: verify ALL workstreams have output  │
│       Missing? → execute or mark deferred (NEVER silent drop)    │
│   8.  Commit on PASS                                             │
│   9.  CHECKPOINT: Write structured state to WORKFLOW.md           │
│       (enables cold-start resumption from any session)            │
│   10. Next phase (loop with budget check)                         │
└──────────────────────────────────────────────────────────────────┘
```
