# Social Cadence Draft Delivery

Status: Shipped, default-denied, operator-gated per tap
Owner: `.claude/scripts/social/` (cadence + notify + executor) plus `.claude/chat/` (button routing + `/social` handler)
Last updated: 2026-06-20

## What It Does

The cadence loop used to draft a post and then drop it — the draft landed in a
SQLite queue (or, in the retired legacy nudge job, on disk as a `.md` file plus a
desktop toast) and never reached the operator's chat. The framework generated
content the operator could not see without running a command.

This slice closes that gap. Now every cadence-generated draft is **delivered to
the operator's Telegram as a card with inline Approve / Edit / Reject buttons**,
and an approved draft auto-posts through the existing gated browser-write
executor. The second brain finally *shows you* the draft instead of filing it
silently.

It builds on two existing slices and changes neither's contract:

- The [Social Post Pipeline](social-post-pipeline.md) owns the queue, the
  channel registry, the gated dispatcher, and the `/social` command.
- The [Social-Write Executor](social-write-executor.md) owns the visible-Chrome
  `BrowserExecutor` plus the injected agent-browser driver that lands one write.

This page is about the new delivery + one-tap-approval seam that joins them.

## The Full Loop

```
cadence tick ──generate_draft──▶ queue row (status=draft)
        │
        ├──deliver_draft_to_telegram──▶ Telegram card  [✅ Approve & Post] [✏️ Edit] [❌ Reject]
        │                                       │
        │                          operator taps a button
        │                                       │
        ├── Approve ─▶ /social approve (status=approved, audit) ─▶ /social post ─▶ gate ─▶ BrowserExecutor ─▶ posted (audit + screenshot)
        ├── Edit ────▶ full draft body returned for manual copy/tweak
        └── Reject ──▶ /social reject (status=rejected, audit)
```

1. **Draft.** `social/cadence.py` `run_cadence_tick()` runs one tick: for each
   cadence-enabled channel whose interval is due it picks a topic from the
   channel's `topic_pool` and calls `generate_draft(...)`, which persists a
   `draft` row in the queue DB.
2. **Deliver.** Immediately after a draft is created, the tick loads the post via
   `SocialPostService.get_post(pid)` and calls
   `social/notify.py` `deliver_draft_to_telegram(post)`. This is **fail-open**:
   a delivery miss is logged and never blocks the tick, un-counts the draft, or
   raises — the draft is already safely persisted in the queue.
3. **Card.** `notify.py` posts directly to the Telegram Bot API
   (`sendMessage`), cross-process safe because the cadence cron runs in a
   SEPARATE process from the chat bot. The card is plain text (no `parse_mode`,
   so arbitrary generated content can never break Telegram entity parsing),
   capped at the 4096-char Telegram limit, with an inline keyboard whose
   `callback_data` values are `social:approve:<id>`, `social:edit:<id>`, and
   `social:reject:<id>`.
4. **Tap.** When the operator taps a button, the Telegram adapter emits the
   callback back into the running bot as a `__button:social:<action>:<id>`
   interaction. `router.py` `_handle_social_button` routes it to the existing
   `/social` handler.
5. **Approve / Edit / Reject.**
   - **Approve** runs `/social approve <id>` and then — *only if the post's real
     DB status is now `approved`* (`_social_post_is_approved`, checked against
     the DB, never by sniffing the reply string) — runs `/social post <id>`,
     which routes through the default-deny gate and the `BrowserExecutor`.
   - **Edit** returns the full draft body so the operator can copy, tweak, and
     post it manually, then clear the queue row with `/social reject <id>`.
   - **Reject** runs `/social reject <id>` and the draft is terminal.

## Default-Deny Write Doctrine

This slice can post to the outside world as the operator, so the gate is the
load-bearing invariant. The new seam adds one rule to the existing default-deny
family: **the auth-checked button tap IS the operator approval.**

- **Only a genuine button interaction can trigger a write.** `router.py`
  `_handle_social_button` refuses any `__button:social:*` whose
  `raw_event.interaction_type` is not `"button"`. The Telegram adapter verifies
  the allowed user id and stamps `interaction_type="button"` before emitting the
  callback; a raw `__button:social:...` string typed through any other ingress
  (CLI or web relay) lacks that marker and is refused with
  *"Social actions only run from the draft buttons."* Typed text can never
  synthesize an approval.
- **Approve is gated against real DB state, not the reply string.** After
  `/social approve`, the router re-reads the post and dispatches only when
  `post.status == "approved"`. `_social_post_is_approved` fails CLOSED on any
  error — no approval, no post.
- **The external write still routes through the canonical gate.** The
  `/social post` leg calls
  `require_integration_action("social", "post_<channel>", surface="operator_confirmed", …)`
  before the external call, writes a pre-send audit row, and re-raises a blocked
  action (see [Social Post Pipeline](social-post-pipeline.md) → Default-Deny
  Safety). The button tap does not bypass any of that — it just supplies the
  approval the gate requires.
- **The executor never re-gates.** `BrowserExecutor.dispatch` receives an
  already-approved task, confirms the physical visible-Chrome readiness envelope
  (Rule 2 — physical state, not a cached flag), and refuses + audits `failed`
  when not ready. It never inspects a token and has no approval path.
- **Cadence never approves.** `run_cadence_tick()` only *drafts* and *delivers*,
  then calls `dispatch_due_posts()` for already-approved, already-scheduled
  posts. The autonomous loop cannot post anything the operator did not tap to
  approve.

## Channels

Channel behavior is config-driven in `social/channels.yaml` (loaded at call
time). The `execution_method` decides what an approved post does:

| Channel | Method | Cadence | On Approve |
|---|---|---|---|
| `linkedin` | browser | enabled (24h) | Auto-posts through the visible-Chrome `BrowserExecutor`. |
| `reddit` | browser | off | Visible-Chrome `BrowserExecutor` (cadence off by default). |
| `facebook` | api | enabled (24h) | Posts via the gated direct-API leg. |
| `instagram` | api | enabled (24h) | Posts via the gated direct-API leg (draft may include a generated scene image). |
| `x` | browser | enabled (12h) | **Primo Agent** — Telegram approval dispatches through the visible `@primo_agent` X session on CDP 18222. |
| `discord` | manual | off | Placeholder; `manual` never auto-posts. |

`manual` channels deliver a draft card like any other, but Approve cannot land a
write (the dispatcher marks a manual channel failed with a "copy and post
manually" reason). For those, the Edit button is the path: it hands back the full
body to paste yourself.

## Source Of Truth Files

| Slice | Files |
|---|---|
| Draft delivery | `.claude/scripts/social/notify.py` (`deliver_draft_to_telegram`, `_build_card_text`, `_build_reply_markup`, `_telegram_credentials`, `_redact`) |
| Cadence wiring | `.claude/scripts/social/cadence.py` (`run_cadence_tick` — draft → load → deliver, fail-open) |
| Channel config | `.claude/scripts/social/channels.yaml`, `.claude/scripts/social/channels.py` |
| Gated dispatch | `.claude/scripts/social/post_executor.py` (`dispatch_post`, `_dispatch_browser`, `_dispatch_api`, `dispatch_due_posts`) |
| Queue + states | `.claude/scripts/social/service.py`, `.claude/scripts/social/models.py` (`draft → approved → posted/failed`, `draft → rejected`) |
| Button routing | `.claude/chat/router.py` (`_handle_social_button`, `_social_post_is_approved`, `_social_edit_reply`) |
| `/social` handler | `.claude/chat/core_handlers.py` (`handle_social`) |
| Injected driver | `.claude/chat/social_write_driver.py` (`make_social_write_driver`, `AgentBrowserSocialWriteDriver`) |
| Visible-Chrome executor | `.claude/scripts/orchestration/browser_executor.py` (`BrowserExecutor.dispatch` — does NOT re-gate; the task is pre-approved by the caller) |

## `/social` Command Reference

The buttons drive the same `/social` subcommands an operator can run by hand
(admin-only):

| Subcommand | Syntax | Does |
|---|---|---|
| `status` | `/social status` | Post counts by status + the channel table (method + cadence on/off). |
| `queue` | `/social queue` | The most recent posts as `[STATUS] #id channel — title`. |
| `draft` | `/social draft <channel> <idea>` | Generate an AI draft; returns the post id + preview + approval instruction. |
| `approve` | `/social approve <id>` | Move a draft to `approved`. Writes an audit row. |
| `reject` | `/social reject <id> [reason]` | Move a draft to `rejected`. Writes an audit row. |
| `post` | `/social post <id>` | Dispatch one approved post now, through the gate. |

The Approve button is exactly `/social approve <id>` followed by `/social post
<id>`; the Reject button is `/social reject <id>`; the Edit button returns the
body (no `/social` write).

## Config / Env Knobs

Env-var names only; values live in `.claude/scripts/.env`.

| Env var | Default | Meaning |
|---|---|---|
| `SOCIAL_CADENCE_ENABLED` | `false` | Master switch for the autonomous draft-and-deliver loop. Off by default. |
| `TELEGRAM_BOT_TOKEN` | — | Used by `notify.py` to post the draft card directly to the Telegram Bot API. Without it, delivery logs "creds not configured" and returns `False` (fail-open). |
| `TELEGRAM_ALLOWED_USER_IDS` | — | The first id is the delivery chat. The Telegram adapter also checks it before stamping a tap as a genuine `button` interaction. |
| `ORCHESTRATION_API_BASE_URL` | `http://127.0.0.1:4322` | Relevant only when an approved post's dispatch path reaches the orchestration API; the cadence delivery seam itself posts straight to Telegram. |
| `ORCHESTRATION_API_TOKEN` | — | Bearer token for the orchestration API when one is set server-side. |

Channel cadence, method, and topic pools live in `social/channels.yaml`, not env.

### Legacy `linkedin_nudge.py` Retirement

The legacy `linkedin_nudge.py` job — which wrote drafts to disk as `.md` files
plus a desktop toast and never reached the operator's chat — is **retired
(superseded)** by this slice. It is default-OFF behind `LINKEDIN_NUDGE_ENABLED`
and its scheduled task is disabled. Cadence draft delivery replaces it; do not
re-enable the nudge job to ship LinkedIn drafts.

## How To Run It

```powershell
cd .claude/scripts

# Cadence dry run (no drafts written, no delivery)
uv run python -m social.cadence --dry-run

# Inspect the queue + channel cadence state (also via /social in chat)
uv run python -c "from social.service import SocialPostService; print(SocialPostService().count_by_status())"
```

With `SOCIAL_CADENCE_ENABLED=true`, a live tick drafts one post per due channel
and delivers each as a Telegram card. Tap **Approve & Post** to land a
browser/api channel, **Edit** to copy-and-post a manual channel, or **Reject** to
discard. The visible-Chrome CDP session must be reachable for a LinkedIn/X/Reddit
auto-post (confirm with `agent-browser --cdp 9222 stream status`); if it is not
ready the executor refuses and audits `failed` without driving.

## Known Limitations / Follow-Ups

- **LinkedIn composer flow is unverified against the live UI.** The browser
  driver's LinkedIn composer steps (the shadow-DOM composer textbox and submit
  references) are documented in the drive docstrings but must be verified during
  the first SUPERVISED real auto-post. Until then, treat a LinkedIn Approve tap
  as a supervised, selector-verify run.
- **CLOSED (2026-07-13): dispatch is CAS-guarded.** Every dispatch ingress
  (approve tap, `/social post`, cadence cron) must win an atomic `claimed_at`
  claim on the still-`approved` row before driving (`social/db.py
  claim_post`). A double-tap or a tap racing the cron loses the CAS and is a
  no-op — the operator sees "already being posted."
- **CLOSED (2026-07-13): receipts are delivered back to Telegram.** The
  Browser Homie runner (`social/browser_homie_runner.py`) sends a
  posted/failed receipt cross-process via `social/notify.py
  send_text_to_telegram` after every dispatch, and the cadence tick's
  stale-claim sweep notifies when a claimed post never finished.
- **Approve-tap dispatch now runs OUT OF PROCESS.** The bot claims the row and
  spawns the detached Browser Homie runner (`spawn_detached`); the browser
  drive can no longer block the chat event loop (the 2026-07-13 wedge class).
  See `docs/manual/features/social-post-pipeline.md` → Browser Homie Runner.

## Public Export Status

Public-safe by construction (mechanism only, placeholder data, no account ids,
handles, or secret values). Because `docs/` is in the sanitizer `DENY_DIRS`, this
page ships publicly only through an explicit per-file entry in the sanitizer
`INCLUDE_FILES` list. Export goes only through `scripts/sanitize.py`; never copy
files between repos by hand.
