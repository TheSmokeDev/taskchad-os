---
description: Independently verify documentation impact and accuracy.
argument-hint: (reads workflow artifacts)
---
# Documentation Review
As a fresh non-editing reviewer, check behavioral, config, API/schema, migration, operational, and developer-workflow documentation against code. No docs change is acceptable only with explicit internal/no-observable-change evidence. Write authoritative `$ARTIFACTS_DIR/review-docs.json` as `{"schema":1,"verdict":"pass|block","findings":[{"severity":"blocking|advisory","path":"...","evidence":"...","remedy":"..."}],"evidence":[...]}`. Output exactly that JSON.
