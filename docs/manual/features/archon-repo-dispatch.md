# Archon Repo Dispatch

Status: public-safe pattern and templates
Owner: Operator workflow
Last updated: 2026-06-08

## What It Does

Archon repo dispatch is an operator pattern for choosing the right repository
context before starting an Archon workflow. It keeps repository selection,
planning, and issue packet scope explicit while leaving execution under human
operator control.

This page documents the public-safe pattern only. It does not enable automatic
runtime dispatch, automatic issue triage, automatic merges, or unattended
workflow execution.

## Operator Entry Points

- CLI profile setup: `thehomie profile init-archon <profile-name>`
- CLI Archon status: `thehomie archon status`
- CLI workflow list: `thehomie archon list`
- CLI workflow run: `thehomie archon run <workflow> -- <args>`
- Templates: `templates/repository-dispatch/`

## Source Of Truth Files

| Layer | Files |
|---|---|
| CLI/runtime | `.claude/scripts/personas/archon.py`, `.claude/scripts/thehomie_cli.py` |
| Templates | `templates/repository-dispatch/README.md`, `templates/repository-dispatch/repositories.example.yaml`, `templates/repository-dispatch/homie-work-item.example.yml` |
| Public docs | `docs/manual/features/archon-repo-dispatch.md` |
| Export guard | `scripts/sanitize.py`, `scripts/sanitize_test.py` |

## Repository-Aware Dispatch Pattern

Use repository-aware dispatch as a checklist before running a workflow:

1. Identify the target repository by a short slug and a GitHub repository name.
2. Confirm the local checkout path and default branch.
3. Confirm whether Archon is appropriate for this work item.
4. Choose a manual dispatch mode.
5. Run the selected workflow only after the operator confirms the target
   repository, branch, and work packet.

The public template schema is intentionally descriptive. The first public slice
ships docs and examples so operators can standardize their own process. Runtime
configuration, validation commands, and session briefing belong to a later
opt-in slice.

## Factory-Lite Work Items

Factory-lite work items are scoped packets of work. They should describe:

- the target repository
- the intended branch or worktree
- the source-of-truth plan or tracker entry
- the expected proof
- the stop conditions

The issue packet is not the source of truth by itself. Each user or team should
keep their own tracker, PRP, runbook, or planning system as the durable record.
The work item points to that system and gives the operator enough context to
start safely.

## Safety Boundaries

- No auto-dispatch: repository selection stays explicit.
- No auto-triage: labels and issue forms are user-owned process aids.
- No auto-merge: code review, validation, and merge remain operator actions.
- No public Dark Factory claim: this is a lightweight workflow pattern, not an
  autonomous factory.
- No tracked real repo map: local paths and repository inventories belong in
  profile-owned config or private operator state.
- No private issue forms: tracked `.github/` issue templates stay outside the
  public framework export unless a project owner copies the example into their
  own repository.
- No provider-readiness claim: this page does not claim that a given Archon,
  Codex, or other provider automation path is fully solved.

## How To Run It

Copy and adapt the templates:

```powershell
Copy-Item templates\repository-dispatch\repositories.example.yaml <profile-config-path>
Copy-Item templates\repository-dispatch\homie-work-item.example.yml <your-repo>\.github\ISSUE_TEMPLATE\homie-work-item.yml
```

Then run the existing Archon CLI surface manually:

```powershell
thehomie profile init-archon <profile-name>
thehomie archon status
thehomie archon list
thehomie archon run <workflow> -- <args>
```

## How To Test It

```powershell
uv run pytest scripts\sanitize_test.py -q
uv run python scripts\sanitize.py --dry-run
```

Before publishing a public export, inspect the categorized public diff and
verify that no runtime state, local paths, personal repository names, issue
queues, private tracker files, or private workflow artifacts are present.

## Latest Live Proof

- Date: 2026-06-08
- Surface: sanitizer dry-run and public-safe template review
- Result: docs/templates slice only; no runtime behavior change
- Proof docs/artifacts: this manual page plus sanitizer regression tests

## Public Export Status

Public-safe docs/templates are eligible for export after sanitizer validation
and explicit operator approval. The private source repository remains the source
of truth; the public mirror must be produced through `scripts/sanitize.py`.

## Next Slices

- Add an opt-in profile-owned `repositories:` config section.
- Add validation-only CLI surfaces such as `thehomie repositories status` and
  `thehomie repositories validate`.
- Add optional compact session briefing only when repository config is enabled.
- Continue to defer automatic dispatch, execution, triage, and merge behavior.
