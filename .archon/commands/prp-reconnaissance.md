---
description: Produce evidence-backed bounded reconnaissance.
argument-hint: (reads workflow artifacts)
---
# PRP Reconnaissance
Read authoritative `preflight.json`, the PRP, repository instructions, relevant code/tests/config, and current diff without editing tracked files. Trace behavior, interfaces, invariants, state transitions, test seams, and blast radius. Create authoritative `$ARTIFACTS_DIR/reconnaissance.json` with exactly `schema: 1`, `status` (`ready|revise|escalate|abort`), nonempty relevant `files`, `invariants`, `risks`, and nonempty path/symbol-based `evidence`. Output exactly the same JSON. Never use readiness wording to hide a non-ready status.
