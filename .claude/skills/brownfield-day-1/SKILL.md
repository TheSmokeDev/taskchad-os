---
name: brownfield-day-1
description: "Derive a complete AI Layer (CLAUDE.md + customized .claude/commands/) for a codebase that has no AI Layer yet. Dispatches N parallel Explorer sub-agents (3 for small repos, 5-10 for monorepos), converges into codebase-analysis.md, then dispatches 3 Builder sub-agents to produce the full AI Layer. One-shot — the agent orchestrates everything end-to-end. Also usable as a YourProduct client-onboarding capability: drop an AI layer onto a client's brownfield repo in a single run."
arguments:
  - template_path
---

# Brownfield Day-1 — Derive AI Layer From Scratch

You are executing the Brownfield Day-1 flow on a codebase. Goal: take a codebase with no AI Layer (no `CLAUDE.md`, no `.claude/`) and derive a working AI Layer — global rules + a customized command suite — in a single agent invocation.

**Template path**: `$1` (a folder containing reference `CLAUDE.md` + `.claude/commands/` to copy and customize from). If `$1` is empty, use the default YourProduct template at `.claude/skills/brownfield-day-1/templates/YourProduct/`.

## What you will do

1. **Initial Scan** — quick read of the codebase shape
2. **Plan the subagent slicing** — decide how many Explorer subagents and what each investigates
3. **Dispatch Explorer subagents in parallel** — N parallel sub-agents, one per slice
4. **Converge** their reports into `tmp/brownfield-day-1/codebase-analysis.md`
5. **Dispatch 3 AI Layer Builders in parallel** — derive `CLAUDE.md` + the command suite
6. **Report** the final state

You orchestrate. The user does not dispatch subagents — you do.

---

## Precondition Check

Before starting, check if `CLAUDE.md` already exists in the target repo:
- If `CLAUDE.md` exists: **STOP**. Warn the user that this codebase already has an AI layer. Suggest updating the existing CLAUDE.md and commands directly, or running `/vertical-slice-audit` to score a specific feature.
- If `CLAUDE.md` does not exist: proceed with the full Day-1 flow below.

---

## Step 0 — Initial Scan (≤3 minutes)

1. **Read `README.md`** if it exists. Capture: what is this product, tech stack at a glance.
2. **Walk the top-level folder structure** — `ls -la` and `tree -L 2` (exclude `node_modules`, `dist`, `build`, `.git`, `__pycache__`, `.venv`).
3. **Read root config files** — whichever apply: `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, `Gemfile`, `composer.json`, `tsconfig.json`, etc.
4. **Glance at 1-2 high-traffic files** — the server entry point, main router, or primary CLI entrypoint.

Output (internal — don't write to disk): a 5-bullet sketch of what the codebase is, its layout, and where the interesting parts live.

---

## Step 1 — Plan the Subagent Slicing

Decide how many Explorer subagents you need. **There is no fixed number.** Choose what fits this codebase.

### Slicing dimensions (use any combination that applies)

| Dimension | When to include it |
|---|---|
| Backend / data layer | Server, API, or non-trivial data layer |
| Frontend / UI | Web, mobile, or desktop UI |
| Infrastructure / deploy | Dockerfile, CI/CD, deploy scripts, IaC |
| Tests | Substantial test suite |
| Domain / business logic | Almost always — what does this codebase DO |
| CLI / scripts | Meaningful CLI surface or scripts/ folder |
| Database / migrations | Non-trivial schema |
| Plugins / extensibility | Adapters, providers, hooks |
| Documentation | Substantial `docs/` folder |
| Per-package (monorepo) | 5+ packages → one subagent per cluster |

### Heuristics

- **Small codebase (1-5K LOC)** — 3 subagents
- **Medium codebase (5-50K LOC)** — 4-6 subagents
- **Large monorepo (50K+ LOC)** — 6-10 subagents, sliced by package cluster or subsystem

---

## Step 2 — Dispatch Explorer Subagents in Parallel

Dispatch all N sub-agents in a **single message** so they run in parallel. Each writes a report to `tmp/brownfield-day-1/0X-<slice-name>.md` (~500-700 words).

### Subagent prompt template

```
You are Explorer Subagent #X in a Brownfield Day-1 onboarding flow. The codebase
is at <REPO_PATH>. There is no AI Layer (no CLAUDE.md, no .claude/). Discover
everything from source.

Your remit: [slice description]

Cover specifically:
- [3-7 bullet points specific to this slice]
- Cite real file paths everywhere.
- Read enough source to be specific. Do NOT make recommendations — only describe what IS.

Output: write your findings to <REPO_PATH>/tmp/brownfield-day-1/0X-<slice-name>.md
(~500-700 words max).
```

### Per-slice coverage

**Backend**: Primary language+version; framework+port; ORM/data-access; migration tool; auth pattern; API style; error handling; where routes live; top 5 substantial files; key interfaces.

**Frontend**: Framework+build tool; state management; styling; component organization; routing; type generation; real-time pattern.

**Infrastructure**: Package manager+lockfile; containerization; CI/CD workflows (list each); build pipeline; lint/format/type-check scripts; deployment; env/secrets management.

**Tests**: Test runner; naming+colocation convention; coverage tool; mocking convention; E2E vs unit split; top 3 test files; coverage gaps.

**Domain**: Elevator pitch; 3-5 core concepts (from schema/types/services); primary user flows; external integrations; non-obvious business rules; glossary.

---

## Step 3 — Converge into `codebase-analysis.md`

Read all N subagent reports and produce `<REPO_PATH>/tmp/brownfield-day-1/codebase-analysis.md`:

```markdown
# Codebase Analysis — <project-name>

## What this codebase does
<1-paragraph elevator pitch>

## Domain concepts
<3-8 bullet points>

## Stack at a glance
| Layer | Tool | Notes |
|-------|------|-------|

## Folder structure (annotated)
<tree of top 2 levels with 1-line annotations>

## Conventions
<bullet points: naming, imports, error handling, logging, tests>

## Most-substantial files
<top 5-10>

## External integrations
<bullet points>

## Non-obvious rules / gotchas
<bullet points>

## Seams — where new agentic work plugs in cleanly
<bullet points>

## Database schema (if applicable)
<table count + named list>

## Key files (cheat sheet)
<5-15 bullets pointing at entry-point files>
```

**Critical rule**: capture what IS, not what should be. Descriptive, no refactor proposals, cite real paths, no placeholders.

---

## Step 4 — Dispatch 3 AI Layer Builders in Parallel

All 3 builders read `codebase-analysis.md` + the template at `$1`.

### Builder A — Generate `CLAUDE.md`

Write `<REPO_PATH>/CLAUDE.md` with up to 10 sections:
1. Core Principles
2. Tech Stack
3. Architecture
4. Code Style
5. Logging
6. Testing
7. API Contracts (if applicable)
8. Development Commands
9. Common Patterns (2-3 real code examples from the analysis)
10. AI Coding Assistant Instructions (10 numbered bullets)

Rules: use ACTUAL paths from the analysis. 200-400 lines. Capture every gotcha. The template is structural reference; content comes from the analysis.

### Builder B — Copy + customize the prime command family

1. Copy generic `prime.md` from template, customize with actual codebase paths.
2. Generate 2-4 codebase-specific `prime-<slice>.md` variants — one per major slice found in the analysis. Each explicitly lists the 5-10 key files for that slice.

Each prime command: YAML frontmatter with description, Objective, Process (exact files to read), Output (scannable summary template). 50-130 lines.

### Builder C — Copy + customize PIV + workflow commands

1. Copy `plan.md`, `implement.md`, `commit.md` from template. Edit in place with this codebase's actual validation/test/lint commands.
2. Generate `validate.md` from scratch — wraps the codebase's full validation chain. Walk each step with per-step fix guidance. Tabular pass/fail report.

---

## Step 5 — Final Report

```
Brownfield Day-1 Complete — <project-name>

Subagents dispatched: N (list them)

Generated AI Layer:
- CLAUDE.md (<line count> lines)
- .claude/commands/
  - prime.md (<lines>)
  - prime-<slice>.md x <count>
  - plan.md (<lines>)
  - implement.md (<lines>)
  - validate.md (<lines>)
  - commit.md (<lines>)

Total: 1 CLAUDE.md + <N> commands

Recommended next steps:
1. Review CLAUDE.md — edit anything the agent got wrong.
2. Try /prime in a fresh session to verify the orientation lands.
3. Pick a small ticket and run /plan then /implement to test the layer.
4. Update the AI layer manually when major features ship.
```

---

## Critical Principles

1. **Dynamic, not fixed.** Subagent count adapts to the codebase. 3 for small, 10 for monorepo.
2. **Capture what IS.** Descriptive, not prescriptive. No refactor proposals.
3. **Cite real paths.** Every claim references a real file. No placeholders.
4. **Copy + customize beats regenerate** for template commands. Generate fresh for CLAUDE.md, validate.md, and codebase-specific prime variants.
5. **One invocation does it all.** The user sends one prompt. You orchestrate the rest.

## Python Flat-Import Caveat

If the codebase uses flat `sys.path` imports (e.g., launchers put a directory on `sys.path` instead of packaging), note this in CLAUDE.md's Architecture section and in the generated prime commands. Static import resolvers will report false orphans — "dead code" verdicts inside such directories require grep confirmation, not just resolver output. This is a deliberate design choice, not a defect.

## YourProduct Client-Onboarding Mode

This skill doubles as a YourProduct "Day-1 AI Enablement" deliverable. When used on a client repo:

- The `template_path` points at YourProduct's canonical AI-Layer template (or any reference template the operator chooses)
- The generated AI Layer is the deliverable — review, hand off to the client, or commit directly
- Follow-up maintenance: update the AI layer files directly when major features ship
- The assessment upsell is `/vertical-slice-audit` (score a feature, hand them a migration plan)

## What This Skill Does NOT Cover

- **Refactoring decisions.** Day 1 is descriptive. Refactor decisions belong to a separate planning session.
- **Skill creation.** New skills come later, once you've seen the same workflow repeat 3+ times.
- **Team rollout.** Day 1 is for one engineer onboarding the agent.
- **Type B Brownfield.** This is Type A (no AI Layer yet). Once the layer exists, update the AI layer files directly when major features ship, or run `/vertical-slice-audit` to score a specific slice.
