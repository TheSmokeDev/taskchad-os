# PRP-WF4-workflow-rail-self-tests: Workflow Rail Self-Tests

**Status:** draft — requires WF2 review and source-specific hardening
**Depends on:** WF1-WF3

## Goal and context
Extract/test deterministic rail validators and exercise malicious/partial artifacts, changed-diff races, approval binding and publish refusal without network or live repositories. Current source is the merged `implement-prp` pilot; this slice may strengthen rails but may not change product behavior or auto-merge.

## Candidate scope (not an implementation allowlist)
- `.archon/workflows/`
- `.archon/commands/`
- `.claude/scripts/tests/`

## Current source anchors
- `.archon/workflows/implement-prp.yaml`
- `.archon/commands/prp-test-fix.md`
- `.archon/commands/prp-package.md`

## Desired structured artifacts, state, and reasons
- **Hermetic fixtures cover each gate and exact sentinel behavior.**
- **Publish adapter is faked; no commit/push/PR occurs in tests.**
- **Reasons: baseline_changed, diff_digest_changed, approval_mismatch, review_blocked, test_failed, publish_forbidden.**

## Security and concurrency invariants
Repository-relative POSIX paths only; argv arrays only; no secrets/local paths; artifacts are schema/version/run/revision bound. Approval binds the exact diff digest. A stale baseline, concurrent edit, malformed artifact, failed review/test, or absent approval fails closed. No shell interpolation of model content and no auto-merge.

## Non-goals
No product code, arbitrary command execution, direct-checkout mode, unattended publication, evidence inflation, or replacement of human merge review.

## Ordered RED → GREEN steps
1. RED fixtures for every malformed/hostile artifact and forbidden transition.
2. RED race tests mutate baseline/diff between validate, approval and publish.
3. GREEN introduce the smallest reusable schema/validator and typed reasons.
4. GREEN wire the workflow nodes, keeping fresh contexts and deterministic gates.
5. GREEN add happy-path fixture plus blocked review/test/approval/publish cases.
6. Run focused rail tests, full Python regression and diff check.

## Exact bootstrap and validation commands
```bash
cd .claude/scripts
uv run --extra dev pytest tests/test_archon_workflow_rails.py -q
uv run --extra dev ruff check ../../.archon tests/test_archon_workflow_rails.py
uv run --extra dev pytest tests -q
cd ../..
git diff --check
```
`tests/test_archon_workflow_rails.py` is the explicitly intended new test. If YAML-only files cannot be parsed by ruff, preflight must replace that focused ruff argv with the exact Python validator paths; it must not silently skip validation.

## Acceptance criteria
All hostile and race fixtures fail with stable reasons and zero publication side effects; happy path emits schema-valid artifacts; command/path allowlists and redaction are enforced; approval remains two-stage where publication is possible; full regression and diff check pass.

## Documentation impact
Update [Polish Architecture Execution Program](../manual/features/polish-architecture-execution-program.md) and [Archon Workflows](../manual/features/archon-workflows.md) only for shipped behavior.

## Backout
Disable the new workflow entry point, preserve run artifacts, restore the prior workflow YAML/commands, and verify the existing `implement-prp` pilot still refuses dirty/direct checkouts and changed approval digests. Never back out by weakening a gate.

## Draft readiness blocker
The paths and commands above are candidates, not authorization or proof. WF2 must enumerate exact files/symbols and prove focused/regression argv in a clean environment before this PRP can advance.
