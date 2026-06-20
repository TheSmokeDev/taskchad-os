# Social Post Pipeline

Status: Shipped (#80), merged to master; default-denied, operator-gated per send
Owner: `.claude/scripts/social/` (queue, channels, executor, cadence, audit) + `.claude/chat/` (the `/social` command)
Last updated: 2026-06-19

## What It Does

The Social Post Pipeline is a default-deny content queue: an idea becomes an
AI-drafted post, the operator approves and schedules it, and only then does a
gated dispatcher land it on a channel — with a pre-send audit row on every
attempt. It adds the decoupled **generate → approve → schedule → dispatch** loop
and per-channel configuration that the single-shot
[Social-Write Executor](social-write-executor.md) does not have.

The two are complementary, not competing:

- **Social-Write Executor** (`/linkedin_post`, `/reddit comment`, …) — one
  operator command lands exactly one write, approval carried in the command. No
  queue, no schedule, no batch.
- **Social Post Pipeline** (`/social …`) — a queue with draft/approve/reject
  states, optional scheduling, optional autonomous drafting (cadence), and
  multi-channel dispatch (browser **and** API). It reuses the same
  `BrowserExecutor` under the hood for LinkedIn/Reddit.

## Operator Entry Points

- Chat/Telegram: the `/social` command family (admin-only). The registry
  description lists the core verbs; the handler supports nine:

| Subcommand | Syntax | Does |
|---|---|---|
| `status` | `/social status` | Post counts by status + the channel table. |
| `queue` | `/social queue` | The most recent posts with `[STATUS] #id channel — title`. |
| `draft` | `/social draft <channel> <idea>` | Generate an AI draft for a channel; returns the post id + preview + approval instruction. |
| `approve` | `/social approve <id>` | Move a draft to `approved`. Writes an audit row. |
| `reject` | `/social reject <id> [reason]` | Move a draft to `rejected`. Writes an audit row. |
| `post` | `/social post <id>` | Dispatch one approved post now (through the gate). |
| `schedule` | `/social schedule <id> <ISO datetime>` | Set `scheduled_for` for a later dispatch. |
| `dispatch-due` | `/social dispatch-due` | Dispatch every approved post whose schedule has passed. |
| `cadence` | `/social cadence` | Show the per-channel cadence config. |

- CLI: the cadence tick is runnable directly — `uv run python -m social.cadence --dry-run`.
- Dashboard/API: none yet (chat + vault surface).

## The Loop

```
idea ──/social draft──▶ DRAFT ──/social approve──▶ APPROVED ──(/social schedule)──▶ scheduled
                                                        │
                          /social post (now) ───────────┼────────▶ require_integration_action ──▶ post ──▶ POSTED
                          /social dispatch-due (≤ now) ──┘                 (gate, per send)
```

- **Draft** — `social/draft_generator.py` builds voice-matched copy and stores a
  `draft` row (`social/service.py` → `social/db.py`). Drafts come from a manual
  `/social draft` or, if enabled, the cadence tick.
- **Approve / reject** — `/social approve|reject` is the *only* path out of
  `draft`; the operator is the sole approver.
- **Schedule** — optional `scheduled_for` timestamp. Without it, a post is
  dispatched only by an explicit `/social post`.
- **Dispatch** — `social/post_executor.py` routes each approved post through the
  default-deny gate before any external call (see below).

## Channels

Channels are config-driven (`social/channels.yaml`, loaded at call time). The
shipped set:

| Channel | Method | Cadence | Notes |
|---|---|---|---|
| `linkedin` | browser | enabled (24h) | Drives the existing visible-Chrome `BrowserExecutor`. |
| `facebook` | api | enabled (24h) | Drafts a caption; posting via Graph API (`FACEBOOK_PAGE_ACCESS_TOKEN` + `FACEBOOK_PAGE_ID`). |
| `x` | manual | enabled (12h) | **Draft-only by policy** — `manual` method + the `post_x` capability is hard-disabled. Drafts are delivered, never auto-posted. |
| `reddit` | browser | off | Visible-Chrome `BrowserExecutor`. |
| `instagram` | api | enabled (24h) | Drafts a caption **plus a best-effort generated scene image** (codex CLI image tool; saved under `<project>/.claude/data/social_images/`, caption-only fallback). Posting via Meta Graph API also needs a business account id + the image at a public URL. |
| `discord` | manual | off | Placeholder; `manual` never auto-posts. |

## Default-Deny Safety

This pipeline can post to the outside world as the operator, so the gate is the
load-bearing invariant. Verified in code:

- **Every external write routes through the canonical gate, before the call.**
  Both legs of `social/post_executor.py` call
  `require_integration_action("social", "post_<channel>", surface="operator_confirmed", …)`
  *before* the external request — the API leg (`_dispatch_api`) and the browser
  leg (`_dispatch_browser`). A blocked action raises `IntegrationPolicyError`,
  which is audited `blocked` and re-raised so it is counted, never swallowed.
- **Pre-send audit.** An audit row with `outcome="pending"` is written *before*
  the external call on both legs — a posted-with-no-record gap is impossible.
- **Dispatch only touches approved + scheduled posts.** `social/db.py`
  `list_due()` is `WHERE status='approved' AND scheduled_for IS NOT NULL AND
  scheduled_for <= now`. A post with no schedule is never auto-dispatched.
- **Cadence never approves.** `social/cadence.py` `run_cadence_tick()` only
  *drafts* and then calls `dispatch_due_posts()` for already-approved posts. It
  never calls `approve()`. The autonomous loop cannot post anything the operator
  did not approve.
- **Cadence is off by default.** `SOCIAL_CADENCE_ENABLED` defaults to `false`;
  the whole autonomous loop is opt-in.
- **X is hard-disabled.** The `post_x` capability is declared
  `default_enabled=False` ("X is draft-only per operator policy") and the channel
  method is `manual`, so X can never auto-post on either path.
- **Capabilities are declared default-deny.** All five post actions
  (`post_linkedin`, `post_facebook`, `post_x`, `post_reddit`, `post_instagram`)
  are registered in `.claude/scripts/integrations/capabilities.py` with effect
  level `external_post` and exposure `operator_confirmed` — the same canonical
  contract the rest of the framework's mutating integrations use.

## Cadence

`run_cadence_tick()` (`social/cadence.py`): if `SOCIAL_CADENCE_ENABLED` is off it
returns immediately. Otherwise, for each cadence-enabled channel whose interval is
due it picks a topic from the channel's `topic_pool` and generates a **draft**
(never an approval), records `last_draft_at`, then calls `dispatch_due_posts()` to
send any already-approved, already-scheduled posts. State lives in
`STATE_DIR/social-cadence-state.json`.

**Scheduling is wired.** A daily Windows Task Scheduler job (07:00) runs the tick
via `run_social_cadence.bat` → `social/cadence.py`. The loop stays **off by
default** (master switch `SOCIAL_CADENCE_ENABLED`, default `false`). To turn it on:
set `SOCIAL_CADENCE_ENABLED=true`, then register the job once with
`setup_social_cadence_scheduler.ps1` (it prints the task name plus the
disable/remove commands). To stop it: set the flag `false`, or disable/unregister
the task. A channel that fails to draft is skipped, never fatal.

## Audit Trail

Every draft, approve, reject, and send attempt appends a sanitized JSONL row via
`social/audit.py` (`append_social_audit_record`) to `.claude/data/social_posts.jsonl`
(git-ignored, inside the sanitizer `DENY_DIR`). Fields: timestamp, channel,
action, post_id, outcome, operator, an 80-char body preview, error, post_url.
Outcomes: `created`, `approved`, `rejected`, `pending` (pre-send), `success`,
`failed`, `blocked` (gate refused). URLs are redacted; no cookies, tokens, or
query strings are logged.

## Data Model

`social_post_queue` (SQLite, `social/db.py`) with a `CHECK` on `status` and a
strict transition map (`social/models.py`):

```
draft ──approve──▶ approved ──post success──▶ posted (terminal)
  └──reject──▶ rejected (terminal)        └──dispatch fail──▶ failed (terminal)
```

Indexes on `status`, `channel`, and a partial index on `scheduled_for`. Key
columns: `scheduled_for` (nullable ISO 8601), `approved_at`, `posted_at`,
`post_url`, `rejection_reason`, `error`, `audit_id`.

## Config Knobs

Env-var names only; values live in `.claude/scripts/.env`.

| Env var | Default | Meaning |
|---|---|---|
| `SOCIAL_CADENCE_ENABLED` | `false` | Master switch for autonomous drafting. |
| `FACEBOOK_PAGE_ACCESS_TOKEN`, `FACEBOOK_PAGE_ID` | — | Facebook Graph API posting. |
| `INSTAGRAM_BUSINESS_ACCOUNT_ID` | — | Instagram Graph API posting. |
| `LINKEDIN_EMAIL`, `LINKEDIN_PASSWORD` | — | Used by the visible-Chrome browser path (not an API). |
| `X_API_KEY`, `X_API_SECRET` | — | Present but unused — `post_x` is disabled by policy. |

Channel cadence, method, and topic pools live in `social/channels.yaml`, not env.
The shipped cadence-enabled set is LinkedIn (24h), X (12h), Facebook (24h), and
Instagram (24h); Reddit and Discord stay off.

Instagram drafts get a best-effort scene image via the codex CLI image tool
(`video_imagegen`) — no env knob; it degrades to caption-only when codex image
generation is unavailable. Images are saved under
`<project>/.claude/data/social_images/` and the path is referenced in the draft.

## How To Run It

```powershell
cd .claude/scripts

# Inspect the queue + channels (also via /social in chat)
uv run python -c "from social.service import SocialPostService; print(SocialPostService().count_by_status())"

# Cadence dry run (no drafts written)
uv run python -m social.cadence --dry-run
```

In chat the full loop is: `/social draft linkedin "<idea>"` → `/social approve <id>`
→ `/social post <id>` (or `/social schedule <id> <iso>` then `/social dispatch-due`).

## How To Test It

```powershell
cd .claude/scripts
uv run pytest tests/test_social_queue.py tests/test_social_channels.py tests/test_social_pipeline.py -q
```

92 tests: queue schema + transitions, channel registry, end-to-end channel loops
(LinkedIn browser, Facebook API, X draft-only), the default-deny proofs (gate
called before the external write on both legs, cadence creates drafts only,
pre-send audit precedes the post, X disabled), the direct-API path, and audit
JSONL shape.

## Latest Live Proof

- Date: 2026-06-20
- Surface: scheduled cadence live + merged PR #80 (squash commit `1ab4386e`).
- Result: 92/92 social tests green; the daily cadence is wired and proven to draft
  LinkedIn / X / Facebook / Instagram (Instagram with a codex-generated image),
  all draft-only with nothing posted. Two runtime bugs fixed (cadence `.env` load
  + `draft_generator` runtime call) in commits `60dd6784` / `7351d697`.
- Proof: a cadence run produced one draft per enabled channel plus a saved image;
  the merged PR review thread (round-3 re-review verdict APPROVE).

## File Ownership Map

| File | Responsibility |
|---|---|
| `social/models.py` | `SocialPost` dataclass + the status transition map. |
| `social/db.py` | `social_post_queue` schema, CRUD, the `list_due()` gate query. |
| `social/service.py` | Business logic: create/approve/reject/mark, transition validation. |
| `social/channels.py` / `channels.yaml` | Config-driven channel registry. |
| `social/draft_generator.py` | Voice-matched AI draft copy; `_generate_social_image()` attaches a best-effort codex-generated image to Instagram drafts. |
| `social/post_executor.py` | Gated dispatch: `require_integration_action` + pre-send audit + API/browser/manual routing. |
| `social/cadence.py` | Opt-in autonomous draft loop (never approves). |
| `run_social_cadence.bat` | Scheduled-job runner — the daily Task Scheduler job invokes this to run one cadence tick. |
| `setup_social_cadence_scheduler.ps1` | One-time Windows Task Scheduler registration for the daily 07:00 cadence. |
| `social/audit.py` | Append-only sanitized JSONL audit. |
| `.claude/scripts/integrations/capabilities.py` | The `social` action declarations (default-deny). |
| `.claude/scripts/integrations/social_media.py` | Platform API wrappers (`post_to_platform`). |
| `.claude/chat/commands.py`, `core_handlers.py` | `/social` registration + `handle_social`. |

## Public Export Status

Public-safe by construction (mechanism only, placeholder data, no account IDs, no
secret values). Because `docs/` is in the sanitizer `DENY_DIRS`, this page ships
publicly only through an explicit per-file entry in the sanitizer `INCLUDE_FILES`
list. Export goes only through `scripts/sanitize.py`; never copy files by hand.

## Next Slices

- A dashboard surface for the queue.
- Prove the token-backed direct-API legs (Facebook/Instagram) against live creds.
- Migrate the Reddit write onto the shared `BrowserExecutor` (shared with the
  Social-Write Executor).
