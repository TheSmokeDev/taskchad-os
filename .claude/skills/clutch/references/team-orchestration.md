# Team Orchestration for CLUTCH v2

## Merged Architecture (thehomie + Smoke)

Team orchestration in this framework merges two lineages:

- **thehomie** contributes the stronger *durable orchestration substrate*: convoy DAGs with dependency edges, DB-backed mailbox with claim/ack lifecycle, a local API on port 4322, and Mission Control as an operator surface.
- **the competitor** contributes the stronger *team-runtime ergonomics*: the coordinator contract (leader/worker discipline), team lifecycle, typed control messages, and operator controls over in-flight teams.

**The merged model:** keep the thehomie substrate as the source of truth, and import the competitor's behavioral discipline as a runtime-injected coordinator contract on top of it.

**Architecture assignments (see `orchestration/CONTRACT.md` §8 for the frozen table):**

| Concern | Owner |
|---------|-------|
| Work ownership and dependency graph | Convoy |
| Agent-to-agent communication | Mailbox |
| Leader/worker behavioral rules | Coordinator contract |
| Operator visibility and control | Mission Control |
| Shared team memory | Framework memory APIs |

**Explicitly rejected:** literal porting of the competitor's filesystem-first design — no `~/.thehomie/teams/...` task dirs, no file-backed teammate inboxes, no vendor-specific team-memory sync endpoints, and Mission Control does not become the source of truth for team lifecycle. The durable state lives in convoy + mailbox; Smoke's contribution is behavioral, not storage.

## Overview

This document covers team-specific orchestration patterns for parallel execution. The orchestrator creates a team of executor teammates that work simultaneously on different workstreams, coordinated through a shared task list and inter-agent messaging.

CLUTCH v2 adds **fail-fast execution guards**, **sequential fallback**, and a **no-retry rule** to prevent wasting context on non-productive teams.

## Team Lifecycle

```
TeamCreate → Spawn Teammates → Create & Assign Tasks → FAIL-FAST CHECK (2 turns) → Monitor → Shutdown → TeamDelete
                                                           ↓ (no output)
                                                      Kill Team → Sequential Fallback
```

### 1. Create Team

Create one team per phase execution:
- **Team name**: `{project-name}-phase-{N}` (e.g., `teambox-phase-2`)
- Use TeamCreate with the team name

### 2. Spawn Executor Teammates

For each workstream in the PRP, spawn one executor teammate:
- **Teammate name**: `executor-{workstream-name}` (e.g., `executor-frontend`, `executor-api`)
- **Subagent type**: `general-purpose` (needs file editing, bash, search tools)
- **Team name**: Pass the team name so the teammate joins the team
- **Prompt**: Include PRP path, project path, and the teammate's specific workstream scope

Each executor teammate receives:
- The full PRP path (they read it themselves for context)
- Their specific workstream assignment (which files they own, which tasks they handle)
- Instructions to follow the execute-prp.md process for their scope
- Instructions to message other teammates if they need coordination

### 3. Create and Assign Tasks

Use the shared task list for tracking:
- Create one task per workstream using TaskCreate
- Assign each task to the corresponding executor teammate using TaskUpdate
- Set up blockedBy relationships if workstreams have dependencies

### 4. Fail-Fast Execution Guard (v2)

**CRITICAL: Check team productivity within 2 turns of spawning.**

After spawning all executor teammates, the orchestrator must:

1. **Wait approximately 2 agent turns** (watch for 2 rounds of teammate idle notifications)
2. **Run git diff check:**
   ```bash
   cd {PROJECT_PATH} && git diff --stat HEAD
   ```
3. **Evaluate result:**
   - **Files changed > 0**: Team is productive. Proceed to normal monitoring (Step 4b).
   - **Files changed = 0**: Team is non-productive. Trigger fail-fast:
     a. Send `shutdown_request` to ALL teammates immediately
     b. Call `TeamDelete` to clean up
     c. Log the failure:
        ```
        TEAM FAIL-FAST: Team {team-name} produced no file output after 2 turns.
        Falling back to sequential single-agent execution.
        ```
     d. Set `TEAM_FAILED_THIS_SESSION = true` (session state)
     e. Proceed to **Sequential Fallback** (Step 4c)

**Why 2 turns?** If agents are doing productive work, they write files within 2 turns. Contract-only messages with no code output = non-productive. The 2-turn window is conservative enough to avoid false positives while catching broken teams early (vs. v1 which burned ~40% of context before detecting failure).

### 4b. Monitor Execution (team is productive)

The orchestrator monitors teammates:
- Teammates send messages when they complete work or encounter issues
- Messages are delivered automatically — no polling needed
- Answer teammate questions promptly to avoid blocking
- If a teammate gets stuck, provide guidance via SendMessage

### 4c. Sequential Fallback (after team failure)

When fail-fast triggers, execute workstreams one at a time:

1. For each workstream in the PRP (in dependency order):
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
   3. Run validation commands from the PRP
   4. Output EXECUTION SUMMARY: Status, Files, Tests, Issues
   ```
2. Spawn as `subagent_type: "general-purpose"` via Task tool (NOT as teammate)
3. Wait for completion before spawning the next workstream
4. Collect each executor's summary

**Sequential is slower but reliable.** The overhead of team coordination is eliminated, and each agent gets full context for its workstream.

### No Team Retry Rule (v2)

**If a team fails (fail-fast triggered) during a session, NEVER retry team-based execution for any remaining phases in that session.**

Always use sequential single-agent execution from that point forward. Rationale:
- Whatever caused the team failure (context pressure, agent confusion, complex workstreams) is likely to recur
- Sequential execution is more reliable and wastes less context
- The orchestrator tracks this via `TEAM_FAILED_THIS_SESSION` flag

### 5. Collect Execution Summaries

Each executor teammate should output an EXECUTION SUMMARY when done:
- Status (COMPLETE / BLOCKED / PARTIAL)
- Files created/modified
- Tests written and their results
- Issues encountered
- Decisions made

The orchestrator collects all summaries for the validator.

### 6. Shutdown and Cleanup

After validation passes (or escalation):
- Send shutdown_request to each teammate via SendMessage
- Wait for shutdown confirmations
- Call TeamDelete to clean up the team

## Teammate Coordination Patterns

### File Ownership

Each workstream exclusively owns its files. This is the primary coordination mechanism:
- **No two teammates should edit the same file**
- If a teammate needs something from another teammate's file, they message and wait
- The PRP's workstream section defines file ownership

## Contract-First Spawning (for dependent workstreams)

When workstreams have `depends_on` relationships, the orchestrator MUST enforce contract-first spawning. This prevents the classic failure mode where parallel agents build incompatible interfaces.

### The Contract Chain

The PRP's Workstreams section defines dependencies via the `depends_on` field. The orchestrator reads this to determine spawn order.

### Anti-Patterns

**Anti-pattern: Fully parallel spawn with dependencies**
All executors spawn simultaneously → each builds to assumptions → integration fails at the end. This is the #1 failure mode for multi-agent teams.

**Anti-pattern: "Tell them to talk to each other"**
Lead says "share your contract with the other executor" → they don't, or share too late, or share something vague. Agents are bad at self-organizing communication.

**Anti-pattern: Late contract sharing**
Upstream executor finishes implementation, THEN shares what they built → downstream already made incompatible assumptions. Contract must come BEFORE implementation.

### Good Pattern: Lead as Active Relay

```
Upstream publishes contract → Lead verifies → Lead forwards to downstream → Both build in parallel
```

The lead is the quality gate. No contract passes to downstream without lead verification.

### Spawn Order Protocol

1. Read PRP Workstreams — identify `depends_on` relationships
2. Workstreams with `depends_on: none` = upstream (spawn first)
3. Each upstream executor's FIRST deliverable: interface contract via SendMessage to lead
4. Lead verifies: exact shapes, URLs, error codes, no ambiguity
5. Lead forwards verified contract to downstream executor's spawn prompt
6. Downstream spawns with contract embedded — NO guessing

### Cross-Cutting Concerns

Before spawning, scan PRP for behaviors spanning multiple workstreams:

| Concern | Example | Risk if unassigned |
|---------|---------|-------------------|
| URL conventions | trailing slashes, path params | 404s at integration |
| Response envelopes | `{data: {...}}` vs flat | Frontend parse failures |
| Error shapes | `{error: "msg"}` vs `{code: N, message: "msg"}` | Inconsistent UX |
| Shared constants | config values, magic numbers | Divergent defaults |
| Storage semantics | per-event vs accumulated, key naming | Data corruption |

Assign each to ONE executor. Include in their spawn prompt.

### Contract Verification Checklist

When lead receives a contract from upstream:
- [ ] URLs are exact (including trailing slashes, path params)
- [ ] Response JSON shapes are explicit (not "returns user data")
- [ ] All status codes specified (200, 400, 404, 500)
- [ ] Error body format specified
- [ ] Any streaming/SSE event types documented
- [ ] Any envelope wrappers noted
- [ ] Cross-cutting concerns addressed

### When to Message Teammates

Teammates should message each other when:
- They've created an interface or type that another teammate depends on
- They've discovered something that affects another workstream
- They need a dependency from another workstream to be completed first
- They've made a design decision that impacts shared contracts

### When NOT to Message

Don't message for:
- Status updates (the task list handles this)
- General progress reports (waste of context)
- Questions the PRP already answers

### Dependency Handling

If Workstream B depends on Workstream A:
1. The orchestrator sets up blockedBy in the task list
2. Executor A messages Executor B when the dependency is ready
3. Executor B can start on non-dependent work while waiting

## Validator as Teammate

The validator joins the same team after all executors complete:
- **Teammate name**: `validator`
- **Subagent type**: `general-purpose`
- **Input**: PRP path, project path, all executor summaries concatenated
- The validator can message executor teammates to ask about specific decisions
- Executors should still be alive (not shut down) during validation

### Validator Flow
1. Spawn validator as teammate in the existing team
2. Validator reads PRP and checks ALL requirements independently
3. Validator can message executors: "Why did you implement X this way?"
4. Validator outputs VERIFICATION REPORT with grade

## Debug via Messaging

When the validator finds gaps (GAPS_FOUND):
1. The orchestrator reads the gap list from the validator's report
2. For each gap, identify which workstream/executor owns the affected files
3. Send the specific gaps to the responsible executor via SendMessage
4. The executor fixes the issues and messages back when done
5. Re-spawn or message the validator to re-check

This is more efficient than spawning new debugger agents because:
- Executors already have context about their workstream
- No context re-loading needed
- Executors can coordinate fixes if gaps span workstreams

### Debug Loop Rules
- Maximum 3 debug iterations (same as solo PIV)
- Each iteration: assign gaps → executors fix → validator re-checks
- After 3 failures: escalate to user with all context

## Team Sizing Guidelines

| Workstreams in PRP | Executor Teammates | Notes |
|---|---|---|
| 0-1 | Don't use teams | Fall back to solo /piv behavior |
| 2 | 2 | Minimum for teams to be worthwhile |
| 3-4 | 3-4 | Sweet spot for most projects |
| 5+ | Cap at 4 | Merge smallest workstreams; more = more overhead |

### When to Merge Workstreams

If the PRP defines 5+ workstreams:
- Combine the two smallest workstreams
- Combine workstreams with heavy dependencies on each other
- Target 4 total executors maximum

## Graceful Degradation

### Team Produces No Output (v2 — Fail-Fast)
If `git diff --stat HEAD` shows 0 files after 2 turns:
1. Kill team immediately (shutdown_request + TeamDelete)
2. Set `TEAM_FAILED_THIS_SESSION = true`
3. Fall back to sequential single-agent execution for this phase AND all remaining phases
4. Log failure in WORKFLOW.md

### Teammate Gets Stuck
If a teammate stops responding or reports BLOCKED:
1. Check what they accomplished (read their files, check task status)
2. Try sending a clarifying message
3. If still stuck after 1 retry: take their remaining tasks and assign to another teammate or handle as orchestrator

### Team Creation Fails
Fall back to sequential single-agent execution (not teams) for this phase AND all remaining phases. Set `TEAM_FAILED_THIS_SESSION = true`.

### Partial Completion
If some workstreams complete but others fail:
1. Do NOT commit incomplete phases — this is a v2 workstream enforcement rule
2. Either execute the missing workstreams (sequential fallback) or mark as deferred with reason
3. Only commit when ALL workstreams are verified or explicitly documented as deferred/skipped

## Team Naming Convention

```
Team:       {project}-phase-{N}
Executors:  executor-{workstream-name}
Validator:  validator
Tasks:      {workstream-name}: {brief description}
```

Examples:
```
Team:       teambox-phase-2
Executors:  executor-frontend, executor-api, executor-database
Validator:  validator
Tasks:      "frontend: Build React components for team dashboard"
            "api: Implement REST endpoints for team CRUD"
            "database: Create migration and seed data"
```
