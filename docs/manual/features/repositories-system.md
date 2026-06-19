# Repositories System

Status: Shipped (#63), merged to master
Owner: `.claude/scripts/repository_memory.py` + `.claude/scripts/repository_config.py`
Last updated: 2026-06-19

## What It Does

The Repositories System is the dispatch arm for coding work across tracked
repos. It is the memory-and-config layer that tells the agent **where** and
**how** to dispatch a coding task; the
[Archon Repo Dispatch](archon-repo-dispatch.md) pattern is the **checklist** that
consumes it, and [Archon Workflows](archon-workflows.md) is the engine that runs
the dispatched work.

It realizes the slices that the Archon Repo Dispatch page listed as future work:
a profile-owned `repositories:` config section, validation-only CLI surfaces, and
a compact session briefing that appears only when repo config is present and
valid. It does **not** auto-dispatch, auto-triage, or auto-merge.

## The Two Layers

| Layer | Where | Holds | Exported? |
|---|---|---|---|
| **Private memory** | a repo index page + one page per tracked repo, in the operator's vault | Dispatch defaults, workflow preferences, dispatch history, per-repo activity | **No** — operator-owned, sanitizer-denied |
| **Profile config** | the active profile's `config.yaml` → `repositories:` section | Per-repo runtime config: slug, repo name, branch, checkout path, Archon-enabled flag, dispatch mode | The schema is public; the values are operator state |

The private memory layer is the durable, human-readable record (read before
dispatch). The profile config layer is the machine-validated runtime config
(`repository_config.py`). Neither layer starts a workflow on its own.

## Operator Entry Points

- CLI: `thehomie repositories status [--json]` and `thehomie repositories validate`
  (validation/reporting only — they never dispatch).
- SessionStart: when the profile config is present and valid, a compact repo
  index + dispatch defaults briefing is injected into the session's opening
  context (via the bootstrap path).
- The per-repo memory pages are read by the agent before substantive coding work.

## Profile Config Schema

The optional `repositories:` section in the profile `config.yaml`. Field names
are fixed; values are operator state (placeholders shown):

```yaml
repositories:
  enabled: true
  items:
    - slug: <short-slug>
      github_repo: <owner>/<repo>
      default_branch: main
      local_path: /path/to/checkout
      archon_enabled: true
      dispatch_mode: archon-preferred   # or: manual
```

| Field | Type | Meaning |
|---|---|---|
| `slug` | string | Short handle used to resolve the repo. |
| `github_repo` | string | `<owner>/<repo>` name. |
| `default_branch` | string | Base branch for dispatch. |
| `local_path` | string | Checkout path on the operator's machine. |
| `archon_enabled` | bool | Whether Archon is appropriate for this repo. |
| `dispatch_mode` | enum | `manual` or `archon-preferred`. |

`repository_config.py` validates shape, required fields, duplicate slugs, and the
dispatch mode; an invalid section surfaces as errors in `validate`/`status` and
suppresses the briefing rather than failing the session.

## Per-Repo Memory Pages

Each tracked repo gets one page in the operator's vault under a required-section
contract (validated by `repository_memory.py`). The pages carry the durable
context the agent needs before dispatch: dispatch defaults, workflow preferences,
dispatch history, and recent per-repo activity. These pages are **operator-owned
and never exported** — they hold real repo names, local paths, and history.

## Integration Points

| Surface | What it does |
|---|---|
| SessionStart briefing | Injects the compact repo index + dispatch defaults when config is enabled + valid. |
| Daily reflection | Routes repo/codebase activity into the matching per-repo page (dispatch history, recent activity, workflow prefs). |
| Session flush | Captures repo slug, workflow, branch, and outcome as daily-log bullets. |

## Safety Boundaries

- **Validation-only.** `status` and `validate` report; they never start a
  workflow, create a worktree, triage an issue, or merge.
- **The private memory layer never exports.** The repo index and per-repo pages
  live in the operator's vault and are sanitizer-denied; only the config *schema*
  and this mechanism description are public.
- **Fail-soft config.** An invalid or absent `repositories:` section degrades to
  "no briefing," never a session failure.
- **No real repo map in the framework.** Tracked repo inventories and local paths
  are profile-owned/private operator state, by design.

## How To Run It

```powershell
cd .claude/scripts
uv run thehomie repositories status --json
uv run thehomie repositories validate
```

With no `repositories:` section configured, `status` reports a clean disabled
config and `validate` exits without errors.

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_repository_session_injection.py -q
# plus the sanitizer denial proof for the private memory layer:
uv run python ../../scripts/sanitize.py --dry-run
```

## Latest Live Proof

- Date: 2026-06-19
- Surface: merged PR #81 (squash commit `d2ed2ba5`).
- Result: shipped with `.claude/sections/09_repositories.md` and the
  `homie-self-map` skill; the sanitizer denies the private repo index + per-repo
  pages while exporting the config schema and helpers.
- Proof: the merged PR + sanitizer denial coverage.

## Public Export Status

Public-safe by construction: this page documents the *mechanism* and the config
*schema* only — no real repo names, paths, or history. The private memory layer
is sanitizer-denied. This page ships publicly through an explicit per-file entry
in the sanitizer `INCLUDE_FILES` list; export goes only through
`scripts/sanitize.py`.

## Next Slices

- Optional richer per-repo briefing (workflow-preference-aware dispatch hints).
- Tighter coupling with the Archon workflow selection (mode → default workflow).
