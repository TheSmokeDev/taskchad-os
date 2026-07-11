# Canonical Epic Index

**Status:** staged canonical planning source; no repository-conformance claim
**Normative source:** [Polish Architecture Specification](../specs/taskchad-os-polish-architecture-spec.md)

This is a roadmap, not proof that every slice is executable. Only rows explicitly marked **implementation-ready** may enter `implement-prp`; all drafts require source-specific hardening and WF2 review.

## Unambiguous workflow states

`draft` → `reviewed (WF2 manifest)` → `implementation-ready` → `implemented/open PR` → `merged` → `epic released`

- **draft:** intent/candidate scope only; no implementation authorization.
- **reviewed:** an immutable WF2 manifest records independent source/anchor/path/symbol/API/test/security/backout review, findings, revision, reviewer, and digest. Review does not authorize implementation.
- **implementation-ready:** all blocking findings closed; exact files/symbols and focused/regression argv proven in a clean environment; manifest digest binds the PRP revision.
- **implemented/open PR:** code exists in an independently reviewable PR; not merged or released.
- **merged:** slice merged with evidence; not automatically an epic release.
- **epic released:** every applicable slice/POL anchor and dependency has release evidence approved through WF3.

WF2 bootstraps before its own implementation through documented independent human/adversarial review with the same manifest fields and digest. After WF2 ships, its deterministic workflow is required. WF2 may never self-certify its initial implementation.

## Dependency DAG

```text
WF1 → WF2 → reviewed PRP → implementation-ready PRP → implement-prp
WF1/WF2 → WF3; WF1-WF3 → WF4
E01A → E01A2/E01A3 → E01B → E01C → E01D → E01E
E01 → E02 → E03
E03A → E03B → E03J → E03K
E03A/E03B → E03C → E03D → E03E → E03F/E03G/E03H → E03I
E03/E04 → E07; E01/E02/E05 → E06; E02/E05 → E08
E02/E03/E05 → E09; E01-E09 + WF3 → E10
```

## Workflow foundation

| ID | Prerequisite | Status |
|---|---|---|
| [WF1](PRP-WF1-workflow-artifact-contracts.md) | none | draft — review found unresolved schema/state/wiring blockers |
| [WF2](PRP-WF2-review-prp-workflow.md) | WF1 | draft — requires WF2 review and source-specific hardening |
| [WF3](PRP-WF3-release-epic-workflow.md) | WF1, WF2 | draft — requires WF2 review and source-specific hardening |
| [WF4](PRP-WF4-workflow-rail-self-tests.md) | WF1-WF3 | draft — requires WF2 review and source-specific hardening |

## Epic 1 — Trustworthy Self-Amendment

| ID | Ownership | Prerequisite | Status |
|---|---|---|---|
| [E01A](PRP-E01A-audit-fail-closed.md) | durable mutation audit record/store | none | draft — persistence/state/API blockers unresolved |
| [E01A2](PRP-E01A2-amendment-audit-fail-closed.md) | amendment integration plus **POL-AM-007 hard autonomous constitutional-mutation denial** | E01A | draft — caller scope/transaction design unresolved |
| [E01A3](PRP-E01A3-skill-promotion-audit-fail-closed.md) | skill-promotion fail-closed integration | E01A | draft — caller/policy/result/reconciliation design unresolved |
| [E01B](PRP-E01B-evidence-binding.md) | POL-AM-001 evidence binding | E01A2 | draft — requires WF2 review and source-specific hardening |
| [E01C](PRP-E01C-safe-apply.md) | atomic policy-gated apply | E01A2, E01B | draft — requires WF2 review and source-specific hardening |
| [E01D](PRP-E01D-rollback-recovery.md) | rollback/crash recovery | E01A2, E01C | draft — requires WF2 review and source-specific hardening |
| [E01E](PRP-E01E-operator-surfaces.md) | operator surfaces | E01C, E01D | draft — requires WF2 review and source-specific hardening |

POL-AM-007 belongs specifically to E01A2 as a **hard denial of autonomous mutation** for core constitutional identity. Audit preparation, identity context, confinement, or CAS checks do not waive this prohibition. The hardened PRP must identify the trusted non-autonomous governance boundary separately and name negative tests proving autonomous callers cannot publish these targets.

## Epic 2 — Unified Capability and Policy Kernel

All Epic 2 PRPs are **draft — requires WF2 review and source-specific hardening**: [E02A](PRP-E02A-capability-descriptor-schema.md) → [E02B](PRP-E02B-capability-inventory-adapters.md), and [E02A](PRP-E02A-capability-descriptor-schema.md) → [E02C](PRP-E02C-policy-decision-api.md) → [E02D](PRP-E02D-approval-grants.md) → [E02E](PRP-E02E-authorized-invocation.md) → [E02F](PRP-E02F-leases-gateway-cutover.md).

## Epic 3 — Persona Turn Unification and session evidence

Every Epic 3 row is **draft — requires WF2 review and source-specific hardening**.

| ID | Ownership | Prerequisite |
|---|---|---|
| [E03A](PRP-E03A-request-context.md) | immutable request context | E02C |
| [E03B](PRP-E03B-session-event-store.md) | POL-SE-001/002/004/005 append-only store only | E03A |
| [E03J](PRP-E03J-compaction-artifact.md) | **POL-SE-003 `CompactionArtifact`** | E03A, E03B |
| [E03K](PRP-E03K-retention-redaction-continuity.md) | **POL-SE-006 retention/redaction/deletion/tombstone/hash continuity** | E03B, E03J |
| [E03C](PRP-E03C-session-manifest.md) | session manifest | E02A, E02C, E03A, E03B |
| [E03D](PRP-E03D-turn-context-recall.md) | turn context/recall | E03A, E03C |
| [E03E](PRP-E03E-persona-turn-service.md) | persona turn authority | E02E, E03B-E03D |
| [E03F](PRP-E03F-main-web-adapters.md) / [E03G](PRP-E03G-discord-adapter.md) / [E03H](PRP-E03H-cabinet-adapter.md) | ingress adapters | E03E |
| [E03I](PRP-E03I-scheduled-turns-cutover.md) | scheduled turns/cutover | E03F-E03H, E03J, E03K |

E03J/E03K are deliberately separate bounded PRPs; neither is crammed into E03B.

## Epics 4-10

E04 Typed Configuration and Doctor; E05 Durable Homie Jobs; E06 Skill Repository and Curator; E07 Transactional Vault and Recall Health; E08 Operating Room Timeline; E09 Adapter Capability Contracts; and E10 Generated Documentation Traceability remain **decomposition-only drafts**. They require bounded PRPs, exact ownership, and WF2 review before implementation.

## Draft preflight rule

Every remaining draft's path list is candidate scope, not an allowlist. Before status can advance, WF2 must enumerate exact existing/intended-new files and symbols, verify current behavior, define typed signatures and state/reason tables, name PRP-specific RED tests with expected failures, specify GREEN/concurrency/crash/backout steps, and prove literal focused and regression argv in a clean environment. Generic `pytest tests -q`, generic directory scopes, boilerplate acceptance text, and model assertions do not establish readiness.

The legacy PRP-001/A-D rollback pilot and open PR #12 map into E01C-E01E but do not complete Epic 1 or change these statuses.
