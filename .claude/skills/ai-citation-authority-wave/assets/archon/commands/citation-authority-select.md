---
description: Select one to three collision-free authority-page targets from validated receipts
argument-hint: (none - reads .citation-authority artifacts)
---

# Select the authority wave

Create the exact bounded write plan. Do not edit site content, research new
queries, publish, deploy, or use a Reddit account.

## Inputs

Read:

- `.citation-authority/run-config.json`
- `.citation-authority/site-profile.json`
- `.citation-authority/evidence-packet.json`
- `.citation-authority/evidence-validation.json`
- the optional fleet intent map
- existing pages in and around the configured content sink

Only validated receipt IDs are eligible. Select between one and `max_pages`
targets, with an absolute maximum of three. If the evidence validation says
`no_evidence`, write a no-op plan with `status: no_evidence` and no targets.

## Selection rules

1. Prefer buyer intent, an observable query-to-page mismatch, an exposed SERP
   slot, or a measured outlier. Do not select on intuition alone.
2. Stay inside the modes enabled by `run-config.json`:
   `reddit_modifier`, `direct_answer`, and `comparison`.
3. Exact query text must appear in the title. The normalized exact query is the
   slug. Do not add a year unless a receipt contains year-stamped demand.
4. Reddit mode requires `reddit` in query/title and a real Reddit discussion in
   sources. It never claims Reddit endorses the site.
5. Direct-answer pages should answer a real `who helps`, eligibility, process,
   or problem-resolution query. Comparison pages must compare criteria and
   tradeoffs without inventing competitor prices, policies, or outcomes.
6. Read existing content and the fleet map. Reject same-site cannibalization and
   cross-property intent collisions.
7. Give every target a genuinely different H2 structure. Reordered template
   headings are not different.
8. Use at least two real source URLs. On a regulated profile, include a source
   from one of `regulated.authoritative_source_domains` and obey the claims
   policy exactly.
9. Include at least two internal links, one of which is a configured service
   hub. The final rendered gate also requires a service hub to link back.
10. Write two complete direct-answer sentences that can be used verbatim as the
    opening paragraph. Make them accurate, concise, and AI-citable.
11. Write a transparent brand-role passage. It may explain who the brand helps
    and how, but cannot create testimonials, rankings, licenses, outcomes, or
    customer counts.
12. Spanish targets are original Spanish from query through headings and CTA.
    Use the site's root route pattern and native conversion vocabulary.

## Output

Write `.citation-authority/candidate-plan.json` matching:

`.claude/skills/ai-citation-authority-wave/references/candidate-plan.schema.json`

Derive each route from `site-profile.route_family.pattern`, each output path
from `content_sink.path`, and the extension from `content_sink.format`. Keep all
paths repo-relative. End with the selected IDs and queries only.
