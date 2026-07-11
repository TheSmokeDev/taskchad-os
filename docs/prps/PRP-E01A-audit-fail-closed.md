# PRP-E01A: Durable Mutation Audit Contract

**Status:** draft — requires WF2 review and source-specific hardening
**Primary POL anchors:** POL-AM-002, POL-AM-003, POL-PA-004
**Depends on:** none

## Bounded outcome
Create one durable audit record/store contract. This slice does not integrate amendment or skill mutation; E01A2 and E01A3 do that separately.

## Exact allowed files
- Intended new `.claude/chat/mutation_audit.py`: `MutationAuditRecord`, `AuditPrepareResult`, `MutationAuditStore`, `prepare_mutation_audit`, `mark_mutation_audit`.
- Intended new `.claude/scripts/tests/test_mutation_audit.py`: only tests for this contract/store.

## Confirmed baseline
`.claude/chat/skill_audit.py:append_skill_audit_record` appends JSONL and explicitly returns `None` on failure. `.claude/chat/cognition/skill_promotion.py:_audit` catches failures. `.claude/chat/cognition/amendments.py:apply_amendment_if_allowed` writes rollback/target before its ledger finalization. No shared fail-closed mutation audit authority exists.

## Typed API
```python
@dataclass(frozen=True)
class MutationAuditRecord:
    audit_id: str; idempotency_key: str; operation: str; actor_id: str
    tenant_id: str; persona_id: str; correlation_id: str; target: str
    before_hash: str; intended_after_hash: str; policy_decision_id: str
    state: Literal["prepared", "published", "verified", "failed", "reconciled"]
    reason: str; created_at: str; updated_at: str

@dataclass(frozen=True)
class AuditPrepareResult:
    ok: bool; audit_id: str | None; state: str; reason: str

class MutationAuditStore(Protocol):
    def prepare(self, record: MutationAuditRecord) -> AuditPrepareResult: ...
    def transition(self, audit_id: str, expected: str, target: str, reason: str) -> bool: ...
    def get(self, audit_id: str) -> MutationAuditRecord | None: ...
```
Reasons: `audit_unavailable`, `audit_prepare_failed`, `audit_conflict`, `invalid_transition`, `publication_failed`, `reconciliation_required`, `internal_error`. Unknown exceptions become redacted `internal_error`, never success.

| State | Allowed next |
|---|---|
| prepared | published, failed |
| published | verified, reconciliation_required |
| reconciliation_required | reconciled, failed |
| verified/reconciled/failed | terminal |

Prepare is durable before returning `ok=True` (temp file, flush/fsync, atomic replace, parent-directory sync where supported). Same idempotency key plus identical digest returns the same record; different content is `audit_conflict`. Writers serialize under one lock; crash recovery ignores/truncates an incomplete tail and never invents success.

## RED tests and expected initial failure
`test_mutation_audit_prepare_is_durable_and_round_trips`, `test_mutation_audit_rejects_conflicting_idempotency_key`, `test_mutation_audit_transitions_are_cas_and_monotonic`, `test_mutation_audit_concurrent_prepare_has_one_identity`, `test_mutation_audit_recovers_incomplete_tail`, `test_mutation_audit_redacts_internal_exception`. All initially fail because `mutation_audit` does not exist.

## GREEN and verification
Add tests first; implement only the API/store above; inject write/fsync/replace faults; prove no `ok=True` before durability. Run:
```bash
cd .claude/scripts
uv run --extra dev pytest tests/test_mutation_audit.py -q
uv run --extra dev ruff check ../chat/mutation_audit.py tests/test_mutation_audit.py
cd ../..
git diff --check
```
Backout exactly: stop new callers (none in this slice), retain the audit JSONL as evidence, delete the two intended-new files. Acceptance requires every named test and command pass with recorded revision/exits.
