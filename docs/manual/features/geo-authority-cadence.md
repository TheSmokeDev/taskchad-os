# GEO Authority Cadence

The GEO Authority cadence turns validated Authority Signal packets into a
small, deterministic editorial schedule. It replaces random LinkedIn topic
selection for this lane; it does not replace the manual `/linkedin` surface.

## Safety boundary

- The job is inert unless `AUTHORITY_ENGINE_ENABLED=true`.
- Research, packets, posts, and articles remain review-first. The scheduler
  cannot approve or publish them.
- Every day calls the strict Authority Signal to LinkedIn queue bridge. That
  bridge has no autopilot parameter.
- Tuesday additionally creates a tenant Insights package awaiting approval
  one. It cannot create a preview, commit, push, deploy, or submit IndexNow.
- Missing, expired, duplicate, unsupported, or weak packets produce a receipt
  and no filler draft.
- Technically valid but socially weak packets are also rejected before copy:
  source-as-title, missing source date, incomplete URL, or no high-confidence
  primary claim are blocking defects.
- Comments, DMs, invitations, and connection requests are never automated.
- A resource-drop CTA is allowed in at most one queued slot per ISO week.
- Authority LinkedIn drafts require media. Image failure blocks the draft rather
  than degrading into a text-only approval card.

Socials `HEARTBEAT.md` is read by this job as an editorial checklist. Its
contents cannot alter schedule times, provider budgets, tools, approvals, or
publication authority. Cadence and Firecrawl limits stay in code and config.

## Pacific-time schedule

| Time | Day | Work |
|---|---|---|
| 06:30 | Daily | Bounded Authority Signal refresh |
| 07:00 | Monday | GEO Signal education draft |
| 07:00 | Tuesday | GEO education draft + Tenant Insights content package |
| 07:00 | Wednesday | GEO how-to or Myth vs Receipt draft |
| 07:00 | Thursday | GEO education draft |
| 07:00 | Friday | Repo Field Note |
| 07:00 | Saturday | GEO how-to draft |
| 07:00 | Sunday | GEO education draft |
| 08:00 | Daily | Retry only if copy or required media failed at 07:00 |

Friday prefers `hermes-talk`, `taskchad-os`, `hermes-talk`, `geo-skills` over
the four-week rotation. If that exact repository has no fresh verified event,
the lane may use the strongest fresh validated GEO education packet instead;
it never invents a repository update or autobiographical filler. Any day with
no validated packet remains a truthful no-op.

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

If a new authority draft is queued but Telegram transport fails, a later
same-day tick retries delivery of that exact post/revision after rechecking
editorial integrity. It never regenerates copy or media, consumes another
resource allowance, or publishes. Revised, expired, terminal or explicitly
no-delivery drafts are not automatically sent. Historical ambiguous delivery
records are not retroactively resent. Transport/runtime failures return a
nonzero scheduler result; ordinary quality rejection remains a no-filler no-op.

## Copy and image quality contract

Socials writes original methods-first prose in six formats: tactical teardown,
compact workflow, myth correction, decision framework, repository demonstration,
and resource drop. It receives up to 14 actually delivered drafts to vary
openings, examples and structure. On initial migration, confirmed posted legacy
rows provide labeled history; pending rows are not assumed delivered.

Research packets remain `authority-signal/v1`. A separate
`authority-editorial/v1` package maps public factual statements to evidence and
stores the public body, format, actual resource binding, visual brief and review.
An independent model-only reviewer classifies every caption sentence and visible
image field. Original writing and independent review use the configured quality
background model through the existing Socials runtime; other social tasks retain
their configured fast default. Writer and reviewer cannot call tools. Checks retain
privacy, freshness, length, prohibited-claim and resource-digest protection.
Failed generation or review creates no canned fallback. First-person editorial
judgment is welcome; past work, clients or results require matching receipts.
One fresh regeneration is allowed for an otherwise valid object missing required
root fields, sharing the same two-attempt total budget as editorial rework.
Privacy, protocol and provider failures do not trigger this recovery. Repository
demonstrations may include the exact configured GitHub destination as a useful
CTA; this is not a research-source footer.

Source links, author narration and borrowed experiment percentages stay out of
public copy. Telegram sends a separately labeled internal evidence note, the
complete public caption, the image, then revision-bound buttons. The browser
receives only the queue row's public caption and image. Comment replies use the
same evidence reviewer and remain drafts; it cannot invent claims about a
commenter's motives, competence, results or use of AI.

The existing SQLite queue has additive editorial-package history keyed by post
ID and revision. Copy/media/full-package digests prevent stale reviews from
authorizing changed artifacts. Copy or image revisions regenerate and review
the pair atomically. A unique ISO-week reservation is consumed only for a queued
post that actually includes a registered resource. The promised artifact's
title and bytes are rechecked; revising away a resource does not erase history.
Older unreviewed authority drafts must be regenerated before approval.

Images run through `social.authority_image_factory`. That adapter re-hashes the
full multilingual 511-case corpus, excludes duplicate bodies and the two catalog
notices without original prompts, and builds a diverse 25-case shortlist. Five
complementary reference roles then shape the concept; recent delivered image
metadata discourages repeating the same cases. Full source excerpts remain in
local-only grounding files, while IDs/hashes and roles enter the public pack.

The approved art direction is tactile, image-led and bold: physical materials,
yellow/orange display typography and one clear visual argument. The monumental
reference-volume preview is a style benchmark, not a subject to reuse every day.
Avoid default white wireframes, generic AI brains, mascots or fabricated proof.
Gray text bars, skeleton document layouts and illegible pseudo-handwriting are
blocking defects, not acceptable substitutes for missing approved wording. A
prop must communicate through approved real text or the scene must be redesigned.
Recent safe visual concepts accompany the writing history to vary settings too.
The image subprocess uses the call-time
`IMAGEGEN_CODEX_MODEL` override or the host-compatible `gpt-5.5` default so it
does not inherit an interactive model unsupported by the installed CLI. The
final bitmap is contained on a 1080x1350 canvas without cropping baked text;
the original is preserved. The final render instruction explicitly binds 4:5
portrait composition. Landscape/square results or more than 17 percent total
padding are rejected, not disguised by large blank bars. Caption and image wording are bound deterministically
before rendering. `authority_visual_review` sends validated PNG/JPEG bytes to
the supported Claude SDK no-tools image transport, not merely a filename or OCR
transcript. It has no fallback to adapters that cannot prove image carriage.
Every strict model-only SDK request explicitly disables settings, hooks, MCP,
skills and provider tools. Magic bytes, file size, static image decoding, exact
visible wording, caption/resource agreement and image digests are checked.
Windows OCR remains an optional diagnostic, not the publication authority.
Aesthetic approval remains the operator's in Telegram. Missing media or a failed
review blocks queue creation; no text-only authority approval is offered.
The local Codex image subprocess is pinned to a compatible tool-capable model
because inheriting an unsupported interactive model fails before imagegen. A
copy or media failure remains retryable and the 08:00 trigger makes one bounded
same-day retry; a successful 07:00 draft makes that trigger a no-op.

Exa's labeled text result format is parsed before any bare-URL fallback. Source
titles, dates, and highlights remain bound to their URL; URL-only rows are
discarded. arXiv, ACL Anthology, ACM, IEEE, OpenReview, and PMLR are classified
as primary research. Primary practical GEO research gets a one-year evergreen
window, while platform changes and vendor/practitioner items keep the configured
short freshness window.

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
