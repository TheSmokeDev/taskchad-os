# PRP-E03J-compaction-artifact: Compaction Artifact Contract

**Status:** draft — requires WF2 review and source-specific hardening
**Primary POL anchor:** POL-SE-003
**Depends on:** E03A request context, E03B session event store

## Bounded intent
Own `CompactionArtifact` as a first-class derived artifact rather than enlarging the append-only event-store slice. It must bind source event range/digest, summary payload, model/config identity, tenant/persona/session/correlation, creation time, and supersession lineage. Raw events remain authoritative; compaction never rewrites or deletes them.

## Candidate scope (not an implementation allowlist)
Candidate source seams include `.claude/chat/cognition/observability.py` (`CompactionEvent`) and session/context persistence discovered by E03B. WF2 reconnaissance must enumerate exact existing and intended-new files and symbols before this may become reviewed.

Required negative proof includes wrong tenant/persona/session, missing event range, digest mismatch, overlapping concurrent compactions, stale source head, corrupt/truncated artifact, retry idempotency, and crash before/after atomic publication. Every invalid artifact is ignored/denied without hiding source events.

## Readiness preflight
WF2 must confirm the real storage authority and typed API, exact state/reason table, exact RED test names and expected initial failures, lock/CAS and crash recovery, and exact backout. Focused and regression argv must be executed successfully in a clean environment and recorded in the WF2 manifest; broad suite placeholders are not evidence and this draft is not executable.
