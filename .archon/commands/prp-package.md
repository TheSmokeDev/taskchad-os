---
description: Package verified work for approval; do not publish.
argument-hint: (reads workflow artifacts)
---
# PRP Package
This node packages only. Re-read the PRP, diff, regression data, review aggregate, branch, and baseline. Verify all gates passed and no commit/push/PR occurred. Write `$ARTIFACTS_DIR/pr-body.md` with summary, acceptance mapping, changed files, exact tests/outcomes, security/state and docs impact, review dispositions, risks, and rollback. Write authoritative `$ARTIFACTS_DIR/pr-package.json` with exactly `{"schema":1,"status":"packaged","title":"...","commit_message":"...","branch":"...","body_file":"pr-body.md","changed_files":[...],"test_evidence":[...]}`. `branch` must equal baseline/current branch; `changed_files` must exactly equal all tracked and untracked changed paths and every path must fit preflight `allowed_paths`; title and commit message must be nonempty. Do not invent a digest: the deterministic package gate records `approved_diff_digest` immediately before approval. Do not commit, push, mutate history, or invoke `gh`. Output exactly the package JSON. Publishing occurs only after separate final human approval.
