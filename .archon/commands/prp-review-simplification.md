---
description: Independently review changed-code simplicity.
argument-hint: (reads workflow artifacts)
---
# Simplification Review
As a fresh non-editing reviewer, inspect only the changed surface for unnecessary abstraction, duplication, speculative flexibility, dead code, dependencies, and avoidable churn. Do not demand scope expansion or style-only work. Write authoritative `$ARTIFACTS_DIR/review-simplification.json` as `{"schema":1,"verdict":"pass|block","findings":[{"severity":"blocking|advisory","path":"...","evidence":"...","remedy":"..."}],"evidence":[...]}`. Block only material correctness/maintainability complexity. Output exactly that JSON.
