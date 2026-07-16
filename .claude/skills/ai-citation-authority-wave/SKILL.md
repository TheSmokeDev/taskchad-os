---
name: ai-citation-authority-wave
description: Build a bounded, evidence-first SEO/GEO/AIO editorial wave for any website. Use after a site is production-ready to scan its content contract, validate GSC/OpenSEO/SERP receipts, create one to three original English or Spanish authority pages, and produce a build-gated deploy handoff without deploying production.
---

# AI Citation Authority Wave

Use this skill for small editorial authority waves that target recommendation,
comparison, direct-answer, or Reddit-modifier searches. It is a sibling of the
TokenMax factory, not a TokenMax page family.

## Stack Position

Run this skill only after the site has a proven content/render contract and is
production-ready. `tokenmax-site-factory` owns site discovery and mass-page
validation; `tokenmax-fleet-orchestrator` may own the site's deployment and
production proof. This workflow owns a separate evidence-backed wave of one to
three editorial pages and stops at a checksummed deploy handoff.

Never place this workflow inside a TokenMax batch loop or treat its local handoff
as production. When present, read
`docs/manual/features/tokenmax-seo-authority-stack.md` for the complete stack
order and handoff contracts.

## Operating Boundary

- Generate one to three pages per domain per wave. Reject larger requests.
- Require a current GSC, OpenSEO/keyword-metrics, or documented live SERP
  receipt for every target.
- Never post to, seed, or automate Reddit accounts. Reddit-modifier pages are
  owned website pages that synthesize public discussions without implying an
  endorsement.
- Write English and Spanish pages independently. Never translate one lane into
  the other.
- Keep every page structurally distinct and source-backed.
- For regulated topics, require authoritative sources and the target site's
  claims policy. Never invent prices, eligibility, legal conclusions, outcomes,
  or competitor policies.
- Stop after a validated deploy handoff. The target site's deploy runbook or
  fleet controller owns production.

## Install In A Target Repo

Copy the skill directory into `.claude/skills/ai-citation-authority-wave/`.
Then copy the bundled workflow and command files into the target repo:

```powershell
Copy-Item .claude/skills/ai-citation-authority-wave/assets/archon/ai-citation-authority-wave.yaml .archon/workflows/ai-citation-authority-wave.yaml
Copy-Item .claude/skills/ai-citation-authority-wave/assets/archon/commands/citation-authority-*.md .archon/commands/
```

Keep installed files byte-identical to their bundled copies. Codex setups that
discover project skills through `.agents/skills/` may mirror this same directory
there, but the workflow's canonical executable copy remains under `.claude/` so
Claude Code and Archon installations share one tracked package.

Validate the installation:

```powershell
archon validate workflows ai-citation-authority-wave
python .claude/skills/ai-citation-authority-wave/scripts/citation_authority.py --help
```

## Run

Run in an Archon worktree:

```powershell
archon workflow run ai-citation-authority-wave `
  --branch codex/citation-wave-example `
  "locale=es max_pages=2 modes=reddit_modifier,direct_answer evidence_packet=research/evidence.json"
```

Controls:

- `locale=auto|en|es` (default `auto`)
- `max_pages=1|2|3` (default `2`)
- `modes=reddit_modifier,direct_answer,comparison`
- `evidence_packet=<relative-or-absolute-json-path>` (optional)
- `fleet_intent_map=<relative-or-absolute-json-path>` (optional)

If `.token-max/site-profile.json` exists, the scanner uses it as evidence. The
workflow always materializes its own `.citation-authority/site-profile.json`
and remains usable without TokenMax.

## Artifacts

The run writes under `.citation-authority/`:

- `run-config.json`
- `site-profile.json`
- `evidence-packet.json`
- `candidate-plan.json`
- `approval-record.json`
- `page-queue.json`
- `pages/*.draft.md` and `pages/*.packet.json`
- `build-result.json`
- `render-validation.json`
- `validation-report.json`
- `deploy-handoff.json`
- `measurement-queue.jsonl`

`status: no_evidence` is a successful no-op. Search Console quota exhaustion is
deferred indexing state and never bypasses build or rendered-HTML gates.

## Deterministic Commands

The workflow calls `scripts/citation_authority.py`. Useful standalone checks:

```powershell
python .claude/skills/ai-citation-authority-wave/scripts/citation_authority.py scan
python .claude/skills/ai-citation-authority-wave/scripts/citation_authority.py gate-profile
python .claude/skills/ai-citation-authority-wave/scripts/citation_authority.py validate-evidence
python .claude/skills/ai-citation-authority-wave/scripts/citation_authority.py validate-plan
python .claude/skills/ai-citation-authority-wave/scripts/citation_authority.py validate-pages
python .claude/skills/ai-citation-authority-wave/scripts/citation_authority.py aggregate
```

The JSON schemas under `references/` document the portable packet contracts.
