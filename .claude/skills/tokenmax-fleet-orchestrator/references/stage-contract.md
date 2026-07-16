# Fleet Stage Contract

## Configuration

The controller accepts YAML with four top-level fields:

- `schema_version: 1`
- `fleet`: fleet id, state directory, and lock file
- `stages`: ordered command definitions
- `sites`: ordered/priority site metadata

Stage commands are arrays, never shell strings. Supported placeholders come
from built-in context, top-level `variables`, site `metadata`, and site
`variables`:

- `{config}`, `{config_dir}`, `{state_dir}`
- `{site_id}`, `{stage}`, `{run_id}`
- Fleet/site-defined scalar values such as `{repo}` or `{domain}`

The controller also exports:

- `TOKENMAX_FLEET_ID`
- `TOKENMAX_SITE_ID`
- `TOKENMAX_STAGE`
- `TOKENMAX_STAGE_RESULT`
- `TOKENMAX_RUN_ID`
- `TOKENMAX_STATE_DIR`
- `TOKENMAX_CONFIG`

## Result JSON

A stage may write this object to `TOKENMAX_STAGE_RESULT`:

```json
{
  "outcome": "passed",
  "summary": "100 rendered pages passed",
  "metrics": {"page_count": 100, "minimum_words": 2712},
  "artifacts": {"report": "/absolute/path/report.json"}
}
```

Valid outcomes:

- `passed`: advance to the next stage.
- `complete_site`: mark the site complete and skip remaining stages.
- `deferred`: retain the current stage and defer the site.
- `failed`: apply the stage failure policy.

Exit code must be zero for `passed`, `complete_site`, or `deferred`. Missing
result JSON defaults to `passed` only when the command exits zero.

## Failure Policies

- `block_site`: retain evidence, ship nothing from that site, leave other
  sites eligible.
- `freeze_fleet`: block the site and prevent all later runs until an operator
  verifies production state and unfreezes the fleet.
- `defer_site`: retain the stage for a later retry without weakening gates.

Use `freeze_fleet` for push, deploy, production-route, or live-sitemap stages.
Use `block_site` for scan, content, build, and local-render failures.

## Idempotence

Every stage command must be safe to rerun after interruption. Durable work
belongs in the site worktree or the fleet state directory, never only in a
process's memory. A completed stage is not rerun unless `retry --stage` resets
it.
