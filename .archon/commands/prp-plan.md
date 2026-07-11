---
description: Produce a bounded test-first implementation plan.
argument-hint: (reads workflow artifacts)
---
# PRP Plan
Read authoritative preflight/reconnaissance artifacts, baseline, PRP, and repository rules. Do not modify tracked files. Write `$ARTIFACTS_DIR/plan.md` with scope/non-goals, acceptance traceability, ordered file/symbol changes, invariants/state transitions, RED-before-production steps, exact focused/regression commands, security/docs implications, rollback, and stop/escalation conditions. Every step must be bounded and independently verifiable. Return the complete plan.
