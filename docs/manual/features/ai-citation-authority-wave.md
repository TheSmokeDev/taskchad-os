# AI Citation Authority Wave

Status: Implemented and local-canary proven; target-site deployment remains external
Owner: SEO/GEO operator
Last updated: 2026-07-15

## What It Does

AI Citation Authority Wave is a site-agnostic, evidence-first editorial
workflow for one to three recommendation, direct-answer, comparison, or
Reddit-modifier pages. It scans a website's real content and render contract,
requires a current GSC, OpenSEO, or live-SERP receipt for every target, writes
one structurally distinct page per fresh AI context, proves the built HTML and
discovery surfaces, and produces a checksummed deploy handoff.

This is a post-factory sibling of TokenMax. It is deliberately not a TokenMax
page family and must not become a mass-generation lane.

## Operator Entry Points

- CLI: `archon workflow run ai-citation-authority-wave`
- Workflow: `.archon/workflows/ai-citation-authority-wave.yaml`
- Skill: `.claude/skills/ai-citation-authority-wave/SKILL.md`
- Deterministic validator: `citation_authority.py`

There is no dashboard, chat-router, deployment, Reddit-account, or indexing
executor in this feature.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Archon DAG | `.archon/workflows/ai-citation-authority-wave.yaml` |
| AI command contracts | `.archon/commands/citation-authority-*.md` |
| Portable skill | `.claude/skills/ai-citation-authority-wave/` |
| Deterministic gates | `.claude/skills/ai-citation-authority-wave/scripts/citation_authority.py` |
| Packet schemas | `.claude/skills/ai-citation-authority-wave/references/*.schema.json` |
| Tests | `.claude/skills/ai-citation-authority-wave/scripts/tests/test_citation_authority.py` |
| Run artifacts | target repo `.citation-authority/` |

The installed workflow and commands must remain byte-identical to the copies
under the skill's `assets/archon/` directory.

## Execution Flow

1. Parse bounded controls: locale, modes, one-to-three-page cap, optional
   evidence packet, and optional fleet intent map.
2. Scan the repo and materialize `.citation-authority/site-profile.json`.
3. Fail closed below `0.80` confidence or while any content, render, sitemap,
   internal-link, brand-role, or regulated-claims question is open.
4. Preserve supplied GSC/OpenSEO evidence or perform a documented live SERP
   autopsy. No defensible receipt produces a successful `no_evidence` no-op.
5. Select collision-free targets and validate exact query/title/slug, source,
   structure, language, route, output, and fleet ownership contracts.
6. Pause for target approval.
7. Write one queued page per fresh-context loop iteration, with a hard maximum
   of three iterations. Validate each page before advancing.
8. Integrate each route into the existing service hub and sitemap architecture.
9. Run the site's own build, start its production build locally, and validate
   the rendered routes.
10. Pause for deploy-handoff approval, then emit checksums and read-only
    48-hour, T+7, and Day-28 after-deploy measurement checkpoints. Stop without
    deploying.

## Site Profile Gate

The scanner reuses `.token-max/site-profile.json` when present but always owns
its independent `.citation-authority/site-profile.json`. A standalone scan is
allowed. It will intentionally stop for operator confirmation when repository
inspection cannot prove a contract.

Required confirmed fields include:

- HTTPS canonical host and root language (`en` or `es`)
- content sink plus a real reference page or frontmatter template
- route pattern and build command
- local production-render start command or static output directory
- sitemap endpoint and at least one service-hub route
- truthful brand name and role
- minimum visible words, text/HTML ratio, and pairwise-overlap limit
- claims policy and authoritative source domains for regulated sites

Edit the generated profile with real site facts, clear its blocking questions,
then resume the failed Archon run. Do not weaken the confidence threshold.

## Evidence Contract

Every selected page references at least one validated receipt:

- `gsc`: query, observation time, date range, and impressions or clicks
- `openseo`: query, observation time, measured metric, and run ID/source URL
- `serp_autopsy`: exact query, engine, observation time, and at least three
  result URLs

Receipts older than 90 days fail. Keyword-tool silence is not converted into a
zero. Public SERPs are not represented as private GSC data. A Reddit-modifier
target needs a real Reddit discussion source, but the resulting owned page must
never imply that Reddit endorses the brand.

## English And Spanish

`locale=en` and `locale=es` are separate research and writing lanes. Spanish
pages must be original Spanish from query through metadata and CTA, use native
terms such as `cotizacion`, and follow the site's root route contract. The
workflow does not create `/es` merely because the copy is Spanish.

The validator rejects common English scaffold text on Spanish pages. It also
requires the page packet to attest that original-language, fabrication, and
regulated-claims checks passed.

## SEO, GEO, And Discovery Gates

The pre-build content gate requires:

- the two approved direct-answer sentences in the first paragraph
- the approved, page-distinct H2 outline
- visible word minimum without keyword-density filler
- all approved external citations and internal links
- a transparent brand-role passage
- source references for material numeric claims
- an integrated content file identical to the validated draft
- bounded pairwise content and heading overlap

The local rendered gate requires:

- HTTP `200` and matching `lang`
- exact production canonical
- `Article` or `WebPage` plus `BreadcrumbList` JSON-LD
- visible `<main>` word count and configured text/HTML ratio
- at least two unique internal links, including a service hub
- a normal HTML inbound link from a configured service hub
- every new route present in the rendered sitemap

Hidden RSC/Suspense/template payload does not count as visible content.

## Safety Boundaries

- One to three pages per domain per wave. Four is invalid input.
- Two explicit approvals: target selection and deploy handoff.
- No mass publishing, automatic wave chaining, or TokenMax factory routing.
- No Reddit login, post, comment, seeding, voting, or account automation.
- No invented prices, rates, savings, eligibility, licenses, rankings,
  testimonials, customer counts, case studies, or competitor policies.
- No production deployment, git push, sitemap submission, URL Inspection
  request, or indexing request.
- No ranking, indexing-speed, or AI-citation guarantee.

Production remains owned by the target site's reviewed deploy runbook or fleet
controller. The handoff explicitly records `production_deployed: false`.

## How To Install It

Copy the portable skill to the target repo and install its bundled Archon
resources:

```powershell
Copy-Item -Recurse <source>/.claude/skills/ai-citation-authority-wave .claude/skills/
Copy-Item .claude/skills/ai-citation-authority-wave/assets/archon/ai-citation-authority-wave.yaml .archon/workflows/
Copy-Item .claude/skills/ai-citation-authority-wave/assets/archon/commands/citation-authority-*.md .archon/commands/
archon validate workflows ai-citation-authority-wave
```

## How To Run It

Example with a prepared receipt packet and fleet ownership map:

```powershell
archon workflow run ai-citation-authority-wave `
  --branch codex/authority-wave-example `
  "locale=es max_pages=2 modes=reddit_modifier,direct_answer evidence_packet=research/evidence.json fleet_intent_map=research/fleet-intents.json"
```

The workflow requires a worktree. If the profile gate exposes facts that cannot
be inferred, update `.citation-authority/site-profile.json` in that worktree and
resume:

```powershell
archon workflow run ai-citation-authority-wave --resume
```

## How To Test It

```powershell
python -m py_compile .claude/skills/ai-citation-authority-wave/scripts/citation_authority.py
python -m unittest discover -s .claude/skills/ai-citation-authority-wave/scripts/tests -v
archon validate workflows ai-citation-authority-wave --json
archon validate commands citation-authority-research --json
archon validate commands citation-authority-select --json
archon validate commands citation-authority-write --json
archon validate commands citation-authority-integrate --json
archon validate commands citation-authority-report --json
```

The focused suite includes complete English and Spanish static-site canaries as
well as negative cases for stale evidence, fleet collisions, regulated-source
gaps, thin content, unsourced numbers, missing sitemap routes, and missing
service-hub inbound links.

## Artifact And Proof Boundary

A completed run writes `.citation-authority/deploy-handoff.json` and
`.citation-authority/measurement-queue.jsonl`. Those artifacts prove local
readiness only. After a separate production deployment, the operator must still
verify the exact production SHA/row state, live HTTP, metadata, links, sitemap,
and measurement start time before marking the site live.

The measurement queue is `pending_deploy` and schedules read-only early-signal
(48-hour), T+7, and Day-28 reviews after actual deployment. It does not create
a standing monitor.

## Public Export Status

Public-framework safe, not yet exported. Export still goes through
`scripts/sanitize.py`; never copy directly into the public repo.

## Next Slices

- Add an optional fleet controller only after this bounded workflow is proven
  on at least one English and one Spanish production property.
- Add schema-library validation if JSON Schema becomes a guaranteed runtime
  dependency; deterministic semantic gates remain the source of truth.
