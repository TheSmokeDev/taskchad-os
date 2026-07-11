# PRP-E01A3: Skill-Promotion Audit-Fail-Closed Integration

**Status:** draft — requires WF2 review and source-specific hardening
**Primary POL anchors:** POL-SK-004, POL-PA-004
**Depends on:** E01A durable mutation audit contract

## Bounded outcome
Make promotion prepare durable audit before moving a generated skill into the live `promoted/` tree. Rejection, preview, and archive behavior are out of scope.

## Exact allowed files and symbols
- Existing `.claude/chat/cognition/skill_promotion.py`: `_audit`, `promote` only.
- Existing `.claude/scripts/tests/test_skill_promotion.py`: focused promotion tests only.

Confirmed behavior: module docs and `_audit` explicitly declare fail-open; `_audit` calls `skill_audit.append_skill_audit_record`, catches every exception, and returns no result. `promote` moves the physical directory, which activates the skill.

## API/behavior
Change `promote(..., *, operator_approved: bool, override_caution: bool=False, audit_store: MutationAuditStore, actor_id: str, tenant_id: str, persona_id: str, correlation_id: str) -> dict`. Prepare `operation="skill_promotion"`, target repository-relative promoted path, source/target content digest, policy decision ID, and idempotency key `skill-promotion:{name}:{source_hash}`. Audit prepare failure returns `{ok: False, outcome: "refused", reason: "audit_prepare_failed"}` with draft unmoved. Existing target/drift after prepare is `audit_conflict`. Successful atomic move transitions prepared→published→verified. Crash after move is `reconciliation_required`; retry verifies physical source/target hashes and finalizes without a second activation.

## Named RED tests (initial expected failure: unexpected keyword or fail-open move)
- `test_promote_audit_prepare_failure_leaves_generated_skill_inert`
- `test_promote_requires_explicit_identity_context`
- `test_promote_rechecks_source_and_target_after_prepare`
- `test_promote_concurrent_retry_moves_once_and_reuses_audit_id`
- `test_promote_recovers_crash_after_move_as_reconciled`
- `test_promote_audit_record_redacts_reason_and_confines_target`

GREEN: tests first; make `_audit` return typed preparation outcome for promote; compute digest after scan/approval but before move; lock source/target, recheck, prepare, atomic move, transition; reconcile retry. Run:
```bash
cd .claude/scripts
uv run --extra dev pytest tests/test_skill_promotion.py -q
uv run --extra dev ruff check ../chat/cognition/skill_promotion.py tests/test_skill_promotion.py
cd ../..
git diff --check
```
Backout exactly: disable promotion entry points, reconcile prepared/published records against physical generated/promoted trees, restore prior signatures only after all in-flight rows are terminal, and retain audit rows. Never re-enable fail-open promotion.
