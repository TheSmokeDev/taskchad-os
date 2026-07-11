---
description: Independently review security and state-machine correctness.
argument-hint: (reads workflow artifacts)
---
# Security and State Review
As a fresh non-editing reviewer, inspect actors, trust boundaries, authorization, secrets/data, failure/rollback, retries/idempotency, concurrency, transitions, terminal states, and invariants. Block only reachable evidence-backed defects. Write authoritative `$ARTIFACTS_DIR/review-security-state.json` as `{"schema":1,"verdict":"pass|block","findings":[{"severity":"blocking|advisory","path":"...","evidence":"...","remedy":"..."}],"evidence":[...]}`. Verdict is `block` iff a blocking finding exists. Output exactly that JSON.
