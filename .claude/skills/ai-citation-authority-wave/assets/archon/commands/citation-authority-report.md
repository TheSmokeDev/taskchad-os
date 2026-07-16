---
description: Report the bounded authority-wave result without overstating deployment, indexing, rankings, or citations
argument-hint: (none - reads .citation-authority artifacts)
---

# Report the authority wave

Read the available `.citation-authority/` artifacts. State only what those
artifacts prove.

If evidence status is `no_evidence`, report a successful no-op with the exact
reason and no page claims.

For a completed write path, report:

- site, locale, selected query/mode/route pairs;
- profile, evidence, plan, content, build, render, schema, canonical, internal
  inbound/outbound link, and sitemap gate results;
- deploy-handoff path and checksum count;
- measurement-queue count and its 48-hour, T+7, and Day-28 after-deploy semantics.

Say explicitly that the workflow did not deploy production, request indexing,
prove indexing, guarantee rankings, or prove an AI citation. Do not summarize
missing artifacts as passing.
