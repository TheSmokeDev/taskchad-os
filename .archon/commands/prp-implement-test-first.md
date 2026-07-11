---
description: Implement the approved PRP test-first and record structured readiness.
argument-hint: (reads workflow artifacts)
---
# Test-First PRP Implementation
Read the PRP, approved plan, reconnaissance, baseline, and repository rules. Confirm HEAD/branch still match baseline and existing diff is attributable to this run. For each behavior, add or adjust the smallest test first, execute RED, make the minimum production change, then execute GREEN. Preserve contracts/invariants unless explicitly changed. Do not commit, push, or open a PR. Write `$ARTIFACTS_DIR/implementation.md` with exact commands, outputs, changed files, and acceptance mapping. Also write authoritative `$ARTIFACTS_DIR/implementation.json`: `{"schema":1,"status":"ready|incomplete|escalate","red_green_evidence":[...],"changed_files":[...],"blockers":[...]}`. Use `ready` only with concrete RED/GREEN evidence and no blockers; output the exact JSON.
