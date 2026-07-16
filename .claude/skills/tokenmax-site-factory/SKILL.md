---
name: tokenmax-site-factory
description: Site-agnostic TokenMax SEO/GEO page factory setup and validation. Use when Codex needs to scan an existing website repo, infer where SEO/GEO content can be written, produce a .token-max/site-profile.json contract, validate rendered HTML, check sitemap/internal-link/JSON-LD/canonical gates, or install/run an Archon workflow for programmatic local SEO pages without hard-coded per-site adapters.
---

# TokenMax Site Factory

Use this skill to turn an arbitrary website repo into a TokenMax-ready target
without hand-writing a site adapter first. The contract is a generated
`.token-max/site-profile.json` with evidence, confidence, route families,
content sinks, SEO surfaces, build commands, and open questions.

## Stack Position

Use this skill below `tokenmax-fleet-orchestrator` and before any target-site
deploy runbook. It discovers and validates one site's content/render contract;
the target repository's versioned generation driver writes the pilot and full
batch. After a production-ready site ships, `ai-citation-authority-wave` may
create a separate evidence-backed wave of one to three editorial pages.

This skill does not schedule fleets, own production deployment, submit URLs for
indexing, or chain authority waves. When present, read
`docs/manual/features/tokenmax-seo-authority-stack.md` for the complete stack
order and handoff contracts.

## Install In A Target Repo

Copy this directory to `.claude/skills/tokenmax-site-factory/`, then copy the
bundled workflow into `.archon/workflows/`. The commands below assume they run
from the target repository root.

## Workflow

1. Run a read-only scan:

```powershell
python .claude/skills/tokenmax-site-factory/scripts/tokenmax_scan.py <repo> --output <repo>/.token-max/site-profile.json
```

2. Gate the profile before writing anything:

```powershell
python .claude/skills/tokenmax-site-factory/scripts/tokenmax_profile.py <repo>/.token-max/site-profile.json --min-confidence 0.75
```

3. Choose the run mode from the profile:

- `augment-existing`: existing renderer/content/sitemap surfaces are strong
  enough to write generated content into the detected sink.
- `install-renderer`: the framework is clear, but no safe content sink exists;
  add the smallest renderer/sitemap hook first.
- `homepage-geo`: there is no page-factory sink; apply homepage/schema/FAQ
  GEO fixes instead.
- `external-url-only`: repo access is missing or confidence is too low; produce
  an audit/report only.

4. Validate rendered pages after sample generation and before any batch ship:

```powershell
python .claude/skills/tokenmax-site-factory/scripts/tokenmax_validate_rendered.py --base-url http://127.0.0.1:3000 --routes /services/example-city /guides/example-city --sitemap-url http://127.0.0.1:3000/sitemap.xml --min-text-html-ratio 0.10 --min-words 2000 --min-main-words 2000 --max-pairwise-overlap 0.10 --shingle-size 8 --require-jsonld --require-canonical --require-internal-links
```

5. For Archon, copy
`assets/archon/tokenmax-site-agnostic-build.yaml` into the target repo's
`.archon/workflows/`, then adjust only the page-generation prompt/command for
the business domain. The target repository's driver remains responsible for
writing content. Keep scan, confidence, build, and rendered validation
deterministic.

## Rules

- Do not hard-code a site adapter unless the scanner emits low confidence and a
  human explicitly chooses a site-specific override.
- Treat `.token-max/site-profile.json` as the adapter. It must include evidence
  for every sink, route, build, sitemap, and SEO surface it claims.
- Do not generate a full batch until a small sample renders as HTML and passes
  build, text/HTML ratio, word-count, canonical, JSON-LD, sitemap, and internal
  linking gates.
- Require at least 2,000 words inside the initial, non-hidden server-rendered
  `<main>`. Text emitted only inside hidden Suspense/RSC containers does not
  count toward the rendered word gate.
- Require pairwise eight-word-shingle overlap at or below 10 percent across the
  pilot or batch. Source-file length and uniqueness do not replace this
  rendered-output check.
- Keep generated routes out of the sitemap and mark them `noindex,follow` until
  the rendered word, originality, metadata, link, and build gates all pass.
- Do not deploy from this skill. Use the target site's existing deploy runbook
  after local build and rendered validation pass.
- For regulated verticals, do not invent legal, medical, insurance, financial,
  or compliance claims. Use source-backed facts and target-site policy files.

## Non-English Locale Contract

- Propagate the configured locale through the page matrix, writer packet,
  frontmatter, route, schema, sitemap, and rendered `<html lang>` value. Never
  hard-code `en` in a locale-neutral factory surface.
- Materialize language-specific output instructions, FAQ/source headings,
  disclosures, authority-source labels, metadata templates, and visible page
  chrome. Internal instructions may be English, but public output may not mix
  languages.
- Spanish content must be original Spanish, not a translated city-swap page.
  Require professional spelling, accents, `ñ`, and opening punctuation where
  appropriate.
- Add a deterministic language gate before build. For Spanish, reject an
  English frontmatter locale, English scaffold phrases, an English FAQ heading,
  or unnaturally accentless long-form copy. Send failures back through a full
  page rewrite rather than patching isolated strings.
- The 10-page pilot remains mandatory for every locale. A valid YAML profile is
  configuration evidence; rendered pilot HTML is the ship evidence.

## Resources

- `scripts/tokenmax_scan.py`: read-only repo scanner and profile writer.
- `scripts/tokenmax_profile.py`: profile summary and confidence gate.
- `scripts/tokenmax_validate_rendered.py`: HTTP/rendered HTML SEO validator.
- `references/site-profile.schema.json`: generated profile contract.
- `references/run-modes.md`: mode selection and safety rules.
- `assets/archon/tokenmax-site-agnostic-build.yaml`: reusable Archon template.
