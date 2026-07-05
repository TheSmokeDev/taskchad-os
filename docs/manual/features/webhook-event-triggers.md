# Webhook Event Triggers

Status: shipped (Phase 4), dormant by default
Owner: `.claude/chat/adapters/`
Last updated: 2026-07-04

## What It Does

Lets an external event trigger an agent run. A signed `POST /webhooks/{route}`
from GitHub, Stripe/Svix, GitLab, or any generic API is verified, then either
enqueues a **least-privileged** agent turn (the rendered payload rides
`prefetched_context`) or, in `deliver_only` mode, renders a template straight to
a chat target with zero LLM cost. This closes the scheduled-only gap — the
framework can now react to the outside world, not just the clock.

The adapter is **fully dormant** until routes are configured. With no
`WEBHOOK_ROUTES` set, `main.py` never even constructs it, so nothing binds a
port and nothing is exposed. This is the Hermes v0.18 Tier-1 port.

## Operator Entry Points

| Entry | What it does | Safety boundary |
|---|---|---|
| `POST /webhooks/{route}` | External event ingress. `route` must be a configured route name. | Signed request required; returns 202 (agent lane), 200 (deliver_only / duplicate), or a 4xx/429 verdict. |
| `GET /health` | Read-only liveness check on the webhook server. | No auth, no state. |
| `--webhook` flag | Runs the webhook adapter alone (no other chat adapters). | Only starts if `WEBHOOK_ROUTES` yields ≥1 valid route; errors out otherwise. |

The adapter object is **constructed only** when `get_webhook_settings().routes`
is non-empty. A dormant install has no HTTP listener at all.

## Route Configuration

`WEBHOOK_ROUTES` is a JSON object keyed by route name. Each route needs a secret
(`secret` inline, or `secret_env` naming an env var), and optionally a signature
event filter, a prompt template, and a delivery target. Malformed JSON logs and
leaves the adapter dormant — it never raises.

```json
{
  "github-pr": {
    "secret_env": "MY_GITHUB_WEBHOOK_SECRET",
    "events": ["pull_request"],
    "prompt": "PR {number} {action} in {repository.full_name}: {pull_request.title}",
    "deliver": "log"
  },
  "deploy-notify": {
    "secret": "<your-webhook-secret>",
    "deliver_only": true,
    "deliver": "telegram",
    "deliver_extra": { "chat_id": "<your-chat-id>" },
    "prompt": "Deploy {status} for {service}"
  }
}
```

- `secret` / `secret_env` — a route with an empty effective secret is DROPPED at
  config time (never binds unauthenticated).
- `events` — allowed event types; `[]`/omitted accepts all.
- `prompt` — `re.sub` dot-notation resolver (`{a.b.c}`, `{__raw__}` dumps JSON);
  NEVER `str.format`, so a payload value like `{admin.token}` is emitted
  literally, never re-resolved.
- `deliver` — `"log"` (default), a platform value (`telegram`, `discord`, …), or
  `"github_comment"`. `deliver_only: true` requires a real target (not `log`).
- `deliver_extra` — operator-FIXED target config by default; payload templating
  is opt-in only via `deliver_extra_templated` (delivery-redirect guard).
- `enabled: false` — explicitly rejects events with 403.

## Signature Verification Modes

Signature is checked over the RAW request body, before any JSON parse, through
`hmac.compare_digest` (constant time) only. Header presence selects the mode:

| Provider | Header(s) | What it verifies |
|---|---|---|
| Svix / AgentMail | `svix-id`, `svix-timestamp`, `svix-signature` | base64 HMAC-SHA256 over the signed `msg_id.timestamp.body` tuple. `whsec_` base64 secret or raw fallback; 300s timestamp tolerance; accepts any `v1` entry (rotation-safe). |
| GitHub | `X-Hub-Signature-256` | `sha256=<hex>` HMAC-SHA256 over the raw body. |
| GitLab | `X-Gitlab-Token` | Constant-time compare of the plaintext token against the route secret. |
| Generic | `X-Webhook-Signature` | Hex HMAC-SHA256 over the raw body. |

A secret is configured but no recognized signature header is present → reject
(401). Svix is checked first, then GitHub, then GitLab, then generic.

## Config And Env

| Var | Default | Purpose |
|---|---|---|
| `WEBHOOK_HOST` | `127.0.0.1` | Bind host. Loopback by default. |
| `WEBHOOK_PORT` | `8622` | Bind port. |
| `WEBHOOK_RATE_LIMIT` | `30` | Per-route fixed-window hits per minute (60s window). |
| `WEBHOOK_MAX_BODY_BYTES` | `1048576` | Request body cap (1 MB). |
| `WEBHOOK_IDEMPOTENCY_TTL_SECONDS` | `3600` | Replay-dedup window for a delivery. |
| `WEBHOOK_ALLOW_NON_LOOPBACK` | `false` | Explicit opt-in to bind a non-loopback host (every route must then carry a real HMAC secret). |
| `WEBHOOK_ROUTES` | unset | JSON route table. Unset / empty / malformed → dormant. |

All knobs resolve at call time (Rule 1 None-sentinel), so an env change takes
effect on the next `get_webhook_settings()` call with no module reload.

## Safety Boundaries

- **Dormant by default** — no routes → adapter never constructed → no listener.
- **Loopback-only** unless `WEBHOOK_ALLOW_NON_LOOPBACK=true`; a non-loopback bind
  additionally requires a real (non-`INSECURE_NO_AUTH`) secret on every route.
- **A valid signature is REQUIRED.** A missing/empty secret fails closed with 403
  at request time, not only at startup — direct handler reuse can never become
  an unauthenticated dispatch surface.
- **`INSECURE_NO_AUTH`** is the loopback-only testing sentinel; it is hard-blocked
  off-loopback (route dropped at config, and `connect()` refuses to start).
- **Signed, content-derived idempotency** — the replay key is
  `sha256(raw_body)` for body-only signature modes, and `sha256` of the signed
  `msg_id.timestamp.body` tuple for Svix. The sanitized delivery-id header is
  DISPLAY/AUDIT only, NEVER the replay key (a mutated header can't bypass dedup;
  two distinct bodies can't suppress each other).
- **Body-size cap enforced BEFORE the full body is read** — a Content-Length
  pre-check plus aiohttp `client_max_size` reject over-large bodies (including
  spoofed/chunked) before a full read; a post-read check is the belt-and-suspenders.
- **Fixed-window rate limit** per route (checked after auth so unauthenticated
  floods can't consume the window).
- **Least-privilege delivery** — the agent lane enqueues with `source="tool"`,
  `user_role="viewer"`, `is_piv=False`, and the untrusted-wrapped payload on
  `prefetched_context`, which forces `allowed_tools=[]` + `max_turns=1` +
  TEXT_REASONING. The raw payload never rides `message.text` (it would enter chat
  history / recall as an operator turn).
- **One audit row per terminal verdict** (accepted, every rejection, duplicate,
  delivered, delivery_failed) in `webhook_actions.jsonl`, with secret-shaped
  tokens redacted. Audit writes fail open — the verdict stands regardless.

## Source Of Truth Files

| Layer | Files |
|---|---|
| Adapter | `.claude/chat/adapters/webhook.py` |
| Audit log writer | `.claude/chat/webhook_audit.py` |
| Config resolvers | `.claude/scripts/config.py` (`get_webhook_settings`, `WebhookRoute`, `WebhookSettings`, `webhook_host_is_loopback`, `WEBHOOK_INSECURE_NO_AUTH`) |
| Registration / launch | `.claude/chat/main.py` (dormant registration, `_make_webhook_adapter_resolver`, `--webhook`) |
| Platform enum | `.claude/chat/models.py` (`Platform.WEBHOOK`) |
| Tests | `.claude/scripts/tests/test_adapter_webhook.py` |

## How To Run It

```powershell
# Set a born-clean route table (placeholder secret), then run the adapter alone.
$env:WEBHOOK_ROUTES = '{"github-pr":{"secret":"<your-webhook-secret>","events":["pull_request"],"deliver":"log"}}'
cd .claude/scripts
uv run python ../chat/main.py --webhook
```

The adapter binds `127.0.0.1:8622` by default. Prefer `secret_env` in real use so
the secret lives in `.env`, not inline in `WEBHOOK_ROUTES`.

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_adapter_webhook.py -q
```

58 tests cover signature modes, the replay-key idempotency contract, body-cap
layers, rate limiting, the deliver_only lane, and least-privilege enqueue.

## Latest Live Proof

- Date: 2026-07-04
- Commits: `36f71ae4` + `fc01c853`
- Surface: aggressive post-build adversarial review — Codex PASS, including a
  live svix-id mutation probe (original delivery 202 / mutated-header replay 401).
- Result: 58 tests green.

## Public Export Status

Public-framework safe — mechanism only, placeholder secrets, no personal data.
Public export must still go through `scripts/sanitize.py`.

## Next Slices

- A `source="tool"` prune pass for `chat.db` rows on high-volume routes (per the
  documented Hermes divergence — no webhook-session reaper yet).
- Per-lane cheap-background model selection for the agent-lane turn.
- Additional provider signature modes as new integrations land.
