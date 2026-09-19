# GEO Authority Cadence

The GEO Authority cadence turns validated Authority Signal packets into a
small, deterministic editorial schedule. It replaces random LinkedIn topic
selection for this lane; it does not replace the manual `/linkedin` surface.

## Safety boundary

- The job is inert unless `AUTHORITY_ENGINE_ENABLED=true`.
- Research, packets, posts, and articles remain review-first. The scheduler
  cannot approve or publish them.
- Monday, Wednesday, and Friday call the strict Authority Signal to LinkedIn
  queue bridge. That bridge has no autopilot parameter.
- Tuesday creates a tenant Insights package awaiting approval one. It cannot
  create a preview, commit, push, deploy, or submit IndexNow.
- Missing, expired, duplicate, unsupported, or weak packets produce a receipt
  and no filler draft.
- Comments, DMs, invitations, and connection requests are never automated.
- A resource-drop CTA is allowed in at most one queued slot per ISO week.

Socials `HEARTBEAT.md` is read by this job as an editorial checklist. Its
contents cannot alter schedule times, provider budgets, tools, approvals, or
publication authority. Cadence and Firecrawl limits stay in code and config.

## Pacific-time schedule

| Time | Day | Work |
|---|---|---|
| 06:30 | Daily | Bounded Authority Signal refresh |
| 07:00 | Monday | GEO Signal education draft |
| 07:00 | Tuesday | Tenant Insights content package |
| 07:00 | Wednesday | GEO how-to or Myth vs Receipt draft |
| 07:00 | Friday | Repo Field Note |

Friday rotates `hermes-talk`, `taskchad-os`, `hermes-talk`, `geo-skills` over
four weeks. A missing packet for the scheduled repository is a no-op; another
repository is never substituted.

The Windows definition is
`.claude/scripts/setup_authority_cadence_scheduler.ps1`. It registers one task
with both triggers and leaves it disabled unless the operator supplies
`-Enable`. The Python feature flag is still required after task enablement.
The installer checks the Windows Pacific timezone, uses the hidden launcher,
and snapshots an existing definition before replacement. The CLI returns a
nonzero exit for operational failures; ordinary no-signal/duplicate outcomes
remain quiet successful no-ops. Inspect the structured run receipt as well as
the scheduler status.

The private deployment installs its tenant Insights publication bridge. The
sanitized public framework intentionally omits that tenant-specific bridge; a
Tuesday slot without an installed bridge records a no-op instead of inventing
an article or publication path.
The private bridge checks the configured checkout's actual CLI, loader, route,
and package command before spending model/media work or issuing preview buttons.

Telegram delivery is separate from the main bot's lifecycle. A scheduled sender
can deliver a review card while the main bot is off, but the approval buttons
require the running, updated main bot. Starting that bot is a separate operator
choice; scheduler activation must not silently change its desired-state switch.

## Operator commands and approvals

- `/signal authority status` shows the last bounded run and Firecrawl ledger.
- `/signal authority refresh` runs the same gated research path on demand.
- `/signal authority queue` lists only validated, unexpired packets.
- `/social outcome <id> metric=value ...` records observed movement without
  causal attribution; `/social outcome list [id]` reads it back.

LinkedIn cards bind the post id, revision, content digest, and media digest.
Changing copy or media invalidates every earlier Telegram button. A successful
browser submit is not `posted` until the `View post` permalink and screenshot
are persisted; ambiguous submission enters non-retryable
`verification_required`.

The Insights bridge uses two different authenticated Telegram buttons:

1. `Approve for Preview` sends the exact Markdown, two canonical source-packet
   JSON files, 1200x630 OG image, and 1080x1350 LinkedIn card into the tenant's
   isolated-worktree validator. It may build and capture a local screenshot,
   but cannot commit, push, deploy, index, or touch production.
2. `Publish: commit + push + deploy + IndexNow` is created only after preview
   proof. It binds the preview artifact hash, ordered source-packet hashes, and
   base commit. A partial or incomplete publication becomes
   `verification_required` and is never retried automatically.

## Manual outcomes

`social.outcomes` stores append-only, idempotent evidence and mirrors a
deterministic note into the Socials persona's isolated `experience/` lane.
Supported observations cover:

- post saves, substantive comments, profile views, and qualified DMs;
- article sessions, GSC impressions, and AI citations;
- repository views, clones, stars, forks, installs, issues, and contributors.

Repository values use explicit `*_delta` metric names. Every GitHub row is
labeled `correlated_movement_not_conversion`; no record claims causal
conversion attribution. Experience notes explicitly grant no capabilities,
tools, autonomy, or publication authority.

Example parser input for the `/social` adapter:

```text
outcome post:42 post_saves=8 substantive_comments=2 note="seven-day observation"
```

The command adapter calls `social.outcomes.record_outcome`; it must not write
directly to the JSONL or persona memory file.
