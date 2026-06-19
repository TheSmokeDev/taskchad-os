---
description: Load the Archon workflows slice — workflow YAML DAGs, command files, project config, PIV loop, Ralph autonomous agent
---

# Prime: Archon Workflows Slice

## Objective

Build understanding of the Archon workflow engine layer — how this project uses YAML DAG workflows, command files, worktree isolation, and the PIV loop (Plan-Implement-Validate) for coding tasks. Archon is the "hands" (deterministic multi-step coding) while The Homie is the "brain" (runtime, memory, reasoning).

## Key Files to Read

Read these files in order. Together they are the complete workflows vertical slice.

### Project config
@.archon/config.yaml

### Workflow YAML DAGs (all project-specific workflows)
@.archon/workflows/archon-vault-a-grade.yaml
@.archon/workflows/archon-evolve-belief.yaml
@.archon/workflows/best-of-both-team-orchestration.yaml
@.archon/workflows/prp-1a-plan.yaml
@.archon/workflows/prp-7-plan.yaml
@.archon/workflows/self-model-live-reinjection.yaml
@.archon/workflows/thehomie-fix-issue-36.yaml
@.archon/workflows/video-production.yaml

Note: default workflows (archon-ralph-dag, archon-piv-loop, archon-smart-pr-review) are bundled with the Archon binary — not project files. Run `archon workflow list` to see both bundled and project-specific workflows.

### Archon command files (prompts callable from workflows or directly)
@.archon/commands/team-phase-0-doctrine.md
@.archon/commands/team-phase-1-coordinator.md
@.archon/commands/team-phase-2-team-state.md
@.archon/commands/team-phase-3-typed-mailbox.md
@.archon/commands/team-phase-4-cli-surface.md
@.archon/commands/team-phase-5-mc-team-view.md
@.archon/commands/team-phase-6-backend-fallback.md
@.archon/commands/team-phase-7-team-memory.md
@.archon/commands/prp-1a-adversarial-r2.md
@.archon/commands/prp-1a-revise-hermes-faithful.md
@.archon/commands/prp-7-r1-adversarial.md
@.archon/commands/prp-7-revise.md
@.archon/commands/prp-7-adversarial-r2.md
@.archon/commands/prp-7-adversarial-r3.md
@.archon/commands/video-intake.md
@.archon/commands/video-research.md
@.archon/commands/video-vision.md
@.archon/commands/video-compose.md
@.archon/commands/video-qa-stills.md
@.archon/commands/video-fix.md
@.archon/commands/video-report.md
@.archon/commands/fix-coding-vault.md
@.archon/commands/fix-thehomie.md
@.archon/commands/fix-unified-vault.md

### PIV command suite (the core dev loop invoked by workflows)
@.claude/commands/core_piv_loop/plan-feature.md
@.claude/commands/core_piv_loop/execute.md

### CLAUDE.md Archon section
Read the "Archon — Coding Workflow Engine" section of CLAUDE.md for workflow triggers, Ralph state pattern, and the Archon vs Convoy/Mailbox distinction.

## Workflow YAML Structure

Workflows are YAML DAGs in `.archon/workflows/`. Structure:
```yaml
name: workflow-name
description: When to use + what it does
provider: claude | codex
interactive: true | false
nodes:
  - id: node-name
    prompt: "..."          # AI prompt
    bash: "..."            # Or shell command
    depends_on: [other]    # DAG edges
    ai:
      model: sonnet | opus
      provider: codex      # Cross-provider for anti-bias
    denied_tools: [Write]  # Read-only safety
```

## Key Distinctions

**Archon != Convoy/Mailbox:**
| | Archon | Convoy/Mailbox |
|--|--------|----------------|
| Purpose | Coding workflows (feature dev, PR) | Runtime agent task coordination |
| Trigger | Developer invokes manually | Agent triggers programmatically |
| State | `prd.json` + `progress.txt` on disk | SQLite via `convoy_service.py` (port 4322) |

**Ralph state pattern:** state persists on disk between iterations — `prd.json` (story list with pass/fail) + `progress.txt` (learnings). Each iteration: read state -> implement ONE story -> validate -> commit -> update state -> exit.

## Slice Boundaries

- **Owns**: workflow YAML definitions, command files, project config, PIV loop commands, validation commands, commit conventions
- **Does NOT own**: Archon binary (compiled Go at `~/.archon/bin/archon.exe` — not editable), runtime memory pipelines (`.claude/scripts/`), chat engine (`.claude/chat/`), convoy/mailbox orchestration (`.claude/scripts/orchestration/`)
- **Cross-slice touchpoints**: workflows invoke commands; Ralph reads/writes prd.json and progress.txt; Archon creates git worktree branches

## Output

After reading, provide:

### Workflows Overview
- List of active workflows with their provider and node count
- Which use interactive mode vs autonomous
- Cross-provider patterns (Opus plan -> Codex execute)

### PIV Loop
- Command flow: prime -> plan -> implement -> validate -> commit
- How each command chains to the next

### Current State
- Active worktree branches
- Recent workflow runs
- Custom commands specific to this project
