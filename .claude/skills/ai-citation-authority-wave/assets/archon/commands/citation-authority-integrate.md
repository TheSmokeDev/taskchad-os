---
description: Integrate approved authority routes into the site's existing internal-link and sitemap architecture
argument-hint: (none - reads the approved plan and site profile)
---

# Integrate discovery surfaces

Read `.citation-authority/site-profile.json` and
`.citation-authority/candidate-plan.json`. Inspect the actual site router,
configured service hubs, and sitemap implementation.

Make only the smallest integration changes required so that, after a production
build:

- every new route appears in the rendered sitemap;
- every new route has at least one normal HTML inbound link from a configured
  service hub;
- the new page keeps its approved outbound service/context links;
- no route, locale, canonical, schema, or existing sitemap policy changes;
- no giant nav/footer dump is created merely to increase link count.

If the blog/resource index and sitemap already discover content files
automatically, make no edit and report that fact. Otherwise update the existing
registry/index/sitemap source using its established structure. Do not rewrite
page copy, create another route, deploy, use git, or request indexing.

End with the exact integration files changed, or `AUTO_DISCOVERY_NO_EDIT`.
