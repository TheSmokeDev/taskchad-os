# PRP-001A: Durable Amendment Rollback Domain Service

**Status:** ready for implementation; **bounded Archon pilot**
**Owner:** one agent, amendment domain only
**Method:** strict TDD; temporary files only
**Depends on:** nothing; B-D are forbidden in this pilot

## 1. Goal and hard scope

Implement one safe, exact-byte compensating operation for one applied amendment, including durable intent, crash reconciliation, and cooperative writer serialization. Also make the existing direct apply write participate in the same lock scope. The deliverable is Python domain code/tests only.

Allowed production files: `.claude/chat/cognition/amendments.py` and new `.claude/chat/cognition/amendment_rollback.py`. Allowed tests: new `.claude/scripts/tests/test_amendment_rollback.py` and narrowly necessary amendments-domain tests. Do not modify CLI, API, `route_policy`, dashboard, chat, commands/workflows, or manuals. No remote actor/auth/UI assumptions.

## 2. Exact source anchors

- `amendments.py:28-36` `AMENDMENT_TARGETS`/`PROPOSAL_STATUSES`.
- `:47-70` `AmendmentProposal`: every existing field has a default.
- `:91-187` `_ledger_lock`/`ledger_file_lock`: 5-second, same-thread-reentrant OS advisory ledger lock.
- `:234-249` `ProposalLedger.read_all`: calls healing `_heal_missing_fields`; unsuitable for read-only listing.
- `:283-300` `_read_raw_lines`/`_iter_records`: non-healing read anchors.
- `:353-376` `_update_record`: updates every duplicate ID; rollback must not use it until uniqueness is proven and should add a unique-update primitive.
- `:574-692` `apply_amendment_if_allowed`: compare/snapshot/direct `Path.write_text`/ledger update currently are not one outer ledger+target critical section.
- `:695-789` `collapse_autonomous_amendments`: already holds ledger lock and atomically writes target; add target lock only around target compare/write scope.
- `:829-834` `_coerce_dataclass`: currently builds every dataclass field from `record.get(name)`, so an absent key is passed explicitly as `None` and overrides that field's dataclass default; unknown keys are ignored.
- existing `_atomic_write_text`, `_sha256`, `_write_rollback_snapshot` near module tail: text behavior is legacy precedent, not an exact-byte rollback primitive.
- `.claude/scripts/tests/test_cognition_amendments.py` and `test_amendment_pipeline_idempotence.py`: lock/crash/idempotence regressions.

## 3. Model and compatibility

Add statuses `rollback_pending`, `rolled_back`. Add **defaulted** proposal fields:

```python
rollback_actor: str | None = None
rollback_reason: str | None = None
rollback_requested_at: str | None = None
rolled_back_at: str | None = None
rollback_before_hash: str = ""
rollback_after_hash: str = ""
rollback_rescue_snapshot_path: str = ""
rollback_error: str | None = None
```

Defaults are mandatory for old ledger rows, but the current `_coerce_dataclass(cls, record)` does **not** honor them: it passes `record.get(name)` for every dataclass field, so every absent key is supplied as `None` and overrides its declared default. Change coercion to filter unknown fields while constructing kwargs only for keys actually present, exactly `cls(**{name: record[name] for name in names if name in record})`, so omitted keys use dataclass defaults. Preserve explicitly present values (including an explicit `None`) and continue ignoring unknown keys. No eager migration.

Export frozen `AmendmentSnapshot` and `AmendmentRollbackResult`; give defaults to all fields after the first default to satisfy dataclass ordering. Result status is `rolled_back|conflict|refused|failed`.

Stable reason codes (no ad-hoc alternatives):

- success: `rollback_completed`, `rollback_reconciled_after_crash`, `rollback_already_completed`;
- refusal: `invalid_proposal_id`, `proposal_not_found`, `duplicate_proposal_id`, `proposal_not_applied`, `missing_apply_hashes`, `invalid_actor`, `invalid_reason`, `target_not_allowed`, `target_path_invalid`, `target_missing`, `target_unreadable`, `snapshot_path_invalid`, `snapshot_missing`, `snapshot_unreadable`, `snapshot_hash_mismatch`;
- conflict: `target_hash_conflict`;
- failure: `lock_timeout`, `rescue_snapshot_failed`, `ledger_prepare_failed`, `target_restore_failed`, `target_verify_failed`, `ledger_finalize_failed`.

Duplicate parseable IDs are an integrity refusal distinct from not-found and mutate nothing.

## 4. Public API and listing

```python
def list_amendment_snapshots(ledger, memory_dir, *, proposal_id=None) -> list[AmendmentSnapshot]: ...
def rollback_amendment(proposal_id, actor, reason, *, ledger, memory_dir) -> AmendmentRollbackResult: ...
```

Listing parses raw lines through non-healing helpers; it must **not call `read_all()`**, heal IDs/timestamps, acquire a mutation lock, create directories, or write anything. Return only rows with non-empty snapshot paths, optionally exact-ID filtered, newest-first by `(applied_at or "", created_at or "", id)` descending. Duplicate IDs remain visible but ineligible with `duplicate_proposal_id`.

Eligibility requires applied state, hashes, confined regular non-symlink snapshot whose byte hash is `before_hash`, and confined regular non-symlink target whose byte hash is `after_hash`. Missing and unreadable target/snapshot have distinct reasons above. Listing catches read/stat errors into eligibility reasons and never repairs target/ledger. An absent ledger returns `[]` without creating it.

## 5. Exact-byte/path helpers

Add `_sha256_bytes(data)` and atomic durable bytes writer: sibling temp, binary write, flush+`os.fsync`, `os.replace`, verify propagated errors, best-effort POSIX parent-directory fsync, best-effort temp cleanup. Rescue and restore never decode/re-encode.

Snapshot confinement: stored path may be legacy relative or absolute, but resolved file must be beneath `<ledger.parent>/rollback`, regular, and non-symlink. Target is only `resolved(memory_dir)/proposal.target_file`; allowed basename, resolved parent exactly memory root, regular and non-symlink. Never accept target/snapshot from caller.

**Legacy limitation:** apply currently reads/writes UTF-8 text (`_read_text`, `_sha256(text)`, `write_text`) and may normalize newline behavior before hashes/snapshots exist. This PRP guarantees exact bytes from the recorded pre-apply snapshot onward; it does not reconstruct line endings already normalized by legacy apply. New byte helpers must define SHA-256 over stored bytes so rollback preserves legacy snapshot bytes exactly.

## 6. Lock and write protocol

Implement target advisory lock `<target>.lock` with bounded timeout and same-thread reentrancy semantics. Always ledger lock then target lock. This guarantee covers only cooperative amendment-domain writers; external editors and other code ignoring the lock remain possible. Therefore re-read/hash immediately before replace, but acknowledge an unavoidable TOCTOU window with uncooperative writers—do not claim universal prevention.

**Direct apply lock scope correction:** in `apply_amendment_if_allowed`, after pure policy evaluation, acquire outer ledger then target lock and re-read/reconcile target while locked; snapshot, target replace, verification, and ledger applied update stay in that critical section. Do not merely lock `write_text`. Use atomic write; preserve existing text semantics/output in this slice. Collapse keeps outer ledger lock and takes target lock around its target read/plan/snapshot/replace; no inverse order.

Fresh rollback under both locks:

1. Validate trimmed non-empty ID/actor/reason; actor <=200, reason <=2000. Re-read raw ledger and require exactly one ID.
2. Validate target/snapshot paths and byte hashes. Fresh status must be `applied`; current target=`after_hash`, snapshot=`before_hash`.
3. Atomically write+verify rescue bytes under ledger rollback dir; rescue hash=`after_hash`.
4. Uniquely rewrite row to `rollback_pending` with trimmed actor/reason, requested time, `rollback_before_hash=after_hash`, rescue path, empty error. Ensure durable success before target mutation.
5. Re-read target and require `after_hash`, atomically replace with snapshot bytes, re-read and require `before_hash`.
6. Uniquely finalize `rolled_back`, completion time, `rollback_after_hash=before_hash`, clear error. Return success only now.

Recovery: pending+target=`after_hash` validates recorded rescue hash, restores without another rescue/actor overwrite, then finalizes; pending+target=`before_hash` only finalizes and returns reconciled; other hash conflicts. Rolled-back+target=`before_hash` returns already-completed with zero writes; drift conflicts. Missing/unreadable target/snapshot are refusal outcomes; operation-level I/O after validated preparation maps to the specific failed code and never false success.

## 7. Detailed red/green plan

1. Model/coercion regression tests: deserialize an old row that omits all rollback keys and assert the exact defaults `rollback_actor is None`, `rollback_reason is None`, `rollback_requested_at is None`, `rolled_back_at is None`, `rollback_before_hash == ""`, `rollback_after_hash == ""`, `rollback_rescue_snapshot_path == ""`, and `rollback_error is None`. In the same test area, prove kwargs-only-present compatibility: known existing values survive unchanged, an explicitly present `None` remains `None`, and an extra unknown key is ignored without rejecting the row. Also assert both new statuses round-trip through ledger serialization/deserialization.
2. Non-healing list tests: absent ledger, malformed/missing IDs byte-identical, ordering/filter, duplicate ID, each eligibility reason, unreadable via mocked `open/stat`.
3. Helper tests: CRLF/LF, non-ASCII and arbitrary bytes round-trip; fsync/replace failures; no temp residue best effort.
4. Path tests: traversal, outside absolute path, directory, symlink snapshot/target, missing/unreadable distinctions.
5. Happy path test captures raw ledger after prepare via injection, proves pending precedes target mutation; verifies original/rescue/final SHA-256 and metadata.
6. Validation/state tests parameterize every reason code, including distinct duplicate ID and no mutation.
7. Failure injection at rescue, prepare, restore, verify, finalize; assert exact durable state and no false success.
8. Recovery tests for pending post/pre/conflicting hash; no second rescue/write; completed idempotence and drift.
9. Lock tests: timeout, same-thread nesting, two threads and portable two-process cooperative contention; one physical restore, no deadlock.
10. Apply regression: outer lock covers re-read through ledger update; injected cooperative rollback/apply cannot interleave; atomic text output unchanged. Collapse lock order regression.
11. Malformed/unrelated ledger lines remain byte-identical through unique status update.
12. Run focused then full amendment regressions.

## 8. Acceptance evidence

All mandatory: exact-byte restoration; pending durable before mutation; rescue/final hashes; conflict no mutation; every path/read/validation code tested; duplicate integrity refusal; both crash boundaries recover; idempotent completed behavior; cooperative concurrency bounded honestly; apply/collapse lock order; non-healing listing; malformed lines preserved; no live state. Evidence JSON in the PR description (no required repository artifact) lists changed files, each criterion/test name, exact command/exit output, and `live_state_touched:false`.

## 9. Validated commands

From repository root:

```bash
cd .claude/scripts
uv run --extra dev pytest tests/test_amendment_rollback.py -q
uv run --extra dev pytest tests/test_cognition_amendments.py tests/test_amendment_pipeline_idempotence.py -q
uv run --extra dev ruff check ../chat/cognition/amendments.py ../chat/cognition/amendment_rollback.py tests/test_amendment_rollback.py
uv run --extra dev pytest tests -q
```

These match `.claude/scripts/pyproject.toml`: Python >=3.12; pytest and ruff are in optional dependency group `dev`, hence `--extra dev`. No npm validation belongs to A.

## 10. Executable backout

Before deploying older code: stop amendment producers; run this version to reconcile every `rollback_pending`; back up ledger and memory root; disable apply. Revert service calls/byte-lock refactor only while retaining the two statuses and defaulted fields. If an old binary cannot recognize them, create a copied ledger, map `rolled_back` and reconciled pending rows to terminal `skipped` with an operator note, atomically install only after hash-verified backup; never edit the sole copy. Retain snapshots/rescues. Restore code immediately if any pending row cannot be reconciled; do not manually copy a snapshot or map it to applied/pending.

## 11. Definition of done

One agent can finish this slice without touching another surface; every listed test is real and green; full Python suite result is recorded (or an explicit pre-existing blocker); `git diff --check` is clean; diff contains only allowed domain/test files. B-D remain untouched.
