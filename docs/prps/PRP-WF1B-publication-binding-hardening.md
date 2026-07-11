# PRP-WF1B-publication-binding-hardening: Digest-bound publication transaction

**Status:** implementation-ready — digest-bound independent security review passed
**Depends on:** WF1 implemented and independently accepted
**Review request:** [`docs/prps/reviews/PRP-WF1B-publication-binding-hardening.review.json`](reviews/PRP-WF1B-publication-binding-hardening.review.json)

## 1. Bounded outcome

Harden only the publication tail of the reviewed WF1 contract. The final human approval must identify one immutable publication revision containing the exact package JSON bytes, PR body bytes, title, commit message, trusted baseline/repository identity, branch, prospective Git tree and deterministic commit. `publish-pr` must revalidate that revision before its first repository or remote publication side effect, install and commit the exact approved Git objects without rereading the worktree, push once, and create a PR with the approved title/body. Keep the current 22-node DAG, both existing approval nodes, and no-auto-merge policy.

This document is reconciled to reviewed WF1 head `e50dcd4` and its actual `.archon/workflows/implement-prp.yaml`, `.archon/scripts/prp_artifact_contracts.py`, `.archon/scripts/test_prp_artifact_contracts.py`, and `.archon/scripts/test_implement_prp_workflow.py`. Independent review, not this author, decides readiness.

## 2. Security decision and non-goals

An approval is the conjunction of (a) Archon's successful `final-approval` node and (b) the exact revision and digest rendered in that node's message. A manifest is context, never authorization. There is no approval field accepted from JSON and no resume path that infers approval from artifact presence.

Owned threats: artifact mutation/substitution, mutable `baseline.json.root`, worktree/common-dir substitution, tracked or untracked symlinks, mode/path/rename/delete changes, index contamination, edits during staging, hook mutation, concurrent publication, malformed/partial files, and retry after partial remote effects.

Explicitly separate/non-goal: repository secret scanning and evidence redaction. No scanner, pattern set, false-positive policy, or producer coverage has been approved. WF1B must not invent one or claim disclosure safety; truncation is not redaction. Also excluded: auto-merge, approval bypass, arbitrary model-supplied commands, product code, general workflow redesign, remote rollback, and changing the first `plan-approval` semantics.

## 3. Exact implementation scope

Allowed existing files/symbols:

1. `.archon/workflows/implement-prp.yaml`
   - `worktree-guard`: call trusted repository discovery and atomic writer;
   - deterministic artifact writers in `focused-test-gate`, `regression-validation`, `review-aggregate`, `package-gate`, and `publish-pr`: use the atomic writer;
   - `package-gate`: build the sealed transaction and write schema-2 manifest;
   - `final-approval`: render `approval_revision`, `approval_digest`, expected commit/tree, title and body digest while retaining `capture_response: true`;
   - `publish-pr`: acquire the publication lock, validate, install exact objects, update branch, push, create PR, write result;
   - preserve all 22 IDs/order/dependencies, command nodes, four reviews, loop, timeouts, and both approval nodes.
2. `.archon/scripts/prp_artifact_contracts.py` (landed by WF1)
   - retain WF1 exports and consumer policies;
   - retain `DiffState` and `compute_regression_diff_state` unchanged; replace publication-only `compute_publication_diff_state`, `_publication_changed_files`, `_publication_digest`, `validate_package_context`, and `validate_publish_context`, their CLI `publication-diff-state` behavior, and their two workflow callers as specified below; regression semantics remain unchanged. Update `_canon_baseline`, `_canon_approval_manifest`, `_canon_publication`, `_CANONICAL_VALIDATORS`, and `REQUIRED_ARTIFACTS`/`PRODUCED_ARTIFACTS` only for the schema-2 artifacts named here.
3. `.archon/scripts/test_prp_artifact_contracts.py` and `.archon/scripts/test_implement_prp_workflow.py` — extend only.

Allowed new files:

4. `.archon/scripts/prp_publication_binding.py` — stdlib-only binding, repository identity, temporary object/index transaction, locking, atomic writes, and publication state machine.
5. `.archon/scripts/test_prp_publication_binding.py` — unit and real-Git adversarial fixtures.
6. `docs/prps/reviews/PRP-WF1B-publication-binding-hardening.review.json` — digest-bound review request.

No other file or symbol may change. In particular, `.archon/commands/**`, product/package files, lockfiles, other workflows, and the canonical index are excluded. The index already states the correct WF1 prerequisite and review-candidate-not-ready meaning; no wording change is needed.

## 4. Trusted invocation and baseline identity

`worktree-guard` obtains trusted values from its actual process cwd before reading any artifact:

```python
@dataclass(frozen=True)
class RepositoryIdentity:
    root: Path                 # realpath of git --show-toplevel
    git_dir: Path              # realpath of git rev-parse --path-format=absolute --git-dir
    common_dir: Path           # realpath of ... --git-common-dir
    worktree_id: str           # sha256(domain + NUL + canonical identity bytes)
    object_format: str         # exactly sha1 or sha256
    branch_ref: str            # full refs/heads/... symbolic ref
    baseline_commit: str       # git rev-parse --verify HEAD^{commit}
```

`discover_repository(cwd: Path) -> RepositoryIdentity` invokes list argv with `shell=False`, rejects bare/detached repositories, `git_dir == common_dir`, missing/non-directory paths, symlinked `git_dir` or `common_dir` path components, a root not registered by `git worktree list --porcelain -z`, and a registered worktree whose `HEAD`/branch differs. Canonical identity bytes are UTF-8 JSON (JCS subset in §6) of normalized absolute root/git/common paths, full branch ref, baseline commit and object format. Windows comparisons use `os.path.normcase(realpath)` and accept drive-letter/case differences only after resolving the same file identity; POSIX comparisons are byte/case sensitive. UNC and non-UTF-8 Git paths fail closed with `repository_identity_invalid`.

The trusted `RepositoryIdentity` is passed in memory within each inline process and rediscovered at package and publish. `baseline.json` schema 2 records it but never selects cwd/root. Consumers compare every field to discovery. Moving/deleting a worktree, replacing `.git`, changing common-dir registration, branch/ref, object format, or baseline commit fails before transaction use.

## 5. Deterministic prospective Git transaction

### 5.1 Build at `package-gate`

Under the exclusive lock in §10:

1. Rediscover identity; validate package/body and prior regression in the existing package-gate order through its current final digest check. Then reread package/body as bytes.
2. Create mode-0700 `$ARTIFACTS_DIR/publication-transactions/.<run-id>.tmp`; reject static symlinks/reparse points in every existing component. Its exact files are `index`, `objects/` (loose objects only), `sealed-pr-package.json`, `sealed-pr-body.md`, and `transaction.json`. Copy the already-open package/body bytes to those names before object construction with `atomic_write(..., mode=0o400, overwrite=False)`. `transaction.json` is canonical JSON containing `schema,payload,object_count` and is written last. Use `GIT_ALTERNATE_OBJECT_DIRECTORIES=<trusted common_dir>/objects`; do not create an `objects/info/alternates` file. Set only scoped environment: `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`, `GIT_ALTERNATE_OBJECT_DIRECTORIES`; never mutate the user's index.
3. `git read-tree <baseline_commit>` into the empty temporary index, then `git add -A -- <exact allowed pathspec roots>`. This snapshots regular files, executable mode, symlink target bytes, deletions and renames according to Git index semantics. It does not follow a symlink as file content. Reject submodules/gitlinks and paths outside package scope.
4. Immediately run `git write-tree`. Derive changed entries solely with `git diff-tree --root -r -z --raw --no-abbrev --find-renames=50% <baseline_commit> <tree>`. Rename detection is therefore mandatory, deterministic at Git's 50% threshold, and copies (`C`) are disabled. The normative grammar is `":" old_mode SP new_mode SP old_oid SP new_oid SP status [score] NUL path NUL` for `A/M/D/T`, and the same header followed by `old_path NUL new_path NUL` for `R`; modes are exactly six octal digits; OIDs are exact object-format width lowercase hex; status is one of `A,M,D,T,R`; score exists only for `R`, is three decimal digits `000..100`. `A`: old mode `000000`, old OID all-zero, new mode/OID present. `D`: new mode `000000`, new OID all-zero, old present. `M`: both present and modes equal. `T`: both present and modes differ. `R`: both present, score required, two paths. Reject all other combinations, malformed/duplicate records, non-UTF-8 or non-canonical POSIX paths, and resulting modes other than `100644`, `100755`, `120000` (a deletion's old mode is checked). The package changed-path projection is the sorted unique union of `path` for `A/M/D/T` and both `old_path` and `new_path` for `R`; `pkg.changed_files` must equal that projection exactly. Thus rename source and destination are both approved.
5. Freeze author/committer name/email from `git var GIT_AUTHOR_IDENT`/`GIT_COMMITTER_IDENT`; freeze one UTC integer timestamp and `+0000` offset for both. Reject multiline/NUL identities. Create the commit in the temporary object directory with `git commit-tree <tree> -p <baseline_commit>` and exact UTF-8 `commit_message` bytes plus one terminating LF. Disable hooks by using plumbing; no `git commit` is called.
6. Verify with `git cat-file` against the temporary object store that the commit has exactly the approved tree, parent, identities, timestamps and message. Enumerate every loose object required by the new commit that is absent from the baseline reachable-object set. After verification and `transaction.json`, fsync every file, best-effort fsync each directory bottom-up, chmod files 0400/directories 0500 (Windows read-only is advisory), and atomically rename the directory to `<run-id>.sealed`; then fsync `publication-transactions`. The manifest is written only after that rename and binds `publication-transactions/<run-id>.sealed`, exact filenames and inventory. Failure to apply mandatory POSIX permissions/fsync fails sealing; documented Windows unsupported directory-fsync/read-only limitations are advisory.

Concurrent edits during `git add` can only produce a self-consistent written tree. After `write-tree`, validation uses tree objects, never the worktree. Any edit may be omitted or captured, but the human sees and approves the resulting tree digest/commit; package `changed_files` must still match. No later staging occurs.

### 5.2 Publish exact objects

After final approval, `publish-pr` acquires the same exclusive lock and runs every check in §9. Only then is the **first repository publication side effect** allowed: copy each verified loose object from the sealed temporary object directory into the trusted common object database using same-directory create/fsync/rename without overwrite mismatch. Rehash every installed object. Then atomically `git update-ref <branch_ref> <expected_commit> <baseline_commit>`. No checkout, `git add`, `git commit`, index mutation, or hook runs. The working tree may consequently show changes already committed; that is expected.

Push exact `<expected_commit>:<branch_ref>` with one lease selected from the already validated remote state: absent remote uses `--force-with-lease=<branch_ref>:` (explicit empty expectation); remote at baseline uses `--force-with-lease=<branch_ref>:<baseline_commit>`; remote already at `<expected_commit>` performs no push and only verifies the exact ref. No other remote state may push, and no broader refspec is allowed. Create the PR only after confirming local and `origin/<branch>` equal expected commit. Immediately before the first create attempt in a publication invocation, reopen `sealed-pr-body.md` once with the confined, no-follow regular-file reader, require nonempty valid UTF-8 with no NUL, recompute `body_bytes_digest`, and require equality to the approved payload. Store the bytes returned by that read as immutable in-memory `approved_body_bytes: bytes`; this is the final pathname read and any permitted retry reuses the same snapshot. Invoke `gh pr create` with list argv, literal `--body-file`, literal `-`, and `shell=False`, passing exactly `approved_body_bytes` as subprocess stdin. `gh` must never reopen `sealed-pr-body.md`, and no temporary body pathname is permitted.

## 6. Binding and schemas

All digests are lowercase SHA-256 hex with domain separation. `canonical_json(value)` is UTF-8, `json.dumps(..., ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))`; inputs permit only JSON null/Boolean/integer/string/list/object, unique keys, and Unicode scalar strings (no floats or lone surrogates). Length-prefix each byte field as `uint64be(length) || bytes`.

```text
approval_digest = SHA256(
  "taskchad:implement-prp:approval:v2\0" ||
  LP(canonical approval_payload JSON)
)
package_bytes_digest = SHA256("taskchad:pr-package:bytes:v2\0" || LP(exact pr-package.json bytes))
body_bytes_digest    = SHA256("taskchad:pr-body:bytes:v2\0" || LP(exact pr-body.md bytes))
object_state_digest  = SHA256("taskchad:git-tree:v2\0" || LP(object_format) || LP(tree_oid) || LP(object_inventory_digest))
```

`LP(x)` is unsigned 64-bit big-endian byte length followed by `x`; record counts use the same unsigned 64-bit big-endian encoding. The normative object inventory bytes are:

```text
"taskchad:git-object-inventory:v2\0" || uint64be(record_count) ||
  for each record sorted by raw lowercase-ASCII OID bytes:
    LP(type_ascii) || LP(oid_ascii) || LP(canonical_content)
```

`type_ascii` is exactly `blob`, `tree`, or `commit`. `canonical_content` is the uncompressed Git object payload returned by `git cat-file <type> <oid>` (not the loose zlib stream and not `"type size\0"` plus payload). Its length is therefore the authoritative object size; no separate size integer is encoded. Duplicate OIDs, unsupported types, an OID not equal to Git's hash of `type SP decimal-size NUL content`, or an inventory not byte-sorted by OID fails. Fixed SHA-256 inventory test vectors (independent of repository object format): empty records → `0c1fd15bd502a782875d336536131e6ac86959f9bfeeccc4ed030ea6a0c71757`; one record `("blob","0000000000000000000000000000000000000000",b"hello\n")` → `25ab149c21f502feae25a65cb4098c77b174154f40ba34df49c8ba5ddf38e6b0`; adding sorted record `("tree","ffffffffffffffffffffffffffffffffffffffff",b"100644 a\x00" + 20 zero bytes)` → `c699ddcd6fbcc8617c061cd8e0f649c150e321b35b907240d4cb67272d1891c2`. Tests must construct these bytes directly and assert all three values, plus reject reordered/length-endian/compressed-content variants.

`approval_revision` is exactly `2:<run_id>:<approval_digest[0:16]>`. `run_id` is 32 lowercase random hex generated once by `package-gate`; uniqueness is context, not authority. Repackaging always creates a new run/revision and invalidates the old transaction. Approval payload schema 2 has exact keys:

```json
{
  "schema": 2,
  "run_id": "32 lowercase hex",
  "package_bytes_digest": "64hex",
  "body_bytes_digest": "64hex",
  "title": "exact package title string",
  "commit_message": "exact package commit_message string",
  "branch_ref": "refs/heads/...",
  "repository": {
    "worktree_id": "64hex", "root": "normalized absolute text",
    "git_dir": "normalized absolute text", "common_dir": "normalized absolute text",
    "object_format": "sha1|sha256", "repository_slug": "owner/name"
  },
  "pr_base_branch": "exact default branch name",
  "baseline_commit": "full object id",
  "baseline_tree": "full object id",
  "changed_entries": [{"status":"A|M|D|T|R","score":"three digits|null","old_mode":"six octal digits","new_mode":"six octal digits","old_oid":"full or all-zero oid","new_oid":"full or all-zero oid","old_path":"POSIX path|null","new_path":"POSIX path"}],
  "tree_oid": "full oid",
  "object_inventory_digest": "64hex",
  "object_state_digest": "64hex",
  "commit_oid": "full oid",
  "author_ident": "exact normalized ident",
  "committer_ident": "exact normalized ident",
  "timestamp": "integer UTC epoch text +0000"
}
```

`approval-manifest.json` schema 2 has exact keys `schema,approval_revision,approval_digest,payload,sealed_transaction`. `payload` is exactly above. `sealed_transaction` is exactly `{"path":"publication-transactions/<run-id>.sealed","package":"sealed-pr-package.json","body":"sealed-pr-body.md","metadata":"transaction.json","object_count":<nonnegative integer>}`. Any other name, absolute path, `..`, backslash, colon, symlink/reparse point, or escape fails. `publish.json` schema 2 has exact keys `schema,status,approval_revision,approval_digest,branch,commit,tree,url,remote_state`; status is `published`, commit/tree equal payload, and remote state is `published`.

Exact byte binding means insignificant JSON whitespace changes are significant. Package is parsed strictly for semantics, but its original bytes are separately bound. Body bytes must be nonempty, decode as strict UTF-8, and contain no NUL; the exact bytes are passed on stdin without newline conversion. Title and commit message are both semantic payload fields and indirectly package-byte-bound; title rejects CR/LF/NUL, commit rejects NUL and is normalized only by the explicitly bound one-LF commit construction.

Public API in `prp_publication_binding.py`:

```python
class PublicationReason(str, Enum): ...  # exact values in §9
@dataclass(frozen=True) class PublicationResult: ok: bool; reason: PublicationReason|None; detail: str; value: object|None = None
@dataclass(frozen=True) class SealedTransaction: manifest: Mapping[str, object]; directory: Path

def atomic_write(path: Path, data: bytes, *, mode: int = 0o600, overwrite: bool = True) -> None: ...
def discover_repository(cwd: Path) -> RepositoryIdentity: ...
def build_sealed_transaction(identity: RepositoryIdentity, artifacts_dir: Path, package_bytes: bytes, body_bytes: bytes, allowed_paths: Sequence[str]) -> SealedTransaction: ...
def validate_sealed_transaction(identity: RepositoryIdentity, artifacts_dir: Path, manifest_bytes: bytes, package_bytes: bytes, body_bytes: bytes) -> PublicationResult: ...
def publish_sealed_transaction(identity: RepositoryIdentity, artifacts_dir: Path, manifest: Mapping[str, object]) -> PublicationResult: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

No API accepts `approved=True`, approval responses, completed nodes, arbitrary commands, or an artifact-selected repository root.

## 7. Atomic authoritative artifact writes

Migrate together every deterministic workflow write of `baseline.json`, `test-results.json`, `regression.json`, `review-aggregate.json`, `approval-manifest.json`, and `publish.json`, plus sealed package/body/transaction metadata. Model-command artifacts remain Archon-owned and cannot be made atomic here; every deterministic consumer still fails closed on malformed/missing command artifacts. This mixed boundary is explicit.

`atomic_write` creates a random same-directory regular temp with `O_CREAT|O_EXCL|O_NOFOLLOW` where available, mode 0600, writes all bytes, flushes and `fsync`s the file, applies requested permissions, closes, then `os.replace`s. It opens and verifies the parent directory before/after and best-effort fsyncs the parent: POSIX directory `fsync` errors `EINVAL/ENOTSUP/EBADF` are recorded as advisory; other errors fail. Windows uses same-volume `os.replace`; file flush uses `fsync`; directory fsync is best effort and unsupported errors are advisory. Existing destination symlinks and non-regular files are rejected. On failure before replace, destination remains old/missing and temp cleanup is best effort; crash leftovers match `.<name>.<random>.tmp`, are never read as authority, and are cleaned only while holding the lock. Readers open once, require regular/non-symlink, cap size at 10 MiB, and parse those captured bytes.

## 8. Package state/check table

Checks are ordered; the first failure is stable. Existing earlier package messages remain through row 6 where WF1 requires them, then typed reasons apply.

| # | state/check | stable reason |
|---:|---|---|
|1|strictly read package, preflight, baseline, regression and body|`artifact_invalid`|
|2|trusted repository discovery equals baseline identity|`repository_identity_mismatch`|
|3|live HEAD/branch equal baseline|`baseline_revision_mismatch`|
|4|ancestor and existing package schema/body/title/commit checks|`package_invalid`|
|5|changed paths nonempty, package-equal, and allowed|`changed_paths_mismatch`|
|6|WF1 regression changed files/digest still match current legacy state|`regression_state_mismatch`|
|7|exclusive lock acquired; no active/stale-unproven transaction|`publication_locked`|
|8|temporary index/tree build and allowed Git modes/paths|`prospective_tree_invalid`|
|9|tree-derived changed entries equal package paths|`git_object_state_mismatch`|
|10|identity/timestamp/message valid; exact commit verifies|`prospective_commit_invalid`|
|11|object inventory verifies and sealed rename succeeds|`transaction_seal_failed`|
|12|reread package/body and identity; all payload digests unchanged before manifest replace|`package_changed_during_seal`|

`package-gate` atomically writes the manifest only after row 12 and prints the revision, digest, tree, commit, title and body digest. It performs no main-repository ref/object or remote publication side effect.

## 9. Publish state/check and side-effect table

| # | state/check | stable reason / transition |
|---:|---|---|
|1|acquire exclusive lock; strictly read one copy of package/body/manifest|`publication_locked` / `validating`|
|2|schema, exact sealed names/path, revision and recomputed approval digest|`approval_binding_invalid`|
|3|rediscovered identity plus canonical `origin` repository slug and `gh repo view --repo <slug> --json nameWithOwner,defaultBranchRef` equal payload repository/base|`repository_identity_mismatch`|
|4|local ref is baseline or expected; classify remote with `git ls-remote --exit-code --refs origin <branch_ref>` as absent/baseline/expected/other|`local_ref_diverged` / `remote_diverged`|
|5|sealed package/body exact byte digests plus semantic title/message/branch match; live command artifacts are not publish inputs|`approved_content_changed`|
|6|sealed directory and exact files are regular/confined; object inventory, tree and commit rehash/parse exactly|`sealed_transaction_invalid`|
|7|working tree may differ; verify baseline→expected fast-forward without rebuilding objects|`commit_transition_invalid`|
|8|repeat rows 2–7 using already-open sealed bytes and fresh identity/ref/remote/PR observations immediately before install|`pre_side_effect_revalidation_failed`|
|9|install exact objects, then compare-and-swap branch ref when local is baseline; exact local is a no-op|`object_install_failed` / `local_ref_update_failed`; state `committed`|
|10|push only when remote is absent/baseline; query remote and require expected|`push_failed` / `remote_verification_failed`; state `pushed`|
|11|qualified PR query; exact resumes; if none, final-read and digest-check body into `approved_body_bytes`, create via `--body-file -` with those exact stdin bytes, then query and byte-verify the unique open PR; conflict/ambiguous/body mismatch blocks|`approved_content_changed` / `remote_pr_conflict` / `remote_pr_ambiguous` / `pr_create_failed` / `remote_pr_body_mismatch`|
|12|validate repository-qualified URL and atomically write publish schema 2|`publication_result_invalid`; state `pr_created` then `published`|

Rows 1–8 have zero main-repository object/ref, push, or PR effects. Remote absence is not encoded as a fake OID. The only lease argv are: absent ref creation → `git push --porcelain --set-upstream origin <expected_commit>:<branch_ref> --force-with-lease=<branch_ref>:` (explicit empty expected value after the final colon); baseline remote → the same argv with `--force-with-lease=<branch_ref>:<baseline_commit>`; expected remote → no push, verify only. If the supported Git cannot preserve the trailing-colon argument, absent creation is disallowed with `remote_creation_unsupported`; an unleased create is forbidden. Never use the baseline lease for absent or expected remote state.

The normative classifier observes local `L∈{baseline,expected,other}`, remote `R∈{absent,baseline,expected,other}`, and repository-qualified open PR result `P∈{none,exact,conflict,body_mismatch,ambiguous}`. Query with `gh pr list --repo <repository_slug> --state open --head <head_owner>:<short_branch> --base <pr_base_branch> --json number,url,headRefName,headRefOid,headRepositoryOwner,headRepository,baseRefName,title,body`; require exactly zero or one result. Encode the returned JSON `body` string as strict UTF-8 (reject lone surrogates) and compare those bytes exactly with the approved body snapshot and its `body_bytes_digest`. `exact` requires exactly one result and exact repository slug, head owner/repository, head ref, head SHA `expected_commit`, base, title, body bytes, and body digest. If every non-body field matches but body bytes/digest differ, classify `body_mismatch`; another one-result mismatch is `conflict`; multiple, malformed, or indeterminate results are `ambiguous`.

The only create pseudocode is:

```python
approved_body_bytes = read_confined_regular_bytes(sealed_body_path, max_bytes=10 * 1024 * 1024)
approved_body_bytes.decode("utf-8", errors="strict")
if not approved_body_bytes or b"\0" in approved_body_bytes or body_digest(approved_body_bytes) != payload["body_bytes_digest"]:
    return fail(PublicationReason.APPROVED_CONTENT_CHANGED)
create = subprocess.run(
    ["gh", "pr", "create", "--repo", repository_slug, "--base", pr_base_branch,
     "--head", f"{head_owner}:{short_branch}", "--title", approved_title,
     "--body-file", "-"],
    input=approved_body_bytes, shell=False, check=False, capture_output=True,
)
# Whether create reports success, timeout, transport failure, or an otherwise ambiguous result,
# discard its body/URL as authority and run the repository-qualified JSON query above.
```

After every create attempt, including a nominal success, and after every ambiguous outcome, the repository-qualified query is mandatory. Exactly one `exact` result transitions to `pr_created`; `none` after an unambiguously failed attempt permits at most the one retry described below; `body_mismatch` fails stably as `remote_pr_body_mismatch`; `conflict`/`ambiguous` retain their stable reasons. A body mismatch is a reconciliation-required terminal state: persist the observed PR number/URL and reason, report it, and do not edit/close/delete the PR or mutate any remote state automatically.

| Local | Remote | PR | persisted state; exactly one action/reason |
|---|---|---|---|
|baseline|absent|none|`validated`; install/CAS, leased-create push, create PR|
|baseline|baseline|none|`validated`; install/CAS, baseline-lease push, create PR|
|expected|absent|none|`committed`; leased-create push, create PR|
|expected|baseline|none|`committed`; baseline-lease push, create PR|
|expected|expected|none|`pushed`; create PR|
|expected|expected|exact|`pr_created`; verify then write publication|
|baseline|expected|none|`remote_ahead_local`; install/CAS then create PR, no push|
|baseline|expected|exact|`remote_ahead_local`; install/CAS then verify/write, no push|
|baseline|absent or baseline|exact|reject `pr_state_impossible`|
|expected|absent or baseline|exact|reject `pr_state_impossible`|
|baseline or expected|any non-diverged|conflict|reject `remote_pr_conflict`|
|baseline or expected|any non-diverged|body_mismatch|reject `remote_pr_body_mismatch`; manual reconciliation only|
|baseline or expected|any non-diverged|ambiguous|reject `remote_pr_ambiguous`|
|other|any|any|reject `local_ref_diverged`|
|baseline or expected|other|any|reject `remote_diverged`|

These rows are exhaustive. Persist after each successful CAS, remote verification, PR verification/creation, and final write in atomic `publication-state.json`, exact keys `schema,approval_digest,state,local_oid,remote_kind,remote_oid,pr_kind,pr_url,last_reason`. It is diagnostic only and must agree with fresh observations. A timed-out/transport-failed push records `push_outcome_ambiguous`, then queries remote: expected resumes; absent/baseline permits one retry with its corresponding lease; other rejects. Every create outcome is reconciled by the qualified query: exact resumes; none after an unambiguously failed create or ambiguous outcome permits one retry using the same in-memory `approved_body_bytes` (never a pathname reread); conflict/ambiguous rejects; body mismatch records `remote_pr_body_mismatch` and requires manual reconciliation. No automatic rollback deletes or mutates a commit, branch, or PR.

## 10. Locking and platform boundaries

One repository-scoped lock name is SHA-256 of trusted common-dir plus branch ref. Store it in `$ARTIFACTS_DIR/locks/<name>.lock`, opened without following symlinks. Hold it for package transaction creation and separately for the entire publish validation/side-effect sequence. The lock file contains schema, pid, process-start marker, hostname, run id and creation time.

POSIX uses `fcntl.flock(LOCK_EX|LOCK_NB)`; Windows uses `msvcrt.locking` on a one-byte file. A held lock always wins regardless of age. An unlocked stale record may be replaced only after process liveness/start-marker disproves ownership; otherwise fail `publication_locked`. Threads in-process also use a keyed mutex. Lock scope serializes same common-dir/branch across runs but does not pretend to control external Git users; compare-and-swap refs and push lease close that boundary. Cleanup of temporary indexes, object dirs and atomic-write leftovers occurs only under this lock and never removes a sealed revision referenced by a manifest.

Portable Python path APIs cannot guarantee handle-relative, no-follow traversal and rename on every supported Windows/POSIX filesystem. Therefore concurrent hostile filesystem mutation (component replacement/mount/reparse changes between checks and use) is explicitly outside the WF1B threat model. Implementations still lstat every existing artifact/object path component immediately before open/rename, reject static symlinks on POSIX and symlinks/junctions/reparse points on Windows, use `O_NOFOLLOW` where available, require regular files/directories, confine resolved paths, and recheck identity/metadata after open. This detects static attacks and many races but is not claimed race-proof against a hostile same-host filesystem administrator. Git ref races and ordinary concurrent worktree edits remain in scope and are closed by object sealing, CAS, and leases.

## 11. Approval presentation and compatibility

Keep `plan-approval` unchanged. Keep node id `final-approval`, type `approval`, `capture_response: true`, and dependency on `package-gate`. `package-gate` prints exactly one compact canonical-JSON object as its node output with keys `approval_revision,approval_digest,repository_slug,pr_base_branch,branch_ref,baseline_commit,tree_oid,commit_oid,title,package_bytes_digest,body_bytes_digest`; this output is computed from the just-written manifest and equality-checked against it. Archon YAML uses the already-supported node-output interpolation form as a standalone scalar line, including the hyphenated node id:

```yaml
  - id: final-approval
    approval:
      message: |
        FINAL PUBLICATION APPROVAL (schema 2)
        Approve exactly the repository, base, head, content and commit encoded below.
        A manifest alone is not approval. Rejection abandons this run.
        $package-gate.output
      capture_response: true
    depends_on: [package-gate]
```

The message has no shell interpolation and no separately typed mutable values: the single package-gate JSON line visibly binds full repository identity, PR base branch, head ref, revision/digest, baseline/tree/commit, title and package/body digests. Workflow tests YAML-parse this exact block and assert `$package-gate.output` is preserved. Approval rejection still abandons the run; no unsafe approval cycle is introduced.

This is a fail-closed schema cutover. Schema-1 baseline/approval/publication manifests and WF1's old publication digest are rejected by the hardened package/publish nodes; there is no implicit upgrade or old-approval reuse. Existing unfinished runs must be abandoned and restarted from `worktree-guard`. Completed schema-1 publications remain historical evidence and are not rewritten.

## 12. Named RED tests and expected failures

Add tests first and run the focused argv in §14 before the new module/workflow wiring. Expected RED is collection error `ModuleNotFoundError: No module named 'prp_publication_binding'`; if WF1 has already added a stub, expected RED is named assertion failures, never fabricated output.

Publication tests:

- `test_binding_vector_binds_exact_package_and_body_bytes_title_message_and_revision`
- `test_manifest_presence_never_supplies_archon_approval`
- `test_trusted_discovery_rejects_mutated_root_common_dir_and_replaced_gitdir`
- `test_temp_index_tree_records_untracked_symlink_target_not_followed_bytes`
- `test_tree_digest_binds_mode_delete_rename_and_path_object_semantics`
- `test_existing_index_is_untouched_and_contamination_cannot_enter_tree`
- `test_concurrent_edit_after_write_tree_cannot_change_approved_commit`
- `test_publish_never_invokes_add_commit_or_hooks`
- `test_package_order_and_stable_reasons_have_zero_publication_side_effects`
- `test_publish_revalidates_all_binding_inputs_immediately_before_object_install`
- `test_mutation_of_each_bound_field_after_approval_fails_before_side_effect`
- `test_ref_compare_and_swap_and_push_lease_reject_concurrent_writer`
- `test_retry_after_commit_or_push_is_idempotent_and_never_duplicates_pr`
- `test_pr_create_uses_literal_body_file_dash_shell_false_and_exact_snapshot_stdin`
- `test_body_path_swap_after_final_snapshot_cannot_change_created_pr_body`
- `test_every_create_outcome_requeries_and_compares_unique_pr_body_utf8_bytes`
- `test_post_create_body_mismatch_is_stable_and_never_mutates_remote_state`
- `test_atomic_write_interruption_preserves_old_file_and_ignores_temp_leftover`
- `test_atomic_write_posix_and_windows_capability_branches_and_parent_fsync`
- `test_lock_serializes_same_repo_branch_and_does_not_break_live_owner`
- `test_schema1_approval_is_rejected_and_cannot_be_upgraded`

Workflow tests:

- `test_exact_22_nodes_two_approvals_and_no_auto_merge_remain`
- `test_final_approval_renders_revision_digest_tree_commit_and_content_digests`
- `test_publish_validation_precedes_first_object_ref_push_or_gh_side_effect`
- `test_all_deterministic_authoritative_writers_use_atomic_write`
- `test_no_product_command_or_unallowlisted_file_changes`

Minimal GREEN: implement canonical bytes/digest vectors and strict schemas; trusted discovery; atomic writer/lock; real-Git temporary index/object transaction; ordered validators; then wire package, approval message, and publish in that order. Do not refactor unrelated WF1 policies.

## 13. Acceptance invariants

Acceptance requires: exact package/body byte mutations, title/message changes, baseline/root substitutions, mode/symlink/path changes, object replacement, and approval-revision replay all fail before publication effects; the installed commit OID and tree OID equal those shown for approval; the body final-read is valid UTF-8/no-NUL and immutable in memory; `gh pr create` uses literal `--body-file -`, `shell=False`, and exactly that snapshot on stdin without reopening a pathname; every create/ambiguous outcome re-queries the repository-qualified unique open PR and verifies exact base/head/SHA/title/body UTF-8 bytes and digest; mismatch has a stable reconciliation-required failure and performs no automatic remote mutation; the PR title/body equal sealed approved bytes; user's index is byte-identical; no hook executes; branch update uses compare-and-swap; push uses a lease; retries do not duplicate PRs; atomic readers never observe a partial deterministic artifact; both approval nodes remain; no merge command/API exists.

Secret scanning remains separately unimplemented and must be called out in review evidence.

## 14. Exact verification argv

After WF1 lands, run from a clean linked worktree with no inherited Git identity/config surprises (`HOME` points at an empty temporary directory; fixtures set local identity). Record actual outputs:

```bash
uv run --project .claude/scripts --extra dev pytest .archon/scripts/test_prp_publication_binding.py .archon/scripts/test_prp_artifact_contracts.py .archon/scripts/test_implement_prp_workflow.py -q
uv run --project .claude/scripts --extra dev ruff check .archon/scripts/prp_publication_binding.py .archon/scripts/prp_artifact_contracts.py .archon/scripts/test_prp_publication_binding.py .archon/scripts/test_prp_artifact_contracts.py .archon/scripts/test_implement_prp_workflow.py
python -c "from pathlib import Path; import yaml; d=yaml.safe_load(Path('.archon/workflows/implement-prp.yaml').read_text(encoding='utf-8')); assert len(d['nodes'])==22; assert sum('approval' in n for n in d['nodes'])==2; print([n['id'] for n in d['nodes']])"
archon workflow list
git diff --check
git status --short
git diff -- .archon/workflows/implement-prp.yaml .archon/scripts docs/prps
```

Focused RED/GREEN is the first pytest argv. Regression is the same full three-file pytest argv because the publication wiring changes the shared contract and workflow tests; Ruff is mandatory. Planning validation now must YAML-parse, discover `implement-prp` with `errorCount: 0`, validate relative Markdown links, recompute review-manifest hashes, and inspect the diff. This PRP remains not ready until an independent reviewer binds a passing verdict to the current hashes and WF1 acceptance is confirmed.

## 15. Exact backout

Stop new hardened runs and retain all artifacts/sealed transactions. Revert only the WF1B hunks in `.archon/workflows/implement-prp.yaml` and `.archon/scripts/prp_artifact_contracts.py`; delete `prp_publication_binding.py` and its test; retain this PRP/review history as planning evidence. Restore the previously documented **manual publication process**, not schema-1 automated approval reuse. Abandon every in-flight schema-2 run. If a run reached `committed` or `pushed`, report its exact commit/remote state for manual reconciliation; do not reset, force-push, close PRs, delete objects, or infer rollback success. Never back out by weakening a digest, accepting an old manifest, bypassing either approval node, or enabling auto-merge.
