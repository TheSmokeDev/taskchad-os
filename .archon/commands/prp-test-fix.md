---
description: Run one bounded test/remediation iteration with machine evidence.
argument-hint: (reads workflow artifacts)
---
# Focused Test/Fix Iteration
Read the PRP, plan, implementation, diff, and authoritative structured test specs. Run every focused test as its literal argv with `shell=False` from its confined repository-relative cwd; never interpret strings, shell syntax, redirects, pipes, substitutions, or metacharacters. Fix only evidenced in-scope failures, tests first, then rerun all focused tests after every code change. Do not commit/push/publish and do not create or modify any review verdict or aggregate artifact. Write authoritative `$ARTIFACTS_DIR/test-results.json` as `{"schema":1,"status":"pass|fail|escalate","runs":[{"spec":{"cwd":"...","argv":[...]},"exit_code":0,"evidence":"..."}],"blockers":[...]}`. `pass` requires a nonempty run list, every exit code exactly zero, and no blocker. Output only `__ARCHON_FOCUSED_PASS_9E31C7__` when that exact condition holds; otherwise output failure evidence without that sentinel. Reviews are fresh, non-editing, and blocked review runs fail permanently.
