---
name: vertical-slice-audit
description: "Audit a feature or module against the Vertical Slice Architecture (VSA) checklist. Scores 10 rows (own folder, local models/schemas/routes/service, local errors, colocated tests, explicit public API, low coupling), classifies the slice (AI-friendly / partially migrated / horizontally organized), and outputs a migration playbook if it scores below 9/10. Handles Python flat-sys.path codebases (rows 8-9 get a documented caveat instead of a blind FAIL)."
arguments:
  - feature_path
---

# Vertical Slice Audit — Score a Feature, Produce a Migration Plan

You are auditing a single feature/folder for adherence to Vertical Slice Architecture (VSA). VSA is the codebase-level optimization that lets a coding agent load one slice end-to-end and ship — instead of chasing cross-folder context across layered architecture.

**Target path**: `$1` (the feature folder to audit). If `$1` is empty, ask the user which feature to audit before proceeding.

## What you will do

1. Read the target folder at `$1`
2. Walk the 10-row checklist below, scoring each row
3. Classify the slice based on the total score
4. If the slice fails (score < 9/10), produce a step-by-step migration plan
5. Report results to the user

---

## The 10-Row Checklist

For the feature at `$1`, score each row PASS or FAIL:

| # | Check | PASS | FAIL |
|---|---|---|---|
| 1 | Feature has its own folder | All its code lives under one top-level folder | Code is spread across `controllers/`, `services/`, `models/`, `repositories/` |
| 2 | Models are local to the feature | `<feature>/models.{py,ts}` exists | Models in a top-level shared folder |
| 3 | Schemas are local to the feature | `<feature>/schemas.{py,ts}` exists | Schemas in a top-level shared folder |
| 4 | Routes are local to the feature | `<feature>/routes.{py,ts}` (or routes registered from here) | Routes in a top-level shared folder |
| 5 | Business logic is local | `<feature>/service.{py,ts}` contains the substance | Logic spread across helpers, utils, lib |
| 6 | Errors are local | `<feature>/errors.{py,ts}` defines feature-specific exceptions | All errors are generic shared exceptions |
| 7 | Tests are colocated | `<feature>/tests/` exists next to the source | Tests in a top-level `tests/` folder |
| 8 | Public API is explicit | Feature has an `index.{ts}` or `__init__.py` that controls exports | Anything can be imported from anywhere |
| 9 | Internal helpers stay internal | Private functions are not exposed via the public API | Everything is exported |
| 10 | Cross-feature dependencies are minimal | Feature imports from <3 other features (excluding shared utils) | Feature imports from many other features |

For each row: cite the actual file path that justifies the PASS or FAIL.

### Python Flat-Import Caveat (Rows 8-9)

Some Python codebases deliberately use **flat `sys.path` imports** — the launchers put a directory on `sys.path` instead of packaging it with `__init__.py` wiring. In this pattern, rows 8-9 (explicit public API, internal helpers stay internal) will FAIL by design. This is a deliberate architecture choice, not a defect.

When you detect flat-import conventions (look for: launcher scripts that manipulate `sys.path`, absence of `__init__.py` files, or documentation explicitly noting flat imports), score rows 8-9 as **CAVEAT** instead of FAIL, and note:
- The slice uses flat `sys.path` imports by design
- Static import resolvers will report false orphans
- "Dead code" verdicts require grep confirmation, not just resolver output
- Do not count these rows against the total score

---

## Scoring Tiers

- **9-10 PASS** — AI-friendly. This is the reference shape. Other features should migrate toward it.
- **6-8 PASS** — Partially migrated. Usually missing colocated tests, explicit public API, or local errors. Quick wins available.
- **≤5 PASS** — Horizontally organized. Migration is meaningful but incremental.

---

## Why Each Check Matters

| Check | Why agents care |
|---|---|
| 1. Own folder | One `cd` or one `Read` round-trip loads everything. Agent context stays clean. |
| 2-5. Local code | A `/prime-<feature>` command can load the whole slice in one prompt. |
| 6. Local errors | When the agent sees an error, it's near the code that raises it. Faster reasoning. |
| 7. Colocated tests | Agent modifies the feature, sees tests immediately — no separate exploration step. |
| 8-9. Explicit API | Agent knows what's importable and what's internal. Prevents leaky abstractions. |
| 10. Low coupling | Changes don't ripple. Agent can plan + implement + validate inside the slice. |

---

## Migration Plan (only if score < 9/10)

If the slice fails, produce a migration plan:

```markdown
# VSA Migration Plan — <feature-path>

## Current state
- Score: X/10
- Tier: <AI-friendly / Partially migrated / Horizontally organized>
- Failed rows: <list row numbers + one-line summary>

## Step-by-step migration

### Step 1 — Create the slice folder
<bash commands to mkdir + touch missing files>

### Step 2 — Move code into the slice (least risky first)
1. Models — usually move cleanly; update imports.
2. Schemas / validation — same.
3. Errors — define feature-local exceptions.
4. Service — move business logic in. May require splitting.
5. Routes — move handlers; register from the slice.
6. Tests — move last. They validate the migration.

After each step: run validation and commit.

### Step 3 — Define the public API
Edit `<feature>/index.{ts}` or `__init__.py` to export only what the rest needs.

### Step 4 — Update imports across the codebase
Find every import referencing old paths. Update to the new public API.
This is the "blast radius" step — mechanical but thorough.

### Step 5 — Update the AI Layer
Edit CLAUDE.md to reference the new slice. Write a per-feature CLAUDE.md if conventions are non-obvious.

## Estimated effort
- Lines moved: ~<count>
- Files touched: ~<count>
- Risk: <Low / Medium / High>

## What NOT to migrate
- Auth middleware, logging, request correlation — stay shared
- Database connections / session management — stay shared
- HTTP framework setup — stays shared
- Domain-free utilities (date helpers, string utils) — stay in shared/
```

---

## Output Format

```markdown
# VSA Audit Result — <feature-path>

## Score: X/10 — <tier name>

## Checklist results

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Own folder | PASS / FAIL | `<file path or 'no such folder'>` |
| 2 | Models local | PASS / FAIL | ... |
| ... | ... | ... | ... |

## Verdict
<one-paragraph: what's working, what's not>

## Migration plan
<only if score < 9/10>

## Recommended next steps
<2-3 concrete actions>
```

---

## Critical Principles

1. **Score honestly.** PASS means the file/convention actually exists.
2. **Cite real paths.** Every row's evidence references a file or notes its absence.
3. **Don't editorialize.** Describe the codebase as it stands.
4. **Coupling beats LOC.** A 2,000-line `service.py` is fine if cohesive. A 200-line file pulling from 14 features fails row 10.
5. **Migration is incremental.** Never recommend "refactor everything." Start with the next greenfield slice.

## What This Skill Does NOT Do

- Refactor code on its own. It produces a plan; execution is a separate session.
- Audit the entire codebase at once. Audit one feature at a time.
- Score non-VSA architectures. If the codebase uses hexagonal/clean architecture by design, these checks may not be the right metric.
