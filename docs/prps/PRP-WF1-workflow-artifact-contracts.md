# PRP-WF1-workflow-artifact-contracts: Behavior-preserving artifact contract extraction

**Status:** implementation-ready — independent review manifest passed
**Depends on:** none
**Review request:** `docs/prps/reviews/PRP-WF1-workflow-artifact-contracts.review.json`

## 1. Bounded outcome and source decision

Extract only the deterministic artifact **shape validation and context computation** duplicated in `.archon/workflows/implement-prp.yaml` into a stdlib-only Python file. Preserve the current 22-node Archon DAG, command payloads, gate decisions and diagnostic order, two Archon approval nodes, subprocess calls, direct writes, and publication behavior.

This is preservation, not a schema, orchestration, or publication-security migration:

- existing JSON schema remains integer `1`; no run/revision fields are added;
- `.archon/commands/prp-*.md` remain unchanged producer authorities;
- workflow completion and approval are exclusively Archon DAG state. They are not artifact fields and are not inferred or re-authorized by the validator;
- the validator may shape-check an existing `approval-manifest.json` and compare its existing fields to supplied/live context only at the current `publish-pr` consumer;
- current direct `Path.write_text(...)` calls remain direct;
- current `(stdout + stderr)[-4000:]` evidence behavior remains, with no secret scanner;
- payload/content binding and race closure belong to draft WF1B, not WF1.

## 2. Normative source inventory

This contract was transcribed from:

- `.archon/workflows/implement-prp.yaml` (22 nodes, inline readers/writers, checks, side effects);
- `.archon/commands/prp-preflight.md`, `prp-reconnaissance.md`, `prp-plan.md`, `prp-implement-test-first.md`, `prp-test-fix.md`;
- `.archon/commands/prp-review-spec.md`, `prp-review-security-state.md`, `prp-review-simplification.md`, `prp-docs-verification.md`, `prp-package.md`.

`prp-regression-validation.md` exists but is not called by this workflow. `regression-validation` is inline Python.

## 3. Exact implementation scope

Allowed existing file:

1. `.archon/workflows/implement-prp.yaml` — replace only duplicated shape/context calculations with imports/calls. Preserve IDs, list order, `depends_on`, commands, contexts, approvals, timeouts, sentinel, argv, `shell=False`, writes, side effects, artifact names, and first-failure messages/order.

Allowed new files:

2. `.archon/scripts/prp_artifact_contracts.py` — stdlib-only functions and optional CLI.
3. `.archon/scripts/test_prp_artifact_contracts.py` — unit/fixture tests.
4. `.archon/scripts/test_implement_prp_workflow.py` — YAML/DAG/wiring/preservation tests.

No command, product code, package metadata, lockfile, or other workflow may change. Planning metadata under `docs/prps/` is not implementation scope.

## 4. Canonical producer payloads and cross-field rules

“Canonical” is producer-test strictness. Workflow consumers must select the existing permissive policy in §5. Exact means no extra keys.

### 4.1 JSON producers

1. **baseline → `baseline.json`** (`worktree-guard`): exact `schema,root,git_dir,common_dir,branch,baseline_head,baseline_status`. `schema == 1` (not Boolean); resolved absolute path strings; nonempty branch/head; `baseline_status == ""`; producer context requires linked worktree (`git_dir != common_dir`), attached branch, and empty porcelain status.
2. **preflight → `preflight.json`** (`prp-preflight`): exact `schema,decision,prp_path,scope,allowed_paths,focused_tests,regression_tests,blockers`. Decision enum `proceed|revise|escalate|abort`; normalized confined PRP path and bounded scope strings; nonempty allowed paths; both test lists nonempty; blockers string array. Each test is exact `{cwd,argv}`. `cwd` is nonempty confined POSIX repository-relative text. `argv` is a nonempty-string list in one current allowlisted family. Producer rule: `decision == "proceed"` only when blockers is empty and path/scope/test discovery is valid; non-proceed is never readiness.
3. **reconnaissance → `reconnaissance.json`** (`prp-reconnaissance`): exact `schema,status,files,invariants,risks,evidence`; status enum `ready|revise|escalate|abort`; **all four arrays are nonempty arrays of strings**, per the command’s “nonempty relevant files, invariants, risks, and ... evidence”; evidence is path/symbol based. `ready` may not conceal a non-ready condition.
4. **implementation → `implementation.json`** (`prp-implement-test-first`): exact `schema,status,red_green_evidence,changed_files,blockers`; status enum `ready|incomplete|escalate`; all collections arrays. **`status == ready` iff concrete RED/GREEN evidence is nonempty and blockers is empty**; non-ready status must not be accepted as ready. Changed-file entries are strings. Nested evidence remains opaque because the command does not define it.
5. **focused_results → `test-results.json`** (`prp-test-fix`, then overwritten by `focused-test-gate`): exact `schema,status,runs,blockers`; command status enum `pass|fail|escalate`, deterministic overwrite `pass|fail`. Runs are exact `{spec,exit_code,evidence}`; spec is exact `{cwd,argv}`; exit code integer; evidence string. **Pass iff runs is nonempty, every exit code is exactly zero (integer, not Boolean), and blockers is empty.** The sentinel is valid only under that same condition. Gate evidence is final 4000 Python characters of concatenated stdout/stderr.
6. **regression → `regression.json`** (`regression-validation`): exact `schema,status,runs,skipped,blockers,changed_files,validated_diff_digest`; runs as above; arrays for skipped/blockers/changed files; lowercase 64-hex digest. Writer sets skipped/blockers empty. **Pass iff runs is nonempty and every exit code is exactly zero**; writer’s blockers/skipped remain empty.
7. **review_spec/security_state/simplification/docs → `review-*.json`** (four commands): exact `schema,verdict,findings,evidence`; verdict `pass|block`; findings exact `{severity,path,evidence,remedy}`, severity `blocking|advisory`, other values strings; evidence array with unspecified nested type. For spec and security-state, **verdict is `block` iff any blocking finding exists**. Simplification may block only material correctness/maintainability complexity, so canonical consistency is the same blocking-finding equivalence. Docs has no literal iff sentence; shape validation must not invent one, while the aggregate consumes its declared verdict.
8. **review_aggregate → `review-aggregate.json`** (`review-aggregate`): exact `schema,verdict,reviews`; reviews exact keys `spec,security-state,simplification,docs`, values `pass|block`; **aggregate pass iff every review verdict passes**, otherwise block.
9. **pr_package → `pr-package.json`** (`prp-package`): exact `schema,status,title,commit_message,branch,body_file,changed_files,test_evidence`; status `packaged`; body file `pr-body.md`; title/commit nonblank; branch equals baseline/current branch; changed files are strings and **exactly all tracked plus untracked paths, nonempty in current package gate, each contextually inside preflight allowed paths**; test evidence array. Shape validation cannot establish Git equality or scope: those are context/live-repository checks.
10. **approval_manifest → `approval-manifest.json`** (`package-gate`): exact `schema,baseline_head,branch,changed_files,approved_diff_digest`; nonempty strings, sorted changed-file strings, lowercase 64-hex digest. This records package-gate context; it is not proof that Archon’s `final-approval` occurred and must never be treated as approval authority.
11. **publication → `publish.json`** (`publish-pr`): exact `schema,status,branch,commit,url`; status `published`; nonempty branch/commit; URL matches `https://github.com/[^/]+/[^/]+/pull/[1-9][0-9]*`.

### 4.2 Non-JSON outputs

- `plan.md`/`$plan.output`: opaque plan material consumed by the Archon approval message; no validator approval inference.
- `implementation.md`: narrative, not read by the current deterministic gate.
- `pr-body.md`: existing package/publish file; package-gate checks file and nonblank text.
- approval responses: captured Archon orchestration state, not files or `ArtifactSet` members.

## 5. Shape validation versus context validation

`validate_payload(kind, payload, policy=...)` is pure shape/cross-field validation. Producer tests use `canonical`; workflow call sites use the exact current consumer policy:

- `preflight_gate`: `.get`, extras accepted; schema 1, decision proceed, falsey blockers, valid nonempty allowed paths, both nonempty valid spec lists.
- `reconnaissance_gate`: extras/missing uninspected fields accepted; schema 1, ready, truthy files/evidence only.
- `implementation_gate`: extras/missing uninspected fields accepted; schema 1, ready, truthy RED/GREEN evidence only.
- `focused_execution` / `regression_execution`: exact spec validation before execution; generated output is canonical.
- `review_aggregate`: review extras accepted; schema 1, verdict enum, findings is list; evidence and verdict/finding consistency are not currently checked.
- `review_gate`: preserve aggregate check, then focused check, then regression check. Preserve vacuous acceptance of missing/empty runs here because execution nodes establish nonempty runs.
- `package_gate`: exact package key set and current body/live Git/path/digest checks.
- `publish_gate`: existing baseline/package/manifest field comparisons and live Git/digest checks only.

Context/live-repository functions compute or compare root, head, branch, ancestry, changed paths, allowed-path containment, file presence/body text, and digest. They must not turn canonical producer promises into stricter workflow acceptance.

## 6. API: artifacts only, no orchestration authority

`.archon/scripts/prp_artifact_contracts.py` exports:

```python
class Reason(str, Enum):
    ARTIFACT_MISSING = "artifact_missing"
    JSON_INVALID = "json_invalid"
    SCHEMA_INVALID = "schema_invalid"
    FIELD_INVALID = "field_invalid"
    PATH_INVALID = "path_invalid"
    COMMAND_NOT_ALLOWLISTED = "command_not_allowlisted"
    HEAD_REVISION_MISMATCH = "head_revision_mismatch"
    BRANCH_MISMATCH = "branch_mismatch"
    ANCESTRY_MISMATCH = "ancestry_mismatch"
    CHANGED_FILES_MISMATCH = "changed_files_mismatch"
    DIFF_DIGEST_MISMATCH = "diff_digest_mismatch"

@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reason: Reason | None
    detail: str
    normalized: object | None = None

@dataclass(frozen=True)
class DiffState:
    changed_files: tuple[str, ...]
    digest: str

@dataclass(frozen=True)
class ArtifactSet:
    artifacts_dir: Path
    values: Mapping[str, object]

REQUIRED_ARTIFACTS: Mapping[str, tuple[str, ...]]
PRODUCED_ARTIFACTS: Mapping[str, tuple[str, ...]]

def validate_payload(kind: str, payload: object, *, policy: str = "canonical") -> ValidationResult: ...
def validate_argv(spec: object, repo_root: Path) -> ValidationResult: ...
def compute_regression_diff_state(repo_root: Path, head: str) -> DiffState: ...
def compute_publication_diff_state(repo_root: Path, base: str) -> DiffState: ...
def validate_package_context(state: ArtifactSet) -> ValidationResult: ...
def validate_publish_context(state: ArtifactSet) -> ValidationResult: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

There is deliberately no `completed_nodes`, `approvals`, `NODE_PREREQUISITES`, or general `validate_node` prerequisite authority. Archon executes `depends_on` and captures approvals. A validator invoked inside `package-gate` or `publish-pr` validates only artifacts/context already consumed there; it does not infer an approval from node position, plan output, package output, or manifest existence.

## 7. Literal 22-node transcription and call-site map

Timing is **pre-node** for consumed artifacts and **post-node (or at next consumer)** for produced artifacts. `—` means no structured artifact. “Inline import” means the existing `python - <<'PY'` block imports the plain script; no YAML `command:` is added. Command nodes remain model commands and do not call the CLI.

| # | node (`depends_on`) | required_artifacts (validation timing) | produced_artifacts (validation timing) | exact YAML call site / mode |
|---:|---|---|---|---|
|1|`worktree-guard` (—)|—|`baseline` (canonical post-write; also next consumer)|`nodes[0].bash`; inline import for generated shape only; retain Git guard/write|
|2|`preflight` (`worktree-guard`)|`baseline` (pre-node is command responsibility; no new deterministic call)|`preflight` (Archon `output_format`; gate validates next)|`nodes[1].command: prp-preflight`; command/Archon inline schema, no CLI|
|3|`preflight-gate` (`preflight`)|`preflight` (`preflight_gate`, pre-node)|—|`nodes[2].bash` Python; inline import|
|4|`reconnaissance` (`preflight-gate`)|`preflight` (pre-node command responsibility)|`reconnaissance` (gate validates next)|`nodes[3].command: prp-reconnaissance`; command/Archon schema, no CLI|
|5|`reconnaissance-gate` (`reconnaissance`)|`reconnaissance` (`reconnaissance_gate`, pre-node)|—|`nodes[4].bash` Python; inline import|
|6|`plan` (`reconnaissance-gate`)|`baseline,preflight,reconnaissance` (pre-node command responsibility)|`plan.md/$plan.output` (opaque)|`nodes[5].command: prp-plan`; no validator/CLI|
|7|`plan-approval` (`plan`)|`$plan.output` (Archon approval message)|captured approval response (Archon state)|`nodes[6].approval`; Archon native, never validator/CLI|
|8|`implementation` (`plan-approval`)|`baseline,preflight,reconnaissance,plan` (pre-node command responsibility)|`implementation,implementation.md` (gate validates JSON next)|`nodes[7].command: prp-implement-test-first`; no CLI|
|9|`implementation-gate` (`implementation`)|`implementation` (`implementation_gate`, pre-node)|—|`nodes[8].bash` Python; inline import|
|10|`focused-test-fix` (`implementation-gate`)|`preflight,implementation` plus plan/diff (loop prompt responsibility)|`focused_results` and sentinel (next node overwrites authoritatively)|`nodes[9].loop` using `prp-test-fix.md`; no CLI|
|11|`focused-test-gate` (`focused-test-fix`)|`preflight,baseline` (pre-execution payload/spec/root validation)|`focused_results` (canonical post-write)|`nodes[10].bash` Python; inline import helpers, retain execution/write|
|12|`regression-validation` (`focused-test-gate`)|`preflight,baseline` (pre-execution payload/spec/root validation)|`regression` (canonical post-write)|`nodes[11].bash` Python; inline import helpers and `compute_regression_diff_state`, retain execution/write|
|13|`spec-review` (`regression-validation`)|workflow evidence (command responsibility)|`review_spec` (aggregate validates next)|`nodes[12].command: prp-review-spec`; no CLI|
|14|`security-state-review` (`regression-validation`)|workflow evidence (command responsibility)|`review_security_state` (aggregate validates next)|`nodes[13].command: prp-review-security-state`; no CLI|
|15|`simplification-review` (`regression-validation`)|workflow evidence (command responsibility)|`review_simplification` (aggregate validates next)|`nodes[14].command: prp-review-simplification`; no CLI|
|16|`docs-review` (`regression-validation`)|workflow evidence (command responsibility)|`review_docs` (aggregate validates next)|`nodes[15].command: prp-docs-verification`; no CLI|
|17|`review-aggregate` (four reviews)|`review_spec,review_security_state,review_simplification,review_docs` (`review_aggregate`, pre-node, in that order)|`review_aggregate` (canonical post-write)|`nodes[16].bash` Python; inline import, retain aggregation/write|
|18|`review-gate` (`review-aggregate`)|`review_aggregate,focused_results,regression` (pre-node, exact order below)|—|`nodes[17].bash` Python; inline import policies|
|19|`package` (`review-gate`)|`baseline,preflight,focused_results,regression,review_aggregate` plus diff (command responsibility)|`pr_package,pr-body.md` (next gate validates)|`nodes[18].command: prp-package`; no CLI|
|20|`package-gate` (`package`)|`pr_package,preflight,baseline,regression,pr-body.md` (pre-node/context, current order)|`approval_manifest` (canonical post-write)|`nodes[19].bash` Python; inline import and `compute_publication_diff_state`, retain write|
|21|`final-approval` (`package-gate`)|`$package.output` in message; manifest is not approval authority|captured approval response (Archon state)|`nodes[20].approval`; Archon native, never validator/CLI|
|22|`publish-pr` (`final-approval`)|`pr_package,baseline,approval_manifest,pr-body.md` plus live Git (pre-side-effect; body is passed through, not currently reread)|`publication` (canonical post-write)|`nodes[21].bash` Python; inline import and `compute_publication_diff_state`, retain add/commit/push/gh/write|

`REQUIRED_ARTIFACTS` and `PRODUCED_ARTIFACTS` contain exactly these tuples, including empty tuples. They are transcription/test metadata only, not a second DAG scheduler. YAML list order and all `depends_on` arrays remain authoritative.

## 8. Exact diff computations (do not merge algorithms)

### 8.1 `compute_regression_diff_state(root, head)` — node 12 only

Preserve lines 182–186:

1. changed strings are `(git diff --name-only -z HEAD stdout + git ls-files --others --exclude-standard -z stdout).split("\0")`, empty removed, then `sorted(...)`; **no slash normalization and no deduplication**;
2. hash starts with verbatim bytes from `git diff --binary HEAD --`;
3. invoke untracked listing again; iterate its emitted order, not explicitly sorted/deduplicated;
4. append `path.encode() + b"\0" + (root/path).read_bytes() + b"\0"`;
5. return lowercase SHA-256 hex.

### 8.2 `compute_publication_diff_state(root, base)` — nodes 20 and 22

Preserve lines 258/263–265 and 294/296–298:

1. tracked=`git diff --name-only -z base`; extra=`git ls-files --others --exclude-standard -z`;
2. changed=`sorted(set(x.replace("\\", "/") for x in tracked+extra if x))`;
3. hash starts with verbatim bytes from `git diff --binary base --`;
4. iterate `sorted(set(extra)-{""})` and append the same encoded path/NUL/file bytes/NUL;
5. return lowercase SHA-256 hex.

Package uses `base=baseline_head`; publish uses `base=head` after requiring head equals baseline. Fixtures pin both algorithms and their intentional ordering/normalization/deduplication difference. There is no ambiguous `compute_diff_state`.

## 9. Paths and argv

Preserve exactly:

- preflight rejects backslash, colon, absolute POSIX path, and any `..` part;
- execution resolves `root/cwd`, then requires `cwd.relative_to(root)` (current symlink escape check);
- package scope accepts exact `allowed.rstrip('/')` or a child prefixed by it plus `/`;
- argv items are nonempty strings and accepted prefixes are `uv run --extra dev pytest ...`, `uv run --extra dev ruff ...`, `npm test ...`, `npm run typecheck ...`;
- execute list argv with `shell=False`; never interpolate model strings.

Do not change current prefix semantics: uv requires token 5 pytest/ruff; `npm test` permits trailing args.

## 10. Preserve node-local first-failure/check order

There is **no global reason precedence**. Each replacement preserves its node’s current check sequence and message. Shape helpers may return typed reasons internally, but callers sequence them to match source.

- `worktree-guard`: resolve root → git dir → common dir → branch → status → one combined linked/attached/clean rejection → baseline write.
- `preflight-gate`: load JSON → derive specs/allowed → evaluate valid paths and both spec lists as part of one `ok` → one authoritative-artifact rejection.
- `reconnaissance-gate`: load → schema → status → files truthiness → evidence truthiness (single condition/message).
- `implementation-gate`: load → schema → status → RED/GREEN truthiness (single condition/message).
- focused/regression each spec: exact key/type/nonempty argv check → resolved-cwd confinement → argv allowlist; then execute all → write → status rejection.
- `review-aggregate`: files in `spec,security-state,simplification,docs` order; for each schema → verdict enum → findings-list in one condition; then aggregate/write.
- `review-gate`: aggregate schema/verdict/review-value-set → focused status/exit codes → regression status/skipped/exit codes.

### 10.1 Literal `package-gate` ordered checks

After reading `pr-package`, `preflight`, `baseline`, `regression` in that order:

1. obtain live branch, then live head;
2. combined `head != baseline_head OR branch != baseline.branch` → `baseline HEAD or branch changed before approval`;
3. merge-base ancestor check → `baseline is not an ancestor`;
4. exact package key set, schema, status, package branch, body_file, nonblank title/commit in one condition → `invalid package schema/status/branch`;
5. `pr-body.md` exists and nonblank → `missing/empty PR body`;
6. compute tracked, extra, changed;
7. allowed is nonempty list; each entry string/nonempty/not absolute/no `..` (note: current package check does not repeat preflight backslash/colon rejection) → `invalid preflight allowed_paths`;
8. changed nonempty, every path in scope, and `sorted(pkg['changed_files']) == changed` in one condition → `changed paths violate preflight scope or package` (retain current direct indexing/type failure behavior unless a helper reproduces it);
9. compute publication digest;
10. `regression.validated_diff_digest != digest OR regression.changed_files != changed` → `code changed after deterministic regression validation`;
11. write manifest.

### 10.2 Literal `publish-pr` ordered checks and side effects

After reading package, baseline, approval manifest in that order:

1. obtain live branch, then live head;
2. `head != baseline_head OR approved.baseline_head != head` → `HEAD changed since baseline/approval (agent commits forbidden)`;
3. `branch != baseline.branch OR branch != pkg['branch'] OR branch != approved.branch` → `branch/package mismatch`;
4. ancestry check → `baseline ancestry failed`;
5. compute tracked, extra, changed;
6. `changed != approved.changed_files OR changed != sorted(pkg.get('changed_files', []))` → `changed files differ from approved package`;
7. compute publication digest;
8. digest mismatch → `approved diff digest changed`;
9. empty changed → `nothing to publish`;
10. only now `git add -- <changed>`;
11. compute staged; staged mismatch → `explicit staging mismatch`;
12. commit using package message → push → `gh pr create` using package title and body file;
13. URL regex check → `gh did not return a valid GitHub PR URL`;
14. resolve post-commit HEAD and write publication.

No generic validator may reorder these checks (for example, package shape before live head) or prevalidate body/title in publish where source does not.

## 11. Import and optional CLI wiring

Inline blocks resolve the repository and import the plain file without requiring `.archon` as a package:

```python
root = pathlib.Path(subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], text=True,
    capture_output=True, check=True, shell=False,
).stdout.strip()).resolve()
sys.path.insert(0, str(root / ".archon" / "scripts"))
from prp_artifact_contracts import validate_payload
```

Tests execute this import from a non-root cwd. Workflow uses **inline imports**, not CLI calls. Optional human/test CLI:

```bash
python .archon/scripts/prp_artifact_contracts.py validate-payload --kind preflight --policy preflight_gate --file "$ARTIFACTS_DIR/preflight.json"
python .archon/scripts/prp_artifact_contracts.py regression-diff-state --repo-root "$(git rev-parse --show-toplevel)" --head "$(git rev-parse HEAD)"
python .archon/scripts/prp_artifact_contracts.py publication-diff-state --repo-root "$(git rev-parse --show-toplevel)" --base "$(git rev-parse HEAD)"
```

Success exits 0; contract rejection 2; usage 64; internal error 1. Sorted JSON is written to stdout. CLI has no approval/completion flags.

## 12. RED/GREEN and exact tests

Run initial RED after adding tests but before module:

```bash
uv run --project .claude/scripts --extra dev pytest .archon/scripts/test_prp_artifact_contracts.py .archon/scripts/test_implement_prp_workflow.py -q
```

Expected real collection failure is missing `prp_artifact_contracts`; record actual output only.

Contract tests:

- `test_all_canonical_payloads_and_cross_field_rules`
- `test_consumer_policies_preserve_extra_and_uninspected_fields`
- `test_reconnaissance_requires_all_four_nonempty_collections_canonically`
- `test_ready_implementation_and_focused_pass_cross_field_equivalence`
- `test_review_verdict_matches_blocking_findings_for_command_contracts`
- `test_package_shape_is_separate_from_live_scope_context`
- `test_preflight_paths_and_four_argv_families`
- `test_regression_diff_algorithm_preserves_git_order_and_duplicates`
- `test_publication_diff_algorithm_sorts_deduplicates_and_normalizes`
- `test_package_gate_literal_check_order_and_messages`
- `test_publish_gate_literal_check_order_and_zero_prior_side_effects`
- `test_approval_manifest_is_context_not_approval_authority`
- `test_artifact_set_has_no_completion_or_approval_state`
- `test_evidence_suffix_and_non_echoing_diagnostics`
- `test_plain_file_import_and_cli_exit_codes`

Workflow tests:

- `test_yaml_safe_loads_with_exact_22_node_order_and_dependencies`
- `test_required_and_produced_artifact_maps_match_all_22_rows`
- `test_four_review_fanout_and_fanin`
- `test_two_archon_approvals_remain_and_validator_does_not_infer_them`
- `test_each_exact_yaml_callsite_uses_inline_import_or_unchanged_command`
- `test_commands_artifact_names_contexts_timeouts_and_sentinel_unchanged`
- `test_literal_argv_shell_false_execution_unchanged`
- `test_direct_writes_remain_non_atomic`
- `test_publish_checks_precede_add_commit_push_and_gh`

Minimal GREEN: implement strict payload tests; consumer policies; path/argv; the two separate diff functions; package/publish context helpers preserving call order; optional CLI; then replace one inline family at a time and rerun tests. Do not commit, push, clean, or broaden scope.

## 13. Preserved WF1 limitations and WF1B ownership

WF1 explicitly accepts these current limitations; none may be claimed fixed:

1. **Approval TOCTOU for package metadata/content:** approval manifest binds changed paths and repository diff, but not `pr-body.md`, title, commit message, package JSON digest, or resulting commit identity.
2. **Mutable baseline root trust:** later nodes trust `baseline.json['root']`, a mutable direct-written artifact, rather than an independently anchored repository identity.
3. **Untracked symlink digest mismatch:** hashing uses `Path.read_bytes()` (dereferenced content), while Git staging records a symlink object/link target; approval digest is not a Git-object digest.
4. **Validation-to-staging race:** edits can occur after publish validation and before/during `git add`.
5. **Direct non-atomic artifacts:** crashes can leave partial JSON/text; readers fail on malformed JSON but writers do not replace atomically.
6. **No secret scanner:** evidence truncation is not redaction and opaque command evidence may contain secrets/private paths.

Draft [`PRP-WF1B-publication-binding-hardening.md`](PRP-WF1B-publication-binding-hardening.md) owns package/body/title/commit digest binding, immutable baseline root anchoring, symlink-aware Git-object digesting, validation-to-staging race closure, and atomic artifact writes. Secret scanning remains explicitly unowned until a policy/scanner is selected; WF1B must not imply truncation solves it.

## 14. Verification and acceptance

```bash
python -c "from pathlib import Path; import yaml; d=yaml.safe_load(Path('.archon/workflows/implement-prp.yaml').read_text(encoding='utf-8')); assert len(d['nodes']) == 22; print([n['id'] for n in d['nodes']])"
archon workflow list
python -c "from pathlib import Path; import re; p=Path('docs/prps/PRP-WF1-workflow-artifact-contracts.md').read_text(); assert len(re.findall(r'^\|[0-9]+\|`',p,re.M)) == 22"
uv run --project .claude/scripts --extra dev pytest .archon/scripts/test_prp_artifact_contracts.py .archon/scripts/test_implement_prp_workflow.py -q
uv run --project .claude/scripts --extra dev ruff check .archon/scripts/prp_artifact_contracts.py .archon/scripts/test_prp_artifact_contracts.py .archon/scripts/test_implement_prp_workflow.py
git diff --check
git status --short
git diff -- .archon/workflows/implement-prp.yaml .archon/scripts docs/prps
```

Planning acceptance requires YAML parse with the exact listed IDs, `archon workflow list` discovery with `errorCount: 0` and `implement-prp`, 22 literal mapping rows, valid links, current source alignment, and fresh manifest hashes. Implementation acceptance additionally requires all named tests and Ruff green.

Independent review must fill the bound manifest. The author does **not** mark ready. No commit/push/clean/product change, auto-merge, approval bypass, or publication-hardening claim is permitted.

## 15. Backout

Restore only the original inline calculations in `.archon/workflows/implement-prp.yaml` and delete the three implementation files. Preserve run artifacts. Never back out by weakening either Archon approval or publication checks.
