# PRP-001: Amendment-Aware Rollback (Epic Index)

**Status:** split; implement slices in order
**Priority:** P0 trust closure
**Source:** `docs/specs/taskchad-os-hermes-polish-assessment.md` §1.6 and §4/P0.1

## Outcome

Provide a conflict-safe compensating operation for one applied autonomous amendment. This is not whole-profile backup restore. The proposal ledger remains lifecycle authority; the target Markdown file remains content authority.

## Ordered implementation slices

| Slice | Deliverable | Dependency | Execution status |
|---|---|---|---|
| [PRP-001A](PRP-001A-domain-rollback-service.md) | Domain durability, exact-byte restore, recovery, cooperative locking | none | **Archon pilot; execute first** |
| [PRP-001B](PRP-001B-local-cli.md) | Local Click list/rollback commands | 001A | blocked on A |
| [PRP-001C](PRP-001C-authenticated-python-api.md) | Authenticated/admin Python API and route policy | 001A | blocked on A |
| [PRP-001D](PRP-001D-dashboard-rollback-ui.md) | Hono proxy and React/Preact Audit UI | 001C | blocked on C |

Do not implement B-D while piloting A. Each slice has its own tests, acceptance evidence, and backout. Chat/Telegram/Discord commands remain out of scope.

## Cross-slice contract

Lifecycle: `applied -> rollback_pending -> rolled_back`. A rollback restores the original snapshot only when the current target's exact-byte SHA-256 equals the recorded post-apply hash. There is no `force` path. The write protocol durably records intent and a rescue snapshot before replacing the target and never reports success before final ledger state is durable.

Actor ownership is deliberately split: B accepts a local explicit `--actor`; C defines the exact authenticated-principal contract and never accepts actor from request JSON; D only forwards the authenticated browser request. Remote auth, HTTP, proxy, and UI assumptions are absent from A.

## Integrated completion

The epic is complete only after all four PRPs pass independently and end-to-end proof shows: authenticated admin request -> proxy -> Python API -> the single A domain service; conflict is shown without optimistic success; no snapshot content is exposed; all focused/full repository checks pass. Documentation changes outside `docs/prps` belong to the implementing slices, not this planning split.
