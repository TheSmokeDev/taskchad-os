# Social Integrations — Meta Graph Direct + Postiz Publishing Lane + Social Tab

Status: Shipped. Optional integration, default-inert (no env config → every
surface degrades to an onboarding state).
Owner slice: `.claude/scripts/social/` (executor + reconcile),
`.claude/scripts/integrations/social_media.py` (Meta Graph client),
`.claude/scripts/integrations/postiz_api.py` (Postiz client),
`dashboard/` (Social tab), `.claude/scripts/social/dashboard_ops.py` (assembly).

The Homie publishes social posts through the operator-approved queue, and each
channel routes to the transport that fits it. Two transports plus two
per-platform paths cover the field:

| Transport (`execution_method`) | Platforms | Why |
|--------------------------------|-----------|-----|
| **`api`** — direct Meta Graph API | **Facebook, Instagram** | Owns the pipe end-to-end; no third-party middleman; most reliable |
| **`postiz`** — self-hosted [Postiz](https://github.com/gitroomhq/postiz-app) | Mastodon, Bluesky, Threads (+ YouTube/TikTok when video ships) | One wrapper reaches ~35 platforms we have no direct client for |
| **`browser`** — visible-Chrome driver | LinkedIn, Reddit | Platforms whose API is gated/heavy; drive the real logged-in session |
| **`manual`** — draft-only | X | Operator policy — never auto-post |

**Design principle:** prefer a direct platform API when the framework can own
it (Facebook/Instagram via Meta Graph); fall back to Postiz for breadth. The
transport is per-channel config in `social/channels.yaml` — the approval
pipeline, gate, and audit trail are identical regardless of transport.

## Why Meta Graph direct for Facebook + Instagram

Postiz's Instagram OAuth connector proved unreliable in practice (stale-token
refresh failures surfacing as `Invalid state` / "Could not add provider —
Invalid API key"). But a Facebook **Page access token** can post to BOTH the
Page *and* the Instagram Business account linked to that Page — so the
framework calls Meta's Graph API directly and skips the connector entirely.
`integrations/social_media.py` already had `post_to_facebook` /
`post_to_instagram`; the channels just route to them via `_dispatch_api`.

- **Facebook** — one call: `POST /{page-id}/feed`.
- **Instagram** — two calls: `POST /{ig-business-account-id}/media` (branded
  quote-card image URL + caption) → `POST /{ig-business-account-id}/media_publish`,
  then resolve the real permalink via `GET /{media-id}?fields=permalink` (the
  media id is NOT the `/p/<shortcode>/` URL — a fixed bug).
- **One token, both platforms** — the Page token reads/writes the linked IG
  account. Mint a **non-expiring** Page token so it's set-and-forget:
  exchange the short-lived token for a long-lived one, then derive the Page
  token from it (`fb_exchange_token` → `GET /{page-id}?fields=access_token`;
  a Page token from a long-lived user token has `expires_at: never`).

### Meta Graph env (`.claude/scripts/.env`)

| Env var | Meaning |
|---------|---------|
| `FACEBOOK_PAGE_ACCESS_TOKEN` | Long-lived Page token (posts to FB Page + linked IG) |
| `FACEBOOK_PAGE_ID` | The Facebook Page id |
| `INSTAGRAM_BUSINESS_ACCOUNT_ID` | The IG Business account id linked to the Page |

Prerequisites: the Instagram account is a **Business/Creator** account **linked
to the Facebook Page**, and the Meta app has `instagram_content_publish` +
`pages_manage_posts` (plus read scopes). The app may stay in **Development
mode** for the app admin's own accounts — no Business Verification needed to
post; verification only affects public visibility of dev-mode content.

## Why Postiz for the rest (and the license boundary)

Postiz is AGPL-3.0. The framework talks to an **unmodified** self-hosted
instance over its Public HTTP API only — no Postiz source is embedded, ported,
or read (payload shapes come from the published API docs, verified live by the
canary). AGPL copyleft does not propagate across a network API boundary, so the
framework's own license is unaffected. Postiz earns its place by reaching ~35
platforms (Mastodon, Bluesky, Threads, Pinterest, Discord, Slack, …) the
framework has no direct client for.

### Postiz env (`.claude/scripts/.env`)

| Env var | Meaning | Default |
|---------|---------|---------|
| `POSTIZ_API_URL` | Backend API origin, e.g. `http://localhost:5000/api` | `""` (not configured) |
| `POSTIZ_API_KEY` | Public API key (sent RAW in `Authorization` — no `Bearer`) | `""` |
| `POSTIZ_TIMEOUT_S` | Total request timeout | `15` |

Bind a Postiz channel in `social/channels.yaml`: `execution_method: postiz` +
`postiz_integration_id` (ids show in the Social tab channel list). Canary
before first use: `cd .claude/scripts && uv run python social/postiz_canary.py`
(status probe → list channels → DRAFT create/list/delete roundtrip, publishes
nothing).

## The publish pipeline (transport-agnostic)

```text
draft (cadence LLM or dashboard compose)      status=draft
  -> operator Approve (Telegram card / dashboard Approve & Post / /social)
  -> social/post_executor.py dispatch          require_integration_action()
       branch on execution_method              default-deny gate + audit rows
       ├─ api     -> integrations/social_media.py (Meta Graph, sync, real URL)
       ├─ postiz  -> integrations/postiz_api.py create_post (+ reconcile poll)
       ├─ browser -> orchestration/browser_executor.py (visible Chrome)
       └─ manual  -> refused (draft-only)
```

Key invariants:

- **Approval pipeline is the ONLY publish path.** Dashboard compose lands as a
  `draft`; nothing publishes without the operator approving that specific post.
  The dashboard **Approve & Post** button (Telegram-button parity) approves and
  immediately dispatches through the full default-deny gate — the tap IS the
  per-post operator confirmation. No direct-publish route exists.
- **The default-deny gate is per-channel** (`social.post_<channel>` actions in
  `integrations/capabilities.py`), not per-transport — flipping a channel's
  transport changes nothing about its authorization.
- **Meta Graph = synchronous** — `post_to_facebook`/`post_to_instagram` return
  the live `post_url` in-band; no reconcile needed.
- **Postiz = optimistic accept + reconcile** — Postiz has no webhooks, so
  `POST /posts` acceptance = enqueued; the row is marked `posted` with
  `external_ref="postiz:<id>"` and the cadence-tick reconcile fills `post_url`
  on confirmation or demotes `posted → failed` (+ Telegram notify) on error.
- **X stays manual** — `social.post_x` ships `default_enabled=False`.

## Dashboard Social tab

`/social` in the dashboard (Workspace section). Nine `admin`-policy routes,
proxied thin through Hono to the Python API; plus an embedded **Studio** view
(the Postiz UI iframed for the Postiz-transport channels — calendar, analytics).

| Route | What it does |
|-------|--------------|
| `GET /api/social/status` | Postiz probe + queue counts + studio URL |
| `GET /api/social/channels` | channels.yaml registry merged with connected integrations |
| `GET /api/social/queue` | Approval-queue rows + status counts |
| `GET /api/social/posts` | Postiz-side posts (state, releaseURL) over a ±window |
| `POST /api/social/compose` | Create a DRAFT queue row (never publishes) |
| `GET /api/social/connect-url` | Fresh Postiz OAuth connect URL (body-only, never audited) |
| `POST /api/social/approve` | Approve & Post: draft → approved → immediate gated dispatch |
| `POST /api/social/reject` | draft → rejected |
| `POST /api/social/reconcile` | On-demand publish-outcome poll (chases the live URL after a Postiz-transport tap) |

## Platform matrix

| Platform | Transport | Notes |
|----------|-----------|-------|
| Facebook | `api` (Meta Graph) | `POST /{page}/feed`; synchronous, real URL |
| Instagram | `api` (Meta Graph) | 2-step media create/publish; branded quote-card auto-generated; real permalink resolved |
| Mastodon, Bluesky, Threads | `postiz` | Minimal `settings.__type`; reconcile fills URL |
| LinkedIn / Reddit | `browser` | Visible-Chrome driver, operator-approved |
| X | `manual` | Draft-only by policy |
| YouTube, TikTok | deferred | Video attachment required — needs executor video support (+ public HTTPS media tunnel for TikTok) |

## Failure modes

| Symptom | Transport | Meaning / Fix |
|---------|-----------|---------------|
| IG/FB dispatch: "API not configured" | api | Missing `FACEBOOK_PAGE_ACCESS_TOKEN` / `FACEBOOK_PAGE_ID` / `INSTAGRAM_BUSINESS_ACCOUNT_ID` in `.env` |
| IG post fails "connected user" / token errors | api | Page token expired or IG↔Page link broken — re-mint the long-lived token; confirm `GET /{page}?fields=instagram_business_account` returns the IG id |
| FB/IG posts publish but hidden from public | api | Meta app in Development mode — needs Business Verification to make dev-published content public (multi-day, Meta-side) |
| Status card: `unreachable` | postiz | Instance down (backend can futex-hang on cold boot) — restart the postiz container after Postgres/Temporal are healthy |
| Status card: `auth failed` | postiz | Public API key wrong or sent with a `Bearer` prefix — the client sends it raw |
| Dispatch: `No postiz_integration_id` | postiz | Channel not bound — copy the id from the Social tab into channels.yaml |
| "Could not add provider — Invalid API key" (Postiz UI) | postiz | Postiz's Instagram *connector* — its `Invalid state` OAuth handshake. Not needed: use the `api` Meta Graph path for Instagram instead |
| Row stuck `posted` with no URL | postiz | Publish still queued in Postiz — reconcile fills it on a later cadence tick |

## Validation map

- `tests/test_social_pipeline.py` — dispatch routing per `execution_method`,
  gate-before-audit ordering, IG quote-card contract; `::TestPostizDispatch` /
  `::TestPostizReconcile` for the Postiz path.
- `tests/test_postiz_api.py` — Postiz client contract (raw-key auth, error
  mapping, payload shapes, not-configured short-circuit).
- `tests/test_social_queue.py` — `external_ref` migration idempotence,
  `posted → failed` transition.
- `tests/test_dashboard_api.py` — route shapes, draft-only compose, connect-URL
  audit hygiene, degraded states.
- `dashboard/server/src/__tests__/social.test.ts` — manifest coverage +
  thin-proxy invariant.
- `social/postiz_canary.py` — live Postiz-instance proof, zero side effects.
