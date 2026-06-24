# PRD Exemplar — Slack Threaded Messages (reconstructed)

> The gold-standard PRD used to teach intent engineering. Reconstructed from
> Slack Design's public retrospective + inferences about a modern PRD for this
> work. Study the SHAPE: a falsifiable hypothesis with a right AND a wrong
> condition, tight non-goals, measurable success metrics with cohort + window,
> and a hard PRD/spec boundary. Notice what it deliberately leaves out.

## Problem statement

Channels at scale stop delivering their core value. In channels with high daily
message volume (>100 msgs/day — roughly 7% of active channels but capturing >60%
of active users' attention), multiple conversations interleave and replies become
unmoored from the messages they answer.

Three observable behaviors in this cohort:

- **Tracking failure** — active members increasingly ask "who is replying to
  what?" in research sessions. Average time-to-respond grows as channel volume
  grows.
- **Posting hesitancy** — members don't reply because the conversation has "moved
  on." Reply-rate per top-level message drops sharply above ~100 msgs/day.
- **Muting and leaving** — the channels that should be most valuable are the ones
  we lose engaged users from. Mute/leave rate among 30-day actives is far higher
  in high-volume channels.

The channels customers consider most strategically important — engineering-wide,
all-hands, project rooms — are exactly the ones where this happens.

## Why now

- Mute/leave rate in high-volume channels has grown month-over-month for two
  consecutive quarters.
- Customer Success surfaced "channels are too noisy" as a top-3 theme in
  enterprise QBRs.
- Slack's own internal channels show the same pattern — engineers and PMs quietly
  muting active channels they should be in.
- Competitor pressure: HipChat and Microsoft Teams are experimenting with
  threading. It's becoming a category expectation.

## Hypothesis

We believe **adding threaded replies inside busy channels** will cause **active
members of channels with 100+ daily messages** to **stop muting or leaving those
channels and keep posting**, resulting in **a 15% lift in weekly message volume
from that cohort**.

We'll know we're **right** if **channel mutes and leaves from that cohort drop by
10%** within **8 weeks of rollout**.

We'll know we're **wrong** if **overall channel engagement (top-level messages per
channel-day) drops**, or **thread-related complaints exceed 5% of NPS verbatim
comments**.

## Target user

- **Primary** — Active members of high-volume channels: users in 5+ channels with
  100+ daily messages who have muted at least one channel in the past 30 days.
- **Secondary** — Channel admins managing 50+ member channels who currently apply
  workarounds (announcement-only mode, splitting into sub-channels, pushing to
  DMs).
- **Not the target** — Users in small (<10 member) or low-volume channels.
  Threading is overhead they don't need.

## Non-goals (explicitly out of scope for v1, to prevent scope creep)

- **No multi-level threading.** A thread is a flat list of replies — nested
  threads felt heavy and users got lost.
- **No retroactive threading.** Old messages can't be converted after the fact.
- **Not replacing channels.** Threads are within-channel, not a separate place.
- **No threads in DMs in v1.** Different usage pattern; deferred.
- **No notification-settings overhaul.** Per-thread preferences are v2.
- **No public-facing API for threads in v1.** Partners notified; API GA follows.

## Risks and assumptions

| # | Risk | Assumption it depends on | How we'll de-risk |
|---|------|--------------------------|-------------------|
| 1 | Users don't know when to start a thread | UI affordances make the action self-evident | Multiple placement prototypes, internal dogfood |
| 2 | Threading makes channels harder, not easier | Reply visibility in-channel can be tuned | Test placement options (inline, sidebar, hybrid) |
| 3 | Overall channel engagement drops | Top-level message volume holds or grows | Guardrail metric: top-level msgs/channel-day |
| 4 | Mobile sync becomes prohibitive | Existing message envelope can carry a `thread_ts` ref | Engineering spike before commit |
| 5 | Old (pre-threads) clients break | A backwards-compat path exists at the API layer | Engineering spike + staged rollout |
| 6 | Search gets confusing | Thread replies can be indexed under their parent | Engineering decision documented in spec |

## Open questions (resolved during build, but flagged here)

- Where do users open a thread — hover action, persistent reply button,
  double-click?
- Should an @-mention inside a thread auto-follow the recipient, or be opt-in?
- How do unread threads appear in channel summaries and unread counts?
- Mobile-first or desktop-first launch sequence?

## Success metrics

| Metric | Target | Cohort | Window |
|--------|--------|--------|--------|
| Mute & leave rate in high-volume channels | −10% | Active members | Week 8 post-rollout |
| Weekly message volume | +15% | Active members in high-volume channels | Week 8 post-rollout |
| Top-level messages per channel-day *(guardrail)* | No drop | All channels | Continuous |
| Thread reply adoption *(leading indicator)* | >20% of eligible users post ≥1 reply | Active members in high-volume channels | Week 4 & 8 |
| NPS verbatims mentioning "noise" / "harder to read" | <5% of respondents | All respondents | Quarterly |

## Experiments & discovery plan (resolve before full build commit)

- **Usability spike (1–2 weeks)** — prototype 3 placement options (inline; right
  panel; modal). Internal dogfood. Decide: least confusing without losing
  discoverability.
- **Feasibility spike (~1 week)** — verify the message envelope can carry a
  `thread_ts` reference without breaking pre-threads clients. Engineering owns.
- **Reverse-experiment (after 2 weeks dogfood)** — turn the feature *off*
  internally and measure complaints. No complaints → it isn't earning its weight.
  Complaints → a stronger signal than any A/B test.

## What is deliberately NOT in this PRD

These are engineering decisions that belong in the **spec**, not here:

- Data model — `thread_ts` field on the messages table vs a separate
  `thread_replies` table
- API design — embedded thread metadata vs `GET /threads/:id`
- Mobile incremental sync semantics
- Notification routing rules
- Backwards-compatibility envelope handling
- Search indexing strategy

The PRD says *we will not break old clients*. The spec says *how*.
