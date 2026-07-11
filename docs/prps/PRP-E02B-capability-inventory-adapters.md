# PRP-E02B-capability-inventory-adapters: Legacy Capability Inventory Adapters

**Status:** draft — requires WF2 review and source-specific hardening
**Primary POL anchors:** POL-CP-001, POL-MG-001, POL-MG-003..POL-MG-005
**Depends on:** E02A

## Goal and context
Inventory commands, intents, integrations, skills, MCP, overlays, Cabinet tools and executors; adapt each invokable operation to exactly one descriptor without moving authority yet.

Normative source: [Polish Architecture Specification](../specs/taskchad-os-polish-architecture-spec.md). The assessment is context only; the POL clauses govern.

## Candidate scope (not an implementation allowlist)
- `.claude/chat/`
- `.claude/scripts/`
- `.claude/scripts/tests/`

New files may be introduced only beneath an allowed directory and must be named during workflow preflight. Everything else is forbidden.

## Current source anchors
- `.claude/chat/core_handlers.py`
- `.claude/chat/cognition/skills.py`
- `.claude/scripts/integrations/`
- `.claude/scripts/orchestration/`

## Desired structured artifacts and state
- **Machine-readable inventory with source, alias, descriptor/version, owner and migration state.**
- **Shadow comparison records missing, duplicate and divergent semantics.**
- **Exit criterion: every reachable operation mapped or proven non-invokable/unreachable.**

Stable reasons include: unmapped_operation, duplicate_authority, semantic_divergence, unsupported_operation. Unknown exceptions map to a typed `internal_error` with redacted detail; they do not become success.

## Delivery protocol

### Security and concurrency invariants
- Preserve spec §8: no ambient identity, narration authority, unaudited activation, blind rollback, fake success, secret propagation, or authorization TOCTOU.
- Carry tenant/persona/actor/correlation explicitly; redact credentials and private reasoning.
- Durable writes are atomic and idempotent; concurrent mutation uses transaction/CAS, stable idempotency keys, or ordered locks. Failure returns a typed reason and never a success-shaped partial result.

### Non-goals
No unrelated UI redesign, provider migration, new feature surface, global singleton, distributed-systems claim, or architecture-level claim. Compatibility paths remain adapters, never a second authority.

### Ordered RED → GREEN implementation
1. **RED—contract:** add schema/reason-code tests, including malformed, denied, unavailable, and legacy-input cases; prove failure.
2. **RED—safety:** add negative isolation, audit failure, stale/concurrent writer, retry/idempotency, and secret-redaction tests; prove failure.
3. **GREEN—core:** implement the smallest typed record/port/service needed to pass contract tests.
4. **GREEN—migration:** adapt the named legacy entry points; shadow/compare where authority moves and preserve stable IDs.
5. **GREEN—proof:** persist structured outcomes and evidence references; add operator-safe projection where in scope.
6. Run focused tests, then regressions and lint; record source revision, environment, commands, exits, and limitations.

### Verified repository bootstrap and commands
From repository root (the Python project and dev extras are defined by `.claude/scripts/pyproject.toml`):
```bash
cd .claude/scripts
uv run --extra dev pytest tests -q
uv run --extra dev ruff check . ../chat
cd ../..
git diff --check
```
These are candidate commands only. WF2 must name exact focused and regression argv, prove them in a clean environment, and record exits in its manifest before this draft can advance.

### Acceptance criteria
- Every listed artifact and reason is schema-tested; denial/failure cannot mutate or report success.
- Negative tenant/persona, concurrency, retry, audit-failure, and redaction cases pass.
- Named legacy paths invoke one domain authority or are mechanically read-only/disabled at cutover.
- Focused and full regressions pass, `git diff --check` is clean, and evidence names revision/environment/command/exit.
- No claim exceeds the evidence floor in spec §11.3.

### Documentation impact
Update the relevant feature manual only when operator behavior changes; link stable record/reason semantics and state limitations. Do not manually raise maturity labels.

### Backout
Disable new ingress, drain or fence in-flight mutation, reconcile pending records, back up affected stores, and restore the previous adapter routing. Retain new records/status readers and audit/proof evidence until all rows are terminal; never downgrade by deleting evidence or re-enabling dual writes.
