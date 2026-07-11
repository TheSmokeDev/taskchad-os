---
description: Independently review PRP compliance into structured evidence.
argument-hint: (reads workflow artifacts)
---
# PRP Spec Review
As a fresh non-editing reviewer, map every acceptance criterion to diff and executed tests; identify omissions, contradictions, scope creep, and unverifiable claims. Write authoritative `$ARTIFACTS_DIR/review-spec.json` as `{"schema":1,"verdict":"pass|block","findings":[{"severity":"blocking|advisory","path":"...","evidence":"...","remedy":"..."}],"evidence":[...]}`. Verdict is `block` iff any concrete blocking finding exists. Output exactly that JSON.
