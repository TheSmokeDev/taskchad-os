name: "Base PRP Template v2 - Context-Rich with Validation Loops"
description: |

## Purpose
Template optimized for AI agents to implement features with sufficient context and self-validation capabilities to achieve working code through iterative refinement.

## Core Principles
1. **Context is King**: Include ALL necessary documentation, examples, and caveats
2. **Validation Loops**: Provide executable tests/lints the AI can run and fix
3. **Information Dense**: Use keywords and patterns from the codebase
4. **Progressive Success**: Start simple, validate, then enhance
5. **Global rules**: Follow any project-level configuration files (CLAUDE.md, AGENTS.md, .cursorrules, etc.)

---

## Goal
[What needs to be built - be specific about the end state and desires]

## Why
- [Business value and user impact]
- [Integration with existing features]
- [Problems this solves and for whom]

## What
[User-visible behavior and technical requirements]

### Success Criteria
- [ ] [Specific measurable outcomes]

## All Needed Context

### Documentation & References (list all context needed to implement the feature)
```yaml
# MUST READ - Include these in your context window
- url: [Official API docs URL]
  why: [Specific sections/methods you'll need]

- file: [path/to/example-file]
  why: [Pattern to follow, gotchas to avoid]

- doc: [Library documentation URL]
  section: [Specific section about common pitfalls]
  critical: [Key insight that prevents common errors]

- docfile: [PRPs/ai_docs/file.md]
  why: [docs that the user has pasted in to the project]

```

### Environment Check
```yaml
# Verify these tools exist before planning validation
model: [model name and context window, e.g., "Kimi K2.5 (131K)" or "Claude Opus (200K)"]
project_type: [detected from config files]
test_command: [verified working, e.g., "pytest", "pnpm test", "forge test"]
lint_command: [verified working, e.g., "ruff check", "eslint", "forge fmt --check"]
type_check: [if applicable, e.g., "mypy", "tsc --noEmit"]
build_command: [if applicable, e.g., "pnpm build", "forge build"]
```

### Current Codebase tree (run `tree` in the root of the project) to get an overview of the codebase
```bash

```

### Desired Codebase tree with files to be added and responsibility of file
```bash

```

### Known Gotchas of our codebase & Library Quirks
```
# CRITICAL: [Library name] requires [specific setup]
# Example: This framework requires specific initialization before use
# Example: This ORM doesn't support batch inserts over 1000 records
# Example: We use version X of this library which has breaking changes from version Y
```

## Implementation Blueprint

### Data models and structure

Create the core data models, we ensure type safety and consistency.
```
Examples:
 - Database models / ORM models
 - Type definitions / schemas
 - Validation rules
 - API request/response types
```

### list of tasks to be completed to fullfill the PRP in the order they should be completed

```yaml
Task 1:
MODIFY src/existing_module:
  - FIND pattern: "class OldImplementation"
  - INJECT after line containing "def __init__"
  - PRESERVE existing method signatures

CREATE src/new_feature:
  - MIRROR pattern from: src/similar_feature
  - MODIFY class name and core logic
  - KEEP error handling pattern identical

...(...)

Task N:
...

```


### Per task pseudocode as needed added to each task
```
# Task 1
# Pseudocode with CRITICAL details - don't write entire code
function new_feature(param):
    # PATTERN: Always validate input first (see src/validators)
    validated = validate_input(param)  # raises ValidationError

    # GOTCHA: This library requires connection pooling
    with get_connection() as conn:  # see src/db/pool
        # PATTERN: Use existing retry decorator
        @retry(attempts=3, backoff=exponential)
        function _inner():
            # CRITICAL: API returns 429 if >10 req/sec
            rate_limiter.acquire()
            return external_api.call(validated)

        result = _inner()

    # PATTERN: Standardized response format
    return format_response(result)  # see src/utils/responses
```

### Integration Points
```yaml
DATABASE:
  - migration: "Add column 'feature_enabled' to users table"
  - index: "CREATE INDEX idx_feature_lookup ON users(feature_id)"

CONFIG:
  - add to: config/settings
  - pattern: "FEATURE_TIMEOUT = env('FEATURE_TIMEOUT', default='30')"

ROUTES:
  - add to: src/api/routes
  - pattern: "router.include_router(feature_router, prefix='/feature')"
```

## Workstreams (for parallel execution)

<!-- Only required when using /clutch. Omit for solo /piv. -->
<!-- Each workstream becomes one executor teammate. -->
<!-- Workstreams should be file-independent (no two workstreams edit the same file). -->
<!-- Aim for 2-4 workstreams. If only 1 is needed, the orchestrator falls back to solo mode. -->

### Workstream 1: {name}
- **Owner**: executor-{name}
- **Files to create**: [list of new files this workstream creates]
- **Files to modify**: [list of existing files this workstream modifies]
- **Depends on**: [other workstream names, or "none"]
- **Tasks**:
  1. [Task description from Implementation Blueprint]
  2. [Task description from Implementation Blueprint]

### Workstream 2: {name}
- **Owner**: executor-{name}
- **Files to create**: [list]
- **Files to modify**: [list]
- **Depends on**: [other workstream names, or "none"]
- **Tasks**:
  1. [Task description]
  2. [Task description]

## Contract Chain (if workstreams have dependencies)

<!-- Only needed when workstreams have depends_on relationships. -->
<!-- Maps what interfaces flow between workstreams so the orchestrator can enforce contract-first spawning. -->

Map the dependency chain between workstreams:

```
[upstream workstream] → publishes [contract type] → [downstream workstream]
```

Example:
```
database → function signatures → api
api → REST contract (URLs, JSON shapes, status codes) → frontend
```

For each contract boundary, define:
- **Exact interface** (URLs with trailing slashes, JSON response shapes, field names and types)
- **Error responses** (status codes, error body format)
- **Edge cases** (empty states, pagination format, streaming format)

## Cross-Cutting Concerns

<!-- Behaviors that span multiple workstreams. Assign each to ONE workstream owner. -->
<!-- If no cross-cutting concerns exist, delete this section. -->

| Concern | Owner (workstream) | Coordinates with | Detail |
|---------|-------------------|-----------------|--------|
| [URL conventions] | [api] | [frontend] | [Trailing slashes on collection endpoints, query param format] |
| [Error shapes] | [api] | [frontend] | [{ error: string, code: number }] |
| [Response envelope] | [api] | [frontend] | [{ data: T, meta?: {...} }] |

## Validation Loop

### Level 1: Syntax & Style
```bash
# Pre-flight: Verify commands exist before running
# command -v <tool> or which <tool> — report missing tools, don't fail silently

# Run these FIRST - fix any errors before proceeding
# Run your project's linter: e.g., ruff check, eslint, golangci-lint
# Run your project's type checker: e.g., mypy, tsc --noEmit, go vet

# Expected: No errors. If errors, READ the error and fix.
```

### Level 2: Unit Tests - each new feature/file/function use existing test patterns
```
# CREATE test files with these test case patterns:
# 1. Happy path - basic functionality works
# 2. Validation error - invalid input is rejected
# 3. Error handling - external failures handled gracefully
```

```bash
# Run your project's test suite: e.g., pytest, vitest, forge test, go test
# If failing: Read error, understand root cause, fix code, re-run (never mock to pass)
```

### Level 3: Integration Test
```bash
# Start the service (if applicable)
# Test the endpoint or integration point
# Verify expected behavior end-to-end
# If error: Check logs for stack trace
```

## Final validation Checklist
- [ ] All tests pass: [run your project's test command]
- [ ] No linting errors: [run your project's lint command]
- [ ] No type errors: [run your project's type check command]
- [ ] Manual test successful: [specific test command]
- [ ] Error cases handled gracefully
- [ ] Logs are informative but not verbose
- [ ] Documentation updated if needed

---

## Anti-Patterns to Avoid
- Don't create new patterns when existing ones work
- Don't skip validation because "it should work"
- Don't ignore failing tests - fix them
- Don't hardcode values that should be config
- Don't catch all exceptions - be specific
