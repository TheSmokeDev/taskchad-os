# PRP-WF1-workflow-artifact-contracts: Workflow Artifact Contracts

**Status:** draft — requires WF2 review and source-specific hardening
**Depends on:** none

## Bounded outcome
Extract the artifact validation now embedded in `.archon/workflows/implement-prp.yaml` into deterministic, versioned Python contracts without changing product behavior, publication authority, or the two approval gates.

## Exact allowed files
Existing:
- `.archon/workflows/implement-prp.yaml` — replace inline artifact checks with calls to the validator while preserving node IDs and dependencies.

Intended new (no other file is allowed):
- `.archon/scripts/prp_artifact_contracts.py` — symbols `ArtifactReason`, `ValidationResult`, `validate_artifact(kind, payload, *, run_id, revision)`, `validate_argv(spec, repo_root)`, and `validate_transition(previous, current)`.
- `.archon/scripts/test_prp_artifact_contracts.py` — focused contract tests.
- `.archon/scripts/test_implement_prp_workflow.py` — YAML/parser and workflow wiring tests.

## Confirmed current behavior
`implement-prp.yaml` currently validates baseline, preflight, reconnaissance, implementation, focused/regression results, review aggregate, package, approval digest, and publication with inline Python. It requires schema `1`; preflight accepts only structured argv beginning `uv run --extra dev pytest`, `uv run --extra dev ruff`, `npm test`, or `npm run typecheck`; subprocess execution uses `shell=False`; package/publish bind the changed-file list and diff digest. There is no existing workflow-rail test in `.claude/scripts/tests`, and Ruff cannot validate YAML.

## Contract
```python
class ArtifactReason(str, Enum):
    ARTIFACT_MISSING = "artifact_missing"
    SCHEMA_INVALID = "schema_invalid"
    SCOPE_INVALID = "scope_invalid"
    COMMAND_NOT_ALLOWLISTED = "command_not_allowlisted"
    REDACTION_FAILED = "redaction_failed"
    RUN_MISMATCH = "run_mismatch"
    REVISION_MISMATCH = "revision_mismatch"
    TRANSITION_INVALID = "transition_invalid"

@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: ArtifactReason | None
    normalized: dict[str, object] | None

def validate_artifact(kind: str, payload: object, *, run_id: str, revision: str) -> ValidationResult: ...
def validate_argv(spec: object, repo_root: Path) -> ValidationResult: ...
def validate_transition(previous: str, current: str) -> ValidationResult: ...
```
Schemas reject missing and extra keys. Paths are repository-relative POSIX paths with no absolute, drive-prefixed, `..`, NUL, or symlink escape. Commands remain literal argv; no interpolation or shell. Every artifact binds schema version, run ID, and source revision. Approval binds the exact final diff digest.

| Previous | Allowed next | Otherwise |
|---|---|---|
| absent | baseline | `transition_invalid` |
| baseline | preflight | `transition_invalid` |
| preflight | reconnaissance | `transition_invalid` |
| reconnaissance | implementation | `transition_invalid` |
| implementation | focused, regression | `transition_invalid` |
| focused + regression | reviews | `transition_invalid` |
| passing reviews | package | `transition_invalid` |
| package | approved | `transition_invalid` |
| approved, same digest | published | `revision_mismatch` or `transition_invalid` |

Any malformed, stale, denied, or redaction-failing input has zero publish side effects.

## RED tests (exact names and expected initial failure)
In `.archon/scripts/test_prp_artifact_contracts.py`:
- `test_validate_artifact_rejects_missing_and_extra_fields` — import/file absent.
- `test_validate_argv_rejects_shell_and_path_escape` — import/file absent.
- `test_validate_artifact_rejects_run_or_revision_mismatch` — import/file absent.
- `test_validate_transition_rejects_skip_and_regression` — import/file absent.
- `test_approval_rejects_changed_diff_digest` — import/file absent.
- `test_redaction_rejects_secret_and_absolute_private_path` — import/file absent.

In `.archon/scripts/test_implement_prp_workflow.py`:
- `test_implement_prp_yaml_safe_loads` — file absent.
- `test_implement_prp_preserves_two_approval_gates_and_no_auto_merge` — file absent.
- `test_publish_depends_on_validated_approval_digest` — file absent.

## GREEN sequence
1. Add failing tests and capture the import/file-not-found RED result.
2. Implement enums, immutable result, strict per-kind field sets, path/argv validation, redaction, and monotonic transitions.
3. Replace duplicated inline checks node-by-node; do not rename/reorder gates.
4. Parse YAML and assert node/dependency/security invariants.
5. Run the exact focused commands below; record argv, revision, environment, exit, and output.

## Concurrency, crash, and backout
Validation is pure. Artifact writes remain write-temp/fsync/atomic-replace and must reject stale run/revision/digest. Concurrent source edits between validation and approval/publish block publication. A crash leaves either the prior complete artifact or the new complete artifact, never a partial success. Backout exactly: revert `implement-prp.yaml` to its prior inline validators and delete the three intended-new Python files; retain run artifacts and verify both approvals, dirty/direct-checkout refusal, and digest mismatch still block. Never back out by bypassing a gate.

## Proven repository commands
Run from repository root:
```bash
uv run --project .claude/scripts --extra dev pytest .archon/scripts/test_prp_artifact_contracts.py .archon/scripts/test_implement_prp_workflow.py -q
uv run --project .claude/scripts --extra dev ruff check .archon/scripts/prp_artifact_contracts.py .archon/scripts/test_prp_artifact_contracts.py .archon/scripts/test_implement_prp_workflow.py
uv run --project .claude/scripts python -c "from pathlib import Path; import yaml; p=Path('.archon/workflows/implement-prp.yaml'); d=yaml.safe_load(p.read_text(encoding='utf-8')); assert isinstance(d, dict) and d.get('name')"
archon workflow list
git diff --check
```
The pytest commands are expected to fail RED until the intended tests exist. The YAML parser is the direct syntax check; `archon workflow list` performs repository workflow discovery/parsing and must report `errorCount: 0` and list `implement-prp`. The installed CLI has no `workflow validate` subcommand. No command runs Ruff over `.archon` or YAML.

## Acceptance
All named tests pass; parser and Archon validation pass; hostile/race fixtures return stable reasons with no publication; existing node IDs, two approvals, `shell=False`, clean-worktree boundary, and no-auto-merge invariant remain intact; `git diff --check` passes.
