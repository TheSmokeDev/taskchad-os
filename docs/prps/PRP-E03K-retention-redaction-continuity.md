# PRP-E03K-retention-redaction-continuity: Retention, Redaction, Deletion and Hash Continuity

**Status:** draft — requires WF2 review and source-specific hardening
**Primary POL anchor:** POL-SE-006
**Depends on:** E03B session event store, E03J compaction artifact

## Bounded intent
Own lifecycle controls separately from event append: explicit retention decisions, field-level redaction, authorized deletion represented by durable tombstones, and verifiable hash continuity across retained records, redacted projections, tombstones, and compaction artifacts. Deletion must not masquerade as a missing/corrupt event, and redaction must not silently change the authoritative chain.

## Candidate scope (not an implementation allowlist)
Candidate seams are the E03B store and E03J artifact contracts after they exist. No source path is currently authorized. WF2 must identify exact files, symbols, policy authority, storage format, migration boundary, and tests.

Required negative proof includes unauthorized delete/redact, cross-tenant/persona scope, expired/stale approval, tombstone omission/reordering, hash discontinuity, concurrent append versus lifecycle operation, retry, crash at every durability boundary, secret reappearance in projections/compaction, and restoration attempts that bypass a tombstone.

## Readiness preflight
WF2 must supply typed request/result/record signatures, state/reason table, cryptographic scope and canonicalization, lock/CAS ordering, recovery and exact backout. Focused and regression argv must be proven in a clean environment and recorded in the WF2 manifest before reviewed/implementation-ready. Generic full-suite commands are not proof; this draft is not executable.
