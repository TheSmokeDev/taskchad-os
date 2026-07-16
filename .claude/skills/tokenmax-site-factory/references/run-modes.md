# TokenMax Run Modes

`augment-existing` is the preferred mode. Use it when the scanner finds a
framework, app root, content sink, matching dynamic route, sitemap surface, and
build command with confidence at or above the local threshold.

`install-renderer` is for repos where the framework is clear but no durable
content sink exists. Install the smallest renderer needed to turn generated
Markdown or MDX into first-class routes, then re-run the scanner and validator.

`homepage-geo` is for brochure sites or small client sites where the best lift is
homepage-visible entity/schema/FAQ/service-area work. Do not force city-spoke
generation when the site has no route surface to support it.

`external-url-only` is for URL-only audits or low-confidence scans. Produce a
report and patch plan, not code writes.

Hard gates:

- Scanner confidence below threshold blocks writes.
- A missing build command blocks batch ship.
- A rendered sample with missing canonical, JSON-LD, sitemap inclusion, or
  internal links blocks batch ship.
- A failed build blocks deploy.
