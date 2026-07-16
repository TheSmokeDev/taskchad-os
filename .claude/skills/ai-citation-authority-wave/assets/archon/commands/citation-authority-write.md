---
description: Write and self-validate exactly one queued authority page from an approved plan
argument-hint: (none - used by the workflow loop)
---

# Write exactly one queued page

This command is executed with fresh context for each page. Write one page,
validate it, repair that same page if necessary, then stop. Never advance to a
second queue item in the same iteration. Do not use a Reddit account, run git,
publish, deploy, or request indexing.

## Find the one target

Run:

```powershell
python .claude/skills/ai-citation-authority-wave/scripts/citation_authority.py next-page
```

If it reports `complete: true`, output `<promise>COMPLETE</promise>` and stop.
Otherwise read only the returned target plus:

- `.citation-authority/site-profile.json`
- `.citation-authority/candidate-plan.json`
- the configured content reference file or frontmatter template
- source URLs and existing pages needed to avoid duplication

Do not alter the approved query, title, slug, route, output path, opening
sentences, H2 list, sources, internal links, or brand-role passage.

## Write the draft and integrated page

Create both:

- `.citation-authority/pages/<target-id>.draft.md`
- the target's approved `output_path`

The two files must have the same normalized content. Match the site's real
frontmatter and content conventions. Keep content server-renderable without
client-side reveal gates.

Content rules:

1. The first paragraph contains the two approved direct-answer sentences
   verbatim and in order.
2. Use exactly the approved H2 headings, in order. Build a distinct argument
   under each rather than filling a template.
3. Meet `quality.min_main_words`, but remove repetition and keyword-density
   filler. The page must deserve to exist without search traffic.
4. Cite every approved external source naturally. Link every approved internal
   route naturally, including a service hub.
5. Include the approved transparent brand-role passage verbatim.
6. Synthesize Reddit discussion honestly. Do not imitate comments, invent
   consensus, imply endorsement, or automate on-platform activity.
7. On regulated sites, distinguish general information from licensed or legal
   advice. Every material number and regulated factual claim needs the approved
   authoritative source.
8. Spanish pages are fully native Spanish, including labels, dates, CTA, and
   metadata. They are not translations and do not use English scaffold copy.
9. Do not invent prices, rates, savings, approval, eligibility, rankings,
   customer counts, case studies, quotes, or outcomes.

## Write the page packet

Create `.citation-authority/pages/<target-id>.packet.json` with:

```json
{
  "schema_version": 1,
  "target_id": "<target id>",
  "slug": "<approved slug>",
  "route": "<approved route>",
  "locale": "<profile locale>",
  "direct_answer_sentences": ["<approved sentence 1>", "<approved sentence 2>"],
  "source_urls": ["<every approved source URL>"],
  "internal_links": ["<every approved internal route>"],
  "brand_role_passage": "<approved passage>",
  "numeric_claims": [
    {"value": "<exact rendered value, such as 3 years>", "source_ref": "<approved source URL>"}
  ],
  "compliance": {
    "fabrication_scan_passed": true,
    "regulated_claims_sourced": true,
    "original_language_copy": true
  }
}
```

Use an empty `numeric_claims` list when the article makes no material numeric
claim. Do not set a compliance flag true until you checked it.

## Validate and repair

Run:

```powershell
python .claude/skills/ai-citation-authority-wave/scripts/citation_authority.py validate-page --id <target-id>
```

If it fails, repair only this page and packet, then rerun until it passes. End
with the target ID, output path, visible word count, and `PAGE_VALIDATED`. Do
not emit `COMPLETE` unless the queue was already complete when this iteration
started.
