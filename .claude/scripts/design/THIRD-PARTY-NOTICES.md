# Third-party notices — native /design capability

The Homie's native `/design` capability ports design *method and assets* from
the open-source **Open Design** project. This file records attribution.

## nexu-io/open-design — Apache License 2.0

Source: https://github.com/nexu-io/open-design (Apache-2.0)

Adapted into this module (modified from the originals; the daemon, web app,
and export pipeline were NOT used — only the prompt method + asset data):

| Homie file | Adapted from (open-design) |
|---|---|
| `directions.py` | `apps/daemon/src/prompts/directions.ts` (5-school direction library) |
| `brief.py` (charter, anti-slop checklist, 5-dim critique, RULE-3 plan) | `apps/daemon/src/prompts/discovery.ts` |
| `.claude/scripts/design/_systems/<slug>/` — full package: `DESIGN.md` + `tokens.css` + `components.html` + `components.manifest.json` + `manifest.json` + `USAGE.md` (MIT — see below) | `design-systems/<slug>/` (same files) |

Per Apache-2.0 §4: the source is attributed here, modifications are stated
("Adapted from … Modified from the original." headers in each file), and this
NOTICE travels with the adapted files. The `/design` code and the bundled
brand systems ship in the public framework via `scripts/sanitize.py`; the
upstream Apache-2.0 / MIT license texts travel with the export.

## DESIGN.md brand systems — MIT

The bundled `DESIGN.md` systems are **MIT-licensed**: per open-design's own
`design-systems/README.md`, they originate from
[`VoltAgent/awesome-design-md`](https://github.com/VoltAgent/awesome-design-md)
(MIT) and are redistributed by open-design. They describe public design facts
(color values, type scales, spacing) of well-known products and are
**inspired-by** references, not affiliated with or endorsed by the named brands.

- **Trademark:** systems carry brand slugs (e.g. `stripe`, `ferrari`), matching
  upstream open-design's own public distribution. They are **inspired-by**
  references describing public design facts (color values, type scales, spacing)
  — no logos, fonts, or brand assets are bundled — shipped with this
  not-affiliated / not-endorsed disclaimer. To eliminate trademark exposure
  entirely, generic-rename before export (e.g. `stripe` → `premium-fintech`).
- The bundled library lives at `.claude/scripts/design/_systems/` (tracked,
  ships with the framework). GENERATED artifacts live under
  `vault/memory/design/` — a sanitizer `DENY_DIR` — so operator/client
  outputs never leak.

**Bundled set (B1 harvest, 2026-06-09 — 26 systems, full 6-file packages):**
`stripe wise revolut coinbase mastercard` (Fintech) · `tesla ferrari bmw lamborghini`
(Automotive) · `apple nike airbnb shopify spotify` (Consumer/Retail/Media) ·
`linear-app vercel notion supabase` (Dev/SaaS) · `claude openai` (AI) ·
`brutalism glassmorphism editorial luxury minimal retro` (aesthetic families).
Each package = `manifest.json + DESIGN.md + tokens.css + components.html +
components.manifest.json + USAGE.md`. Add more on demand from open-design's
150-system catalog.

## The Homie's own assets (no third-party source)

- The anti-AI-slop hard rules (no blue, no em-dashes, single type pairing, one
  accent, LLM-tell word ban) originate from The Homie's own `de-ai-slop-frontend`
  skill. They are fused into `brief.py` but are first-party (not third-party).
