# TaskChad Build + Grow

Status: Implemented developer slice; production deployment and public plugin publication pending
Owner: `build_grow/`, orchestration API, dashboard proxy and web UI
Last updated: 2026-09-30

## What It Does

Build + Grow turns an owner-provided business brief into a reviewable website
draft and a technical SEO/GEO artifact report. It saves the business context,
every brief revision, each job's exact inputs, immutable deliverables, and
hash-bound review decisions.

The first workflow is:

```text
Business brief → website job → downloadable static site + checks
              → GEO job → evidence report → artifact acceptance or requested changes
```

The portable plugin teaches the host to use the same API through a packaged
Python HTTP client. Reasoning and repository work use the host's available
tools. The bounded website and GEO workers themselves make no model or provider
calls, execute no shell commands, and never publish files.

## Operator Entry Points

- Dashboard: `/build-grow`, under Workspace navigation.
- Python API: `/api/build-grow/*` on the existing orchestration API.
- Developer plugin: `plugins/taskchad-build-grow/`.
- Repo marketplace: `.agents/plugins/marketplace.json`.
- Plugin client: `python plugins/taskchad-build-grow/scripts/taskchad_client.py --help`.

Start the existing OS API and dashboard using their normal installation and
authentication flow. The router mounts with the orchestration API; it needs no
new provider key. The dashboard uses the existing operator session.

## Business Context And Storage

The brief contains business name, public description, industry, service area,
services and their descriptions, contact email, optional phone, optional final
HTTPS site URL, optional GitHub repository URL, and a brand color. The saved
details are owner-provided, not independently verified. The renderer does not
invent testimonials, certifications, prices, operating hours, rankings or
customer results.

`build-grow.db` lives beside the profile-resolved orchestration database. It is
separate from shared chat history and persona memory. Every public storage
operation requires the caller's workspace. Brief updates use the expected
revision; stale updates return `409`. A job freezes the brief and source files
it used, so later edits do not silently change an earlier deliverable.

Brief revisions, artifact bytes, and reviews are immutable. SQLite foreign
keys bind projects, jobs, sources, artifacts and reviews to their workspace.
Connections are per operation, with WAL and transactional job claims.

## Website Drafts

A website job produces a ZIP with Home, Services and Contact pages, responsive
CSS, business JSON-LD, a robots file, a review note and a checksum manifest.
When a final site URL is supplied, the draft also includes canonicals and a
sitemap. Supplied facts are escaped in HTML and JSON-LD.

Email and phone links use the supplied contact details. There is no form
submission backend, CRM, payment flow, analytics or external media generation
in this version. Drafts are deliberately `noindex` and robots-blocked. Before
a separate live release, review the facts, domain, contact behavior, hosting
and indexing policy, then verify the exact deployed files.

This builder is self-contained. It does not depend on the older client-site
factory's workflow/compiler files missing from the public export.

## SEO/GEO Artifact Checks

A GEO job uses a ready website job from the same project and current brief,
or a caller-selected packet of rendered HTML and supporting static files.
Packets are capped at 30 files and 2 MiB of uncompressed text. ZIP inputs are
read only in memory; traversal, duplicate names, symlinks and encrypted files
are refused. Uploaded content is never executed or fetched as instructions.

Reports record supplied-file hashes, pages inspected, language, primary
headings, page titles, descriptions, canonicals, indexing directives, JSON-LD
parseability, local navigation and readable HTML. These are bounded artifact
observations, not a comprehensive live audit or a predictive GEO score.
Live crawl/index coverage, search demand, AI citations and qualified lead
outcomes remain explicitly unknown without their own evidence.

TokenMax content expansion, private GEO research retrieval, automated repo
dispatch and live analytics remain future adapters. The plugin can guide
authorized repository edits using the host's tools; the worker does not claim
to have run those integrations.

## Job And Review Semantics

Jobs move `queued → running → ready_for_review` or `failed`. Running is claimed
atomically before generation. Deliverable insertion and ready status commit
together. A failed job keeps its inputs and an error state; it is not retried
automatically. Interrupted deterministic runs expire after five minutes and
become failed when that workspace's job state is next inspected. A new job is
required to try again.

Download exact artifact bytes before reviewing them. Acceptance and change
requests bind the artifact's SHA-256 digest. An artifact from an older brief
revision cannot receive a new review. Review records never commit, merge,
deploy, index, post or send anything.

## Authentication And Deployment Boundary

The Python router requires the shared middleware's workspace binding; mounting
it without that middleware fails closed. Every route declares
`tenant_workspace` policy. Cross-workspace project, job, source and artifact
IDs return `404`.

The dashboard is an **operator interface** in this release: its existing Hono
authentication accepts the operator token. The new proxy forwards the actual
incoming bearer, never silently substituting an admin identity. Tenant-scoped
clients can use the Python API when the existing tenant enforcement has been
explicitly enabled and scoped tokens provisioned. A customer login/dashboard,
OAuth MCP service and production hosting require additional implementation;
this slice does not turn the entire OS into a shared customer SaaS.

Plugin credentials come from `TASKCHAD_API_TOKEN` in the authorized execution
host, never chat text, committed files or command arguments. Loopback reaches
only that host's OS; a remote client needs an authorized HTTPS endpoint. Local
skills require shell access. The developer package is not installed in an
account or published in the public directory by this change.

Using the plugin does not grant TaskChad.com access to a customer's ChatGPT
subscription. Commercial subscription-backed API access requires the relevant
OpenAI program. Plugin workflows do not sell digital subscriptions, promote
upgrades, or initiate purchases.

## Source Of Truth And Verification

| Concern | Files |
|---|---|
| Input and artifact contracts | `.claude/scripts/build_grow/models.py` |
| Workspace persistence and immutable history | `.claude/scripts/build_grow/store.py` |
| Job orchestration and safe source loading | `.claude/scripts/build_grow/service.py` |
| Auth-bound REST routes | `.claude/scripts/build_grow/api.py`, `orchestration/api.py`, `orchestration/route_policy.py` |
| Website and artifact checks | `.claude/scripts/build_grow/artifacts.py` |
| Dashboard proxy and UI | `dashboard/server/src/routes/build-grow.ts`, `dashboard/web/src/pages/BuildGrow.tsx` |
| Plugin package and client | `plugins/taskchad-build-grow/` |
| Python checks | `.claude/scripts/tests/test_build_grow_*.py` |
| Dashboard checks | `dashboard/{server,web}/src/__tests__/build-grow.test.*` |

From `.claude/scripts`, run the focused Build + Grow tests and the existing
tenant route-policy gate. From each dashboard package, run its test command and
build. The plugin has its own fictional inputs and activation cases in
`TEST_CASES.md`; a real host skill activation review remains distinct from
manifest validation or HTTP client tests.

Public-safe source only: no private GEO corpus, customer brief, credentials,
production paths or client runtime data belong in this feature's public export.
