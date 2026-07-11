# PRP-WF1B-publication-binding-hardening: Publication binding and race hardening

**Status:** draft — requires independent WF2 review; not implementation-ready
**Depends on:** WF1

## 1. Bounded outcome

After WF1 proves behavior-preserving artifact validation extraction, design and implement a separately reviewed migration that binds final human publication approval to the exact package metadata, body, repository object state, and intended commit, anchors repository identity independently of mutable artifacts, closes the validation-to-staging race, and writes authoritative artifacts atomically.

This PRP is intentionally a **draft ownership boundary**, not authorization to implement. Exact compatibility/schema versions, symbols, files, platform behavior, and test argv must be established by reconnaissance and independent review after WF1 lands.

## 2. Exact security ownership

WF1B exclusively owns these changes that WF1 preserves as limitations:

1. **Package/body/title/commit binding**
   - define a versioned approval payload that cryptographically binds canonical `pr-package.json`, exact `pr-body.md` bytes, title, commit message, branch, changed-path set, repository-content digest, baseline identity, and intended resulting commit/tree identity;
   - make final approval display/reference that binding and make publish reject any post-approval mutation;
   - specify canonical serialization, domain separation, digest algorithm, encoding, and compatibility/fail-closed behavior; never infer human approval merely from manifest presence.
2. **Baseline root anchor**
   - stop treating mutable `baseline.json.root` as sole repository authority;
   - anchor repository/worktree identity from a trusted invocation context and verified Git common-dir/worktree relationship, then compare artifacts against it;
   - define behavior for moved/deleted worktrees and replaced Git directories.
3. **Symlink-aware Git-object digest**
   - replace dereferenced `Path.read_bytes()` semantics with a digest over the exact Git object representation that staging/commit will record, including mode, path, blob bytes, symlink target bytes, deletions, renames, and executable-bit changes;
   - bind the staged tree or prospective tree, not an approximation of working-tree files.
4. **Validation-to-staging race closure**
   - construct/validate a prospective index or tree in an isolated temporary index (`GIT_INDEX_FILE`) or equivalently proven mechanism;
   - verify the approved tree and commit inputs, then commit that exact tree without a mutable-working-tree `git add` gap;
   - specify concurrent edit, index contamination, hook mutation, failure, retry, and cleanup behavior.
5. **Atomic artifact writes**
   - provide one same-directory temporary-write, flush/fsync policy, permission policy, and atomic replace helper for every authoritative JSON/text artifact;
   - define Windows and POSIX semantics, crash leftovers, overwrite policy, and reader behavior;
   - migrate all relevant workflow writers together or define explicit mixed-version compatibility.

## 3. Explicit non-goal / separate decision

A repository secret scanner and evidence-redaction policy are **not owned yet**. WF1B review may create a separately bounded dependency once an approved scanner, patterns/policy, producer coverage, false-positive handling, and failure reason are selected. Truncation is not redaction. WF1B must not claim publication artifacts are disclosure-safe without that work.

No auto-merge, approval bypass, arbitrary command execution, product behavior change, or broad workflow redesign.

## 4. Required design artifacts before readiness

Independent hardening must supply:

- exact current and proposed artifact schemas plus migration/compatibility table;
- exact allowed existing/new files and symbols;
- a threat model for artifact tampering, root substitution, symlinks, concurrent edits, hooks, index mutation, crashes, retries, and partial remote side effects;
- byte-level canonicalization and digest test vectors;
- an approval-state design that leaves authorization with Archon/human capture while binding the approved digest;
- an exact prospective-tree/commit algorithm and proof that publication uses the same approved object;
- platform-specific atomic-write contract;
- stable node-local failure order and side-effect boundaries;
- exact RED fixtures, focused argv, regression argv, cleanup, rollout, and backout.

## 5. Mandatory adversarial acceptance cases

At minimum, future tests must prove zero publish side effects for mutations after package gate to:

- package JSON formatting or fields, title, commit message, body bytes, branch, baseline, path set, file bytes, file mode, symlink target, deletion/rename, or untracked content;
- mutable `baseline.json.root`, substituted worktree/common-dir, existing index contents, concurrent working-tree edits, and commit-hook mutation;
- malformed/truncated artifacts and interrupted atomic replacement.

Tests must prove the committed tree and approved tree/object digest are identical, the PR uses the approved title/body, atomic writers never expose partial authoritative files after simulated interruption, approval cannot be inferred from a manifest, and retries do not duplicate unsafe remote effects.

## 6. Dependency policy

WF1B starts only after WF1 is reviewed and implemented. WF2 depends on WF1B **when WF2 is permitted to publish or promises secure publication binding**. A strictly review-only WF2 with no commit/push/PR side effects may proceed on WF1 alone, but must state that boundary and may not claim WF1B guarantees. WF3 and any release/publication workflow must resolve whether WF1B is mandatory before readiness.

## 7. Backout constraints

Backout must disable the hardened publication path or restore the prior explicitly documented manual process; it may not silently accept old unbound approvals, weaken digest checks, reuse stale manifests, or leave temporary indexes/artifacts as authority. Preserve diagnostic evidence without publishing it.

## 8. Draft blocker

This document assigns exact security ownership but deliberately lacks source-proven symbols, schema migration, platform-tested algorithms, and executable argv. It remains draft and must not enter `implement-prp` until WF1 completion and independent WF2 review close those blockers.
