# TokenMax SEO Authority Stack

Status: Implemented portable stack; production remains target-owned
Owner: SEO/GEO operator
Last updated: 2026-07-15

## What It Does

The TokenMax SEO Authority Stack is a set of three reusable skills with clear
handoffs. It discovers how a site renders content, coordinates many sites
without weakening gates, and creates small evidence-backed authority waves
after a site is production-ready.

It is not one monolithic workflow. A target repository still owns the code that
writes its pages and the runbook that deploys them.

```text
Evidence and fleet intent
  -> TokenMax Fleet Orchestrator
  -> TokenMax Site Factory
  -> repository-specific generation driver
  -> build, render, and production gates
  -> AI Citation Authority Wave
  -> separate deploy handoff
  -> indexing and 48-hour, 7-day, and 28-day measurement
```

## Responsibility Matrix

| Layer | Owns | Does not own |
|---|---|---|
| Evidence and intent | Query receipts, demand, locale, geography, domain ownership, collision rules | Content generation or deployment |
| `tokenmax-fleet-orchestrator` | Queueing, stage state, retries, freeze policy, deployment stages, live proof, indexing queue | Site-specific rendering logic |
| `tokenmax-site-factory` | Read-only scan, site profile, run-mode selection, pilot contract, rendered SEO validation | Fleet scheduling, page writing, production deployment |
| Repository driver | Pilot and batch content files, domain facts, framework integration, exact app build | Fleet-wide state or generic policy |
| Target deploy runbook | Commit, merge, deploy, live verification, rollback | Content research or indexing claims |
| `ai-citation-authority-wave` | One to three evidence-backed editorial pages, local gates, deploy handoff | Mass generation, production deploy, Reddit activity, indexing requests |
| Indexing and measurement | Sitemap submission, bounded URL queue, 48-hour/7-day/28-day observations | Proof of rankings or AI citations before results exist |

## Dependencies

Required:

- Git and isolated worktrees for mutable runs.
- Python for deterministic scanners, controllers, and validators.
- The target site's own package manager, build command, and production-like
  local render command or static output directory.
- A versioned repository driver that can write the target site's content
  format and integrate routes, links, metadata, and sitemap discovery.
- A reviewed production deployment and rollback runbook.
- Current first-party or live-search evidence for keyword ownership and every
  Authority Wave target.

Conditional:

- Archon for `tokenmax-site-agnostic-build` and
  `ai-citation-authority-wave` DAG execution.
- A fleet intent map when more than one related domain is active.
- A system scheduler for unattended fleet invocations, installed only after a
  canary passes.
- An authorized Search Console client or persistent browser worker for later
  sitemap and URL-submission work.

Credentials never belong in workflow arguments, fleet YAML, generated
artifacts, logs, or this manual.

## Installation

The tracked source packages live under `.claude/skills/`:

- `.claude/skills/tokenmax-site-factory/`
- `.claude/skills/tokenmax-fleet-orchestrator/`
- `.claude/skills/ai-citation-authority-wave/`

Install or verify global copies from the framework repository:

```powershell
python scripts/sync_seo_stack_skills.py install --target-root "$HOME/.agents/skills"
python scripts/sync_seo_stack_skills.py check --target-root "$HOME/.agents/skills"
```

For a target repository, copy only the skills it uses into `.claude/skills/`.
Install the bundled Archon resources from each skill's `assets/archon/`
directory, then validate the workflow before running it.

## End-To-End Order

### 1. Establish evidence and ownership

For a single site, record the target audience, query family, locale, geography,
and authoritative sources. For a fleet, assign each active domain a unique
intent and route owner before enabling generation.

Do not convert a missing keyword metric into zero demand. Preserve the source,
query, observation time, and measurement or SERP URLs in a versioned evidence
packet.

### 2. Scan the site and gate its profile

Run from the target repository root:

```powershell
python .claude/skills/tokenmax-site-factory/scripts/tokenmax_scan.py . --output .token-max/site-profile.json --json
python .claude/skills/tokenmax-site-factory/scripts/tokenmax_profile.py .token-max/site-profile.json --min-confidence 0.75 --json
```

The generated `.token-max/site-profile.json` is the portable adapter. It must
prove the content sink, route family, renderer, build command, sitemap surface,
internal-link surface, and local render contract. Open questions or confidence
below the configured threshold block writes.

Choose one run mode:

| Mode | Use it when |
|---|---|
| `augment-existing` | The repository already has a safe renderer, content sink, routes, and sitemap hooks. |
| `install-renderer` | The framework is clear but no durable content sink exists. |
| `homepage-geo` | A small brochure site should receive bounded homepage/entity improvements rather than city spokes. |
| `external-url-only` | Repository evidence is missing or confidence is too low; produce an audit only. |

### 3. Generate and validate a 10-page pilot

The Site Factory does not write domain content by itself. Configure the target
repository's driver to materialize exactly 10 representative routes using the
approved site profile, evidence, claims policy, and locale contract.

The pilot must pass:

- target-specific word and structure requirements;
- at most 10 percent pairwise eight-word-shingle overlap;
- source-backed regulated claims and numeric facts;
- one canonical URL and matching server-rendered language per page;
- JSON-LD, breadcrumbs, internal links, and an inbound hub link;
- local production-like HTTP `200` rendering;
- at least 10 percent normalized text-to-HTML ratio;
- the exact application build.

Keep pilot routes out of the production sitemap and use `noindex,follow` until
every configured gate passes.

### 4. Generate the full batch

Only the target repository driver writes the full batch. It must consume the
same approved profile and intent fingerprint as the pilot. A changed intent,
locale, route owner, renderer, or claims policy invalidates the pilot.

Run per-page validation while generating and a full-batch validation after the
last page. Source-file counts and lengths do not replace rendered-output proof.

### 5. Build, render, and prepare deployment

Start the target's production build locally and validate representative and
boundary routes:

```powershell
python .claude/skills/tokenmax-site-factory/scripts/tokenmax_validate_rendered.py --base-url http://127.0.0.1:3000 --routes /services/example-city /guides/example-city --sitemap-url http://127.0.0.1:3000/sitemap.xml --min-text-html-ratio 0.10 --min-words 2000 --min-main-words 2000 --max-pairwise-overlap 0.10 --shingle-size 8 --require-jsonld --require-canonical --require-internal-links
```

The target deploy runbook owns commit, merge, and production. A successful
local build is not production evidence.

### 6. Sequence sites through the fleet controller

Validate configuration and inspect a dry run before mutation:

```bash
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /srv/tokenmax/fleet.yaml validate-config
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /srv/tokenmax/fleet.yaml init
python .claude/skills/tokenmax-fleet-orchestrator/scripts/fleet_controller.py --config /srv/tokenmax/fleet.yaml run-next --dry-run
```

Each stage is a command array. The repository driver receives fleet and site
context through environment variables and may write a JSON stage result.

Use `block_site` for quality, content, build, and local-render failures. Use
`freeze_fleet` for uncertain push, deploy, production-route, or live-sitemap
state. Use `defer_site` only for work that is safe to retry later.

### 7. Run a bounded Authority Wave

Run Authority Wave only after the site is production-ready:

```powershell
archon workflow run ai-citation-authority-wave `
  --branch codex/example-authority-wave `
  "locale=auto max_pages=2 modes=reddit_modifier,direct_answer,comparison evidence_packet=research/evidence.json fleet_intent_map=research/fleet-intents.json"
```

Every target needs a current GSC, keyword-metrics, or documented live-SERP
receipt. The workflow creates at most three pages, uses fresh context per page,
requires two bound approvals, runs build and rendered gates, and stops at
`.citation-authority/deploy-handoff.json` with `production_deployed: false`.

It never logs into Reddit, posts or votes, requests indexing, or guarantees a
ranking or citation.

### 8. Deploy, index, and measure separately

The target deploy runbook or fleet deployment stage verifies the exact commit,
live HTTP, canonicals, schema, links, sitemap, and robots behavior. Only after
that proof should indexing state advance.

Submit the sitemap through an authorized client when available. Ordinary URL
requests belong in a bounded browser queue; the restricted Indexing API is not
a general page-submission mechanism. Quota, authentication, CAPTCHA, or UI
drift defers the queue without weakening production gates.

Start read-only measurement after actual deployment:

- 48 hours: crawl/indexing signals and early query discovery;
- 7 days: impressions, queries, ranking movement, and first citation checks;
- 28 days: page-level outcomes and the keep, revise, consolidate, or retire
  decision.

## Locale Contract

English and Spanish are independent research and writing lanes. Propagate the
chosen locale through evidence, page matrices, frontmatter, routes, metadata,
schema, sitemap entries, and rendered `<html lang>`.

Spanish output must be original Spanish, including native query phrasing and
conversion language such as `cotización`. Do not create `/es` automatically;
the site's root-language and route contract decide the path.

## Durable Artifacts

| Artifact | Owner | Meaning |
|---|---|---|
| `.token-max/site-profile.json` | Site Factory | Proven adapter and write gate for one repository |
| `.token-max/sample-plan.md` | Site Factory workflow | Approved 10-route pilot plan, not generated content |
| Fleet YAML and intent map | Fleet operator | Ordered stages, domains, intent ownership, and failure policy |
| `fleet-state.json` plus result/log files | Fleet controller | Resumable stage truth and evidence |
| Target content manifest | Repository driver | Expected routes, files, locale, claims, and validation state |
| `.citation-authority/*` | Authority Wave | Evidence, plan, approvals, drafts, validation, deploy handoff, and measurement queue |
| Live verification report | Deploy runbook | Exact production commit, routes, metadata, links, and sitemap proof |

## Definition Of Done

A site batch is complete only when the expected page count, per-page gates,
full-batch originality, exact build, rendered HTML, production deployment, live
routes, internal discovery, and live sitemap all match the approved contract.

An Authority Wave is locally ready when its validation report and checksummed
deploy handoff pass. It is production-complete only after a separate deploy and
live verification. Indexing, rankings, and AI citations remain measured
outcomes, not completion claims.

## Source Of Truth

- `.claude/skills/tokenmax-site-factory/`
- `.claude/skills/tokenmax-fleet-orchestrator/`
- `.claude/skills/ai-citation-authority-wave/`
- `scripts/sync_seo_stack_skills.py`
- `docs/manual/features/ai-citation-authority-wave.md`

## Public Export Status

Public-safe. The portable skills, this guide, and the sync utility contain no
tenant identities, production paths, credentials, or live fleet state.
