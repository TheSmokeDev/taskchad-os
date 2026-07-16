---
name: tokenmax-fleet-orchestrator
description: Run multiple TokenMax SEO/GEO sites as a resumable, fail-closed fleet. Use when Codex needs to audit a site queue, execute one isolated site at a time, preserve deterministic quality/build/deploy gates, resume interrupted runs, install a server-side scheduler, or report production and indexing state without hard-coding one website into the controller.
---

# TokenMax Fleet Orchestrator

Use this skill above `tokenmax-site-factory`. The site factory discovers and
validates one site, a versioned repository driver generates its pages, and this
skill sequences sites while recording what actually passed, shipped, and
reached production.

## Stack Position

Use evidence and a versioned fleet intent map before enabling related domains.
This controller owns queueing, retries, deployment stages, production proof,
and indexing queues; it delegates site discovery and rendered page gates to
`tokenmax-site-factory`. After a site is production-ready,
`ai-citation-authority-wave` may run separately and must stop at its own deploy
handoff.

Do not make the authority workflow a mass-generation stage or infer production
from local build state. When present, read
`docs/manual/features/tokenmax-seo-authority-stack.md` for the complete stack
order and handoff contracts.

## Start

1. Read the target repo's deploy manual and its existing TokenMax/fleet files.
2. Inspect active processes, worktrees, schedules, disk, and available memory.
3. Read `references/stage-contract.md` and validate the fleet YAML against
   `references/fleet-config.schema.json`.
4. Run a dry plan before any mutation:

```bash
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /path/to/fleet.yaml validate-config
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /path/to/fleet.yaml init
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /path/to/fleet.yaml run-next --dry-run
```

5. Forward-test one unproven site through production before installing a
   recurring timer.

## Controller

The controller is generic. Fleet-specific behavior belongs in YAML stage
commands and a versioned repo driver. Commands receive site/stage context via
environment variables and write an optional JSON stage result.

```bash
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /path/to/fleet.yaml status
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /path/to/fleet.yaml run-next --max-sites 1
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /path/to/fleet.yaml resume --site <id>
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /path/to/fleet.yaml retry --site <id>
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /path/to/fleet.yaml pause
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /path/to/fleet.yaml index-queue
```

## Fleet Intent Ownership

Before running more than one related domain, create a versioned intent map and
validate it with the fleet config. Use OpenSEO for first-party Search Console
evidence and focused keyword/SERP research; do not require GSC Wizard when
OpenSEO already provides the connection and data.

Each site contract must define:

- one primary query family, audience job, locale, and geography scope;
- one route-segment owner and page archetype;
- allowed modifiers and forbidden adjacent intents;
- a unique vertical profile for every active generation lane;
- `hold` status when demand, SERP intent, source safety, or regional inventory
  is not proven.

Fail config validation when two non-held sites share a normalized primary
query or route owner, an active site uses a generic fleet profile, a held site
is enabled, or the site/profile/locale values disagree between the fleet and
intent map. Treat old materialized pilots as stale when their intent
fingerprint changes; replace and revalidate the 10-page pilot before allowing
the 100-page stage.

Internal links stay inside each owning site by default. Cross-domain links are
intent-change handoffs only, with transparent branding. Never create a
fleet-wide reciprocal footer or a network of near-identical city pages.

## Required Gates

- One clean, isolated worktree per site.
- Scan/profile confidence before content writes.
- A small rendered pilot before the full batch.
- Per-page and full-batch word, uniqueness, fact, structure, and prohibited
  claim validation.
- Exact app build before any commit or deploy.
- Rendered HTTP, canonical, JSON-LD, internal-link, text/HTML, robots, and
  sitemap validation.
- Scoped staging and the target platform's required commit identity.
- Merge current remote base and rerun affected gates before pushing.
- Production content and sitemap proof after deploy.

Pre-deploy quality failures may block one site while leaving the remaining
queue eligible. Any uncertain post-push or live-production state freezes the
fleet. Search Console quota exhaustion defers indexing and never weakens the
build or production gates.

## Indexing Boundary

Automate sitemap submission when an authorized Search Console write client is
available. For ordinary pages, create a ranked top-URL queue for a persistent
browser worker; do not misuse Google's restricted Indexing API. Browser quota,
authentication, or UI drift is a deferred operation, not proof that a deploy
failed.

## Scheduling

Use the bundled systemd templates only after the canary passes. Keep one site
per invocation by default, retain the process lock, and inspect existing
timers before installation. A monitor may report status from another machine,
but generation and builds remain on the configured server.

## Rules

- Never place secrets in fleet YAML, command arguments, logs, or state JSON.
- Do not deploy from the generation skill; shipping is a separate stage here.
- Retries are for transient failures. Quality failures retain evidence and
  stop after their configured rewrite budget.
- Do not trust stale completion lists. Reconcile repo artifacts, live routes,
  sitemap state, and deployment evidence.
- Do not claim rankings, indexing, or production state from a successful local
  build.
