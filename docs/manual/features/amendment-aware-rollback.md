# Amendment-Aware Rollback

Status: **IN DEVELOPMENT** — PRP-001A ready for bounded implementation; PRP-001B-D planned
Owner: amendment domain and operator surfaces
Last updated: 2026-07-10

## What It Does

Amendment-aware rollback is the planned conflict-safe compensating operation
for one previously applied autonomous amendment. Its target behavior is to
restore the exact bytes in that amendment's recorded pre-apply snapshot only
when the current target still matches the recorded post-apply hash. It is not
whole-profile backup restore, generic file undo, or a shipped operator feature.

The proposal ledger remains lifecycle authority and the target Markdown file
remains content authority. The architecture contract is
`POL-AM-001` through `POL-AM-007`, especially conflict checking, durable intent,
audit, atomic restoration, and deterministic crash recovery.

## Current Status And Limitations

- **PRP-001A is ready for bounded implementation, not proven or merged.**
  It targets the domain service, exact-byte restore, recovery, and cooperative
  locks plus narrowly scoped tests.
- PRP-001B (local CLI), PRP-001C (authenticated Python API), and PRP-001D
  (Hono proxy and Audit UI) are planned and dependency-blocked. They must not
  be described as available.
- There is currently no rollback CLI command, API route, dashboard action, or
  chat/channel command from this program.
- The target lock coordinates amendment-domain writers only. External editors
  or code that ignores the lock can still race; immediate re-read/hash checks
  reduce but cannot universally eliminate that TOCTOU window.
- Exact-byte guarantees begin with the recorded snapshot. The pilot does not
  reconstruct line endings already normalized by legacy text-based apply.
- There is no force path. Whole-profile restore remains a separate capability.

## Operator Entry Points

- Chat/Telegram/Discord: none planned in PRP-001; explicitly out of scope.
- CLI: **future PRP-001B** — planned `thehomie amendments list` and
  `thehomie amendments rollback ...`; not currently available.
- API: **future PRP-001C** — planned authenticated admin list and rollback
  routes; not currently available.
- Dashboard: **future PRP-001D** — planned bounded Audit view and confirmation
  flow through a thin Hono proxy; not currently available.
- Domain API: **PRP-001A target** — planned read-only listing and rollback
  functions; implementation is not yet proven or merged.

Do not attempt a manual snapshot copy and do not infer that the legacy
self-amendment references to “rollback” provide this lifecycle.

## Target State Machine

```text
applied --rollback request--> rollback_pending --durable finalize--> rolled_back
   |                              |
   +-- invalid/conflict: refuse   +-- restart: reconcile from ledger + hashes
```

A fresh target operation is intended to:

1. Validate proposal ID, actor, reason, unique ledger row, allowed target, and
   confined snapshot without mutating state.
2. Require proposal state `applied`, snapshot hash equal to `before_hash`, and
   current target hash equal to `after_hash`.
3. Under ledger-then-target cooperative locks, durably create and verify a
   rescue snapshot of the current target.
4. Durably record `rollback_pending` before changing the target.
5. Recheck the current hash, atomically restore snapshot bytes, and verify the
   restored hash.
6. Durably finalize `rolled_back` with actor, reason, times, hashes, rescue
   reference, and audit state. Success is not returned before this final write.

Recovery is state-aware: pending plus the post-apply target resumes restore;
pending plus the already-restored target finalizes; an unexpected hash
conflicts. An already completed rollback is idempotent only while the target
still matches the restored hash.

## Safety And Conflict Semantics

- **Compare before restore:** current target bytes must hash to the recorded
  post-apply value. Drift returns `target_hash_conflict` without restoration.
- **No force:** PRP-001 deliberately provides no override. The broader
  architecture allows only a separately authorized forced path that preserves
  displaced state, but this program does not implement one.
- **Confined paths:** neither caller nor remote surface chooses target or
  snapshot paths. Target and snapshot must resolve inside their allowed roots
  and be regular, non-symlink files.
- **Durable intent first:** rescue and `rollback_pending` must be durable before
  target replacement. Audit/ledger preparation failure prevents mutation.
- **Exact-byte restore:** snapshot, rescue, hashing, and restoration operate on
  bytes; no decode/re-encode occurs in rollback.
- **Cooperative serialization:** lock order is always ledger then target for
  apply, collapse, and rollback participants. Lock timeout is a failure, not
  optimistic success.
- **Fail closed:** missing/unreadable files, duplicate proposal IDs, hash
  mismatch, invalid state, or durability failure never report success.
- **No sensitive projection:** future remote/UI surfaces must not return
  snapshot content, unrestricted absolute paths, tokens, or supplied actor
  identities.

## Future Surface Contract

### Local CLI — PRP-001B

The planned local command requires explicit actor and reason plus confirmation,
has no `--force`, and maps only completed/reconciled/already-completed outcomes
to exit 0. It is an adapter over the single domain service, not a second
implementation.

### Authenticated Python API — PRP-001C

The planned routes are admin-only. POST accepts a reason but never an actor;
actor identity is derived from the authenticated principal. Unauthorized calls
must reach neither listing mutation nor rollback. Domain conflicts remain HTTP
conflicts and unknown outcomes fail closed with sanitized errors.

### Dashboard — PRP-001D

The planned Hono layer is a thin authenticated proxy. The Audit UI lists safe
metadata, disables ineligible actions, requires a non-empty reason, prevents
duplicate submission, and refetches after success. It must never optimistically
mark a rollback complete or render snapshot contents/paths.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Normative requirements | `docs/specs/taskchad-os-polish-architecture-spec.md` §§6.7, 8, 11, 13 |
| Epic/lifecycle | `docs/prps/PRP-001-amendment-aware-rollback.md` |
| Domain pilot | `docs/prps/PRP-001A-domain-rollback-service.md` |
| Planned CLI | `docs/prps/PRP-001B-local-cli.md` |
| Planned API | `docs/prps/PRP-001C-authenticated-python-api.md` |
| Planned dashboard | `docs/prps/PRP-001D-dashboard-rollback-ui.md` |
| Current legacy amendment anchor | `.claude/chat/cognition/amendments.py` |
| Planned domain/test anchors | `.claude/chat/cognition/amendment_rollback.py`, `.claude/scripts/tests/test_amendment_rollback.py` |

## How To Run It

There is no supported operator command to run yet. PRP-001A is intended to run
through the bounded polish workflow described in
[Polish Architecture And Execution Program](polish-architecture-execution-program.md).
Wait for independently reviewed and merged slices before using any proposed
surface.

## How To Test It

These are PRP acceptance commands, **not current green-proof claims**. PRP-001A
must record their actual outcomes before it can be called proven:

```bash
cd .claude/scripts
uv run --extra dev pytest tests/test_amendment_rollback.py -q
uv run --extra dev pytest tests/test_cognition_amendments.py tests/test_amendment_pipeline_idempotence.py -q
uv run --extra dev ruff check ../chat/cognition/amendments.py ../chat/cognition/amendment_rollback.py tests/test_amendment_rollback.py
uv run --extra dev pytest tests -q
```

Later slices add their own CLI, API, server, and web tests. Integrated completion
requires all four PRPs independently passing plus an authenticated
admin-to-proxy-to-Python-to-single-domain-service proof.

## Latest Live Proof

- Date: 2026-07-10
- Surface: amendment-aware rollback product behavior
- Result: **none**. No rollback behavior has been exercised or proven live, and
  no rollback implementation, test pass, CLI/API/dashboard availability, or
  merged product behavior is claimed.
- Declared/source evidence: foundation PR #9 merged the canonical requirements,
  bounded PRP-001 A-D contracts, and execution workflow. This is planning and
  source evidence only; it is not live product proof.

## Source/Test/Proof Traceability

| Target claim | Requirement/source | Required evidence before status can advance |
|---|---|---|
| Conflict-safe exact-byte domain rollback | `POL-AM-004..006`; PRP-001A §§3-7 | Focused byte/path/state/failure/recovery/concurrency tests, amendment regressions, ruff, and full Python suite |
| Read-only, non-healing listing | PRP-001A §4 | Tests proving absent/malformed ledgers are not created or changed, ordering/filtering, duplicate visibility, and eligibility reasons |
| Cooperative apply/rollback serialization | PRP-001A §6 | Thread/process contention and apply/collapse lock-order regressions, with the uncooperative-writer limitation retained |
| Local operator command | PRP-001B | CLI registration, confirmation, JSON purity, complete outcome mapping, and existing CLI regression tests; planned only |
| Authenticated admin API | PRP-001C | Route-policy, identity derivation, 401/403 zero-call, complete HTTP mapping, and leakage tests; planned only |
| Non-optimistic Audit UI | PRP-001D | Proxy and UI tests for auth, exact forwarding, confirmation, duplicate prevention, conflict, refetch, and data minimization; planned only |
| Integrated epic completion | PRP-001 epic §28 | All slices merged plus end-to-end authenticated flow and conflict proof; not yet available |

## Public Export Status

Public-framework safe as a status and target-behavior manual. It contains no
snapshot content, live profile data, credentials, run-local artifact paths, or
private handoff details.

## Next Slices

1. Finish, review, and merge PRP-001A only after every required acceptance gate
   has real evidence.
2. Implement PRP-001B and PRP-001C only after A is available; keep both as thin
   adapters over one domain authority.
3. Implement PRP-001D only after C, then run the integrated end-to-end proof.
4. Update this status from **IN DEVELOPMENT** only when merged behavior and
   evidence justify a narrower, honest claim.
