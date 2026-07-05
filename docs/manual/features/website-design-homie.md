# Website Design Homie

Status: Playbook/skill added; runtime generation uses existing `/design`
Owner: `.claude/skills/website-design-homie/` + native design slice
Last updated: 2026-06-29

## What It Does

Website Design Homie is the conversion-site role for premium local-business
website demos. It turns a niche or prospect into a 5-10 page agency-grade site
concept: page architecture, design direction, proof sections, quote/booking
flow, and automation hooks.

This is intentionally different from a one-page landing page. The goal is a
prospect-wowing site that feels like a $10k-$50k agency build, while still being
fast enough to generate from The Homie's native `/design` capability.

When an operator wants to show the work to a prospect, the output can also be
published to an isolated client-preview host. Those previews should feel like
real customer-ready business sites, while the operator keeps a separate table
of contents for tracking links, status, and verification proof.

## Operator Entry Points

- Chat/Telegram: ask for `website-design-homie`, a premium prospect site, or a
  5-10 page local service business website demo.
- CLI: invoke `/design system <slug> "<brief>"` through `thehomie chat`.
- Dashboard: same artifact path as native `/design`; dashboard preview remains
  part of the broader design roadmap.
- API: none direct; generation routes through runtime.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Skill/playbook | `.claude/skills/website-design-homie/SKILL.md` |
| Python/runtime | `.claude/scripts/design/`, `.claude/scripts/runtime/lane_router.py` |
| Chat/router | `.claude/chat/core_handlers.py::handle_design`, `.claude/chat/commands.py` |
| Bundled systems | `.claude/scripts/design/_systems/` |
| Generated artifacts | `vault/memory/design/` |
| Docs/proof | `docs/manual/features/design-capability.md`, this page |

## Operating Contract

Use Website Design Homie when the ask is about a prospect-facing conversion
site, especially for local service businesses such as car detailers, ceramic
coating shops, HVAC, roofers, remodelers, med spas, dentists, lawyers, or other
high-ticket local operators.

Default site shape:

- Home: promise, CTA, proof, high-ticket service entry points.
- Services: revenue-driving services with conversion copy.
- Packages/Pricing: packages, add-ons, FAQs, booking CTA.
- Gallery/Results: before/after proof and process.
- Reviews: review-style proof and objection handling.
- About: owner/operator credibility.
- Service Area: local SEO and mobile-service coverage.
- Contact/Booking: quote form, phone/SMS CTA, missed-call follow-up.
- Optional: niche money pages like Ceramic Coating, Fleet/Commercial,
  Memberships, Blog/Guides.

For car detailers, default to an automotive-premium design system such as
`tesla`, `bmw`, `ferrari`, `lamborghini`, `luxury`, `apple`, or `nike`.

## Client Preview Table Of Contents

Client preview links are the operator-facing index for generated prospect
sites. They are not the same thing as the public business website, and they
should not expose private notes, scraped contact details, internal workflow
language, or unfinished draft copy.

Use one row per preview:

| Column | Required Value |
|---|---|
| Client / prospect | Public-safe name or short label. |
| Vertical | Business category, such as auto detailing, insurance, med spa, legal, or home services. |
| Slug | Clean URL slug used for the preview route. |
| Preview URL | The isolated noindex preview link, usually `<client-preview-host>/<slug>`. |
| Pages | Count and short page map, such as home, services, service detail pages, about, booking, guide. |
| Status | Draft, visual QA, approved, sent, stale, or archived. |
| Verification | Date plus proof that routes, assets, mobile/desktop screenshots, and noindex headers passed. |
| Notes | Only operator-safe next steps. No secrets, private contact details, or fabricated claims. |

Preview routing rules:

- Use one canonical clean URL per client preview, such as
  `<client-preview-host>/<prospect-slug>`.
- Keep preview hosts isolated from production SEO sites when the work is meant
  for one-to-one outreach.
- Enforce `noindex, nofollow` on the preview host unless the operator explicitly
  approves public indexing.
- Do not create duplicate aliases like `/demo/<slug>` and `/client/<slug>`
  unless there is a documented redirect reason.
- Keep the table of contents operator-facing by default. If it is ever shared,
  remove private notes and list only approved public-safe links.

Every preview row should be customer-ready before it is marked sent: no
`demo`, `template`, `mock`, internal-agent language, TODO copy, fake proof,
fake contact details, or claims the prospect did not supply.

## Safety Boundaries

- Do not invent reviews, review counts, awards, license claims, certifications,
  service areas, prices, or photos as facts.
- When real business data is missing, omit the claim or keep the placeholder in
  the private operator brief. Do not ship visible placeholder copy in a
  customer-ready preview.
- Keep generated client/prospect artifacts in the vault artifact path unless the
  operator explicitly asks to move or deploy them.
- External sends, paid ads, publishing, or outreach still require explicit
  approval through the normal default-deny policy.

## How To Run It

```powershell
cd .claude\scripts
uv run thehomie chat -q "/design system tesla \"premium 8-page conversion website for a mobile auto detailer in Dallas with instant quote booking, ceramic coating page, before-after gallery, reviews, service area, and missed-call follow-up\" -Q
```

## How To Test It

```powershell
python .claude/chat/cognition/skills.py --validate-skill .claude/skills/website-design-homie/SKILL.md
cd .claude\scripts
uv run python -m py_compile design\systems.py design\brief.py ..\chat\core_handlers.py
uv run pytest tests/test_design_brief.py -q
```

## Latest Live Proof

- Date: 2026-06-28
- Surface: docs/skill definition only
- Result: Website Design Homie playbook added on top of the already-shipped
  native `/design` capability.
- Proof docs/artifacts: `.claude/skills/website-design-homie/SKILL.md`, this
  manual page.

## Public Export Status

Public-framework safe. Generated prospect artifacts stay private unless
sanitized/exported through the normal framework flow.

## Next Slices

- Add a `/design local-site` or `/website` command wrapper that preloads this
  page architecture.
- Add a car-detailer seed brief with pages, services, quote fields, and outreach
  copy.
- Add a visual QA loop that screenshots desktop/mobile and rejects generic
  one-page output.
