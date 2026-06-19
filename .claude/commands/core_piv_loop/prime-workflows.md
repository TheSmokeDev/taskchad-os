---
description: Load the Archon workflows slice — workflow YAML DAGs, command files, project config, PIV loop, Ralph autonomous agent
---

# Prime: Archon Workflows Slice

## Objective

Build understanding of the Archon workflow engine layer — how this project uses YAML DAG workflows, command files, worktree isolation, and the PIV loop (Plan-Implement-Validate) for coding tasks. Archon is the "hands" (deterministic multi-step coding) while The Homie is the "brain" (runtime, memory, reasoning).

## Key Files to Read

### Project config
@.archon/config.yaml

### Active workflows (read all YAML files)
@.archon/workflows/archon-vault-a-grade.yaml
@.archon/workflows/best-of-both-team-orchestration.yaml
@.archon/workflows/archon-evolve-belief.yaml

### PIV command suite (the core dev loop)
@.claude/commands/core_piv_loop/prime.md
@.claude/commands/core_piv_loop/plan-feature.md
@.claude/commands/core_piv_loop/execute.md

### Validation + review commands
@.claude/commands/validation/validate.md
@.claude/commands/validation/code-review.md
@.claude/commands/commit.md

### Bug fix commands
@.claude/commands/github_bug_fix/rca.md
@.claude/commands/github_bug_fix/implement-fix.md

### CLAUDE.md Archon section (search for "## Archon")
Read the Archon section of CLAUDE.md for workflow triggers, Ralph state pattern, Archon vs Convoy/Mailbox distinction.

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

## Command File Structure

Commands are markdown in `.archon/commands/` with YAML frontmatter. They're prompts callable from workflows or directly via `archon workflow run`.

## Slice Boundaries

- **Owns**: workflow YAML definitions, command files, project config, PIV loop commands, validation commands, commit conventions
- **Does NOT own**: Archon binary (compiled Go at `~/.archon/bin/archon.exe` — not editable), runtime memory pipelines, convoy/mailbox orchestration (that's the framework, not Archon)
- **Key distinction**: Archon = developer coding workflows (feature dev, PR review). Convoy/Mailbox = runtime agent task coordination. Different systems, different state stores.

## Key Patterns

1. **Ralph state pattern**: state persists on disk between iterations — `prd.json` (story list with pass/fail) + `progress.txt` (learnings). Each iteration: read state → implement ONE story → validate → commit → update state → exit
2. **Model routing**: Opus plans, Codex executes with fresh context (anti-self-bias)
3. **Worktree isolation**: every workflow run creates a git worktree branch (`archon/task-{description}`)
4. **PIV loop**: Prime (load context) → Plan (design approach) → Implement (code it) → Validate (verify it works)

## Output

After reading, provide:

### Workflows Overview
- List of active workflows with their provider and node count
- Which use interactive mode vs autonomous
- Cross-provider patterns (Opus plan → Codex execute)

### PIV Loop
- Command flow: prime → plan → implement → validate → commit
- How each command chains to the next

### Current State
- Active worktree branches (`archon isolation list`)
- Recent workflow runs
- Any custom commands specific to this project
