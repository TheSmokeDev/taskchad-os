# PRP-E01D-rollback-recovery: Amendment Rollback and Crash Recovery

**Status:** draft — requires WF2 review and source-specific hardening
**Primary POL anchors:** POL-AM-004..POL-AM-006, POL-PA-004, POL-PA-007; invariants 6, 7, 16
**Depends on:** E01A, E01C

## Goal and context
Deliver conflict-safe exact-byte rollback and deterministic pending-state recovery by implementing the current detailed rollback PRP; do not duplicate or weaken its protocol. PR #12 is the open implementation of this rollback domain only.

Normative source: [Polish Architecture Specification](../specs/taskchad-os-polish-architecture-spec.md). The assessment is context only; the POL clauses govern.

## Candidate scope (not an implementation allowlist)
- `.claude/chat/cognition/`
- `.claude/scripts/tests/`
- `docs/manual/features/amendment-aware-rollback.md`

New files may be introduced only beneath an allowed directory and must be named during workflow preflight. Everything else is forbidden.

## Current source anchors
- `docs/prps/PRP-001A-domain-rollback-service.md`
- `docs/prps/PRP-001B-local-cli.md`
- `docs/prps/PRP-001C-authenticated-python-api.md`
- `docs/prps/PRP-001D-dashboard-rollback-ui.md`
- `.claude/chat/cognition/amendments.py`

## Desired structured artifacts and state
- **Canonical implementation contract is PRP-001A; this catalog entry supplies dependency/traceability only.**
- **RollbackRecord binds expected current, restored and displaced hashes, actor/reason, authorization and audit.**
- **State: applied → rollback_pending → rolled_back; crash reconciliation is deterministic.**

Stable reasons include: use the exhaustive stable reason catalog in PRP-001A; no aliases. Unknown exceptions map to a typed `internal_error` with redacted detail; they do not become success.

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
