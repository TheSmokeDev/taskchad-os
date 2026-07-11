# PRP-E01A2: Amendment Audit-Fail-Closed Integration

**Status:** draft — requires WF2 review and source-specific hardening
**Primary POL anchors:** POL-AM-002, POL-AM-003, POL-AM-007, POL-PA-004
**Depends on:** E01A durable mutation audit contract

## Constitutional-target deny boundary
`POL-AM-007` is a hard prohibition: autonomous self-amendment MUST NOT publish core constitutional identity targets. Audit preparation, identity context, confinement, approval, or CAS checks do not waive the prohibition. This draft must be redesigned around an explicit trusted non-autonomous governance operation and negative tests proving every autonomous amendment caller denies constitutional targets before any audit preparation, snapshot, or target write.

## Exact allowed files and symbols
- Existing `.claude/chat/cognition/amendments.py`: `AmendmentApplyResult`, `apply_policy_approved_amendments`, `apply_amendment_if_allowed` only.
- Existing `.claude/scripts/tests/test_cognition_amendments.py`: focused integration tests only.
No new files and no broad directory allowance.

Current behavior: `apply_amendment_if_allowed` computes hashes, writes rollback then `target.write_text`, and only afterward updates `ProposalLedger`; `AmendmentApplyResult` has no audit ID. It therefore cannot fail closed on an independent audit prepare failure.

## Contract change
Add keyword-only `audit_store: MutationAuditStore` and explicit `actor_id`, `tenant_id`, `persona_id`, `correlation_id` to both apply entry points. Add `audit_id: str = ""` to `AmendmentApplyResult`. Prepare using idempotency key `amendment:{proposal.id}:{before_hash}:{after_hash}` before target write. State outcomes: prepare denied→`policy_decision="reject"`, `policy_reason="audit_prepare_failed"`, unchanged target; publication success→audit `published`, then ledger completion→`verified`; target-write failure→audit `failed`; target changed after prepare→`audit_conflict`; publication succeeded but audit transition/ledger finalize failed→`reconciliation_required`, never success-shaped.

## Named RED tests (initial expected failure: unexpected keyword/mutated target)
- `test_apply_denies_constitutional_target_when_audit_prepare_fails`
- `test_apply_denies_missing_identity_context`
- `test_apply_denies_non_constitutional_and_symlink_escape_target`
- `test_apply_rechecks_before_hash_after_audit_prepare`
- `test_apply_concurrent_same_proposal_publishes_once`
- `test_apply_crash_after_publish_returns_reconciliation_required`
- `test_apply_result_carries_audit_id_without_secret_detail`

GREEN: extend result/signatures; compute intended bytes/hashes before prepare; prepare under existing ledger/target lock order; CAS recheck; atomic target publication; transition audit and ledger; add restart reconciliation. Exact verification:
```bash
cd .claude/scripts
uv run --extra dev pytest tests/test_cognition_amendments.py -q
uv run --extra dev ruff check ../chat/cognition/amendments.py tests/test_cognition_amendments.py
cd ../..
git diff --check
```
Backout exactly: disable autonomous constitutional mutation at its caller, drain/reconcile `prepared`/`published` rows, restore the previous two function signatures and result field set, and retain audit/rollback records. Never back out by restoring unaudited mutation.
