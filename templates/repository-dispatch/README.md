# Repository Dispatch Templates

These templates give operators a public-safe starting point for repository-aware
Archon workflow planning. They are examples, not runtime behavior.

## Files

- `repositories.example.yaml` shows a placeholder-only repository schema.
- `homie-work-item.example.yml` shows a GitHub issue-form work packet that a
  project owner can copy into a repository.

## How To Use

1. Copy `repositories.example.yaml` into your profile-owned configuration area.
2. Replace placeholders such as `<owner>/<repo>`, `<path-to-local-repo>`, and
   `<default-branch>` with your own values.
3. Keep real local paths, private repository maps, and operator state out of
   tracked public framework files.
4. If you want the issue form, copy `homie-work-item.example.yml` into your own
   repository under `.github/ISSUE_TEMPLATE/`.
5. Run Archon workflows manually after confirming the repository, branch, work
   packet, and stop conditions.

## Boundaries

- These templates do not auto-dispatch work.
- These templates do not auto-triage issues.
- These templates do not auto-merge branches.
- These templates do not claim an autonomous factory.
- Runtime config support is a separate opt-in follow-up.
