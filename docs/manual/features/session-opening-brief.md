# Session Opening Brief (Living Mind Act 4)

Status: Shipped
Owner: Framework (memory pipelines + chat engine)
Last updated: 2026-06-12

## What It Does

When the operator's first message arrives after a meaningful absence
(default 8 hours, env-tunable), the engine deterministically assembles a
"while you were out" block — fresh heartbeat observations, episodes written
while away, applied memory amendments, and mid-flight open threads — and
rides it on the SAME turn's runtime prompt with an explicit instruction to
OPEN the reply with a short first-person brief before answering the message.

**Zero new LLM calls.** The model that was already going to answer the turn
voices the brief as part of that same response, on every lane. There is no
scheduled morning job, no proactive push, and no new notification channel —
the brief only ever rides an incoming operator turn.

## When It Fires (the gate)

ALL of these must hold:

| Condition | Detail |
|---|---|
| Enabled | `SESSION_BRIEF_ENABLED` (default `true`) |
| Interactive turn | The RAW message source equals `interactive` exactly — `cron`, `tool`, `hook`, and malformed variants fail closed. No normalization rescue. |
| Not a workflow turn | PIV/structured-workflow turns never fire. |
| History exists | A fresh install with no sessions and no clear events stays silent (`no_history`). |
| Away long enough | `away >= SESSION_BRIEF_AWAY_HOURS` (INCLUSIVE — exactly the threshold fires). |
| Something fresh happened | The boredom gate below. |

Turns that carry pre-fetched data ("good morning, how are we looking?") DO
fire — the brief coexists with prefetched context on the same single pass.

## The Boredom Contract (silence is success)

`fresh_items` counts ONLY change-source items that happened while away:

- fresh heartbeat **observations** (bullet date ≥ the away boundary date —
  day-floor resolution, documented below)
- fresh **`[heartbeat]`-tagged open threads** (heartbeat promotions ARE
  changes — the one explicit thread exception)
- **episodes** written strictly after the boundary instant (status-agnostic:
  an overnight-consolidated episode still counts)
- applied memory **amendments** strictly after the boundary instant

Open threads are otherwise CONTEXT ONLY: they render in the Mid-flight
section when the brief already fired, and they NEVER count toward
`fresh_items` — stale threads and fresh MANUAL threads contribute zero at
any away duration. `fresh_items < SESSION_BRIEF_MIN_FRESH_ITEMS` means NO
brief — not even a minimal line. Silence has its own receipt (the
`[SessionBrief] silent` log line and a `no_fresh_items` trace decision).

## What The Brief Contains

```
# Session Opening Brief (deliver first)

The operator is opening a new working session after ~8.5h away (...).
OPEN your reply with a short first-person brief — ...

## What changed while away
- (observations + fresh [heartbeat] threads, newest first, capped)
- session (surface, date): episode summary

## Self updates (memory amendments)
- TARGET.md: amendment summary

## Mid-flight (open threads)
- (open thread bullets — any date; context only)
```

Cap-priority semantics: per-source caps (`SESSION_BRIEF_MAX_PER_SECTION`)
apply first, then the total `SESSION_BRIEF_MAX_CHARS` cap. The instruction
block is reserved budget and is never truncated; one item from each fired
fresh source is reserved before any section deep-fills; Mid-flight is last
and drops first under pressure. Truncation happens at a newline boundary
with an explicit `[TRUNCATED]` marker. Section headers render only when
non-empty, and every rendered line is single-line sanitized.

## Away Time Comes From Physical State

`last_operator_activity` = max over two physical stores:

1. the newest INTERACTIVE chat session `updated_at` (cron/tool/hook-tagged
   sessions never count — counting them would mask real operator absence)
2. the newest interactive-trigger row in the append-only clear-event
   receipts file (`/clear` deletes the session row, so the event is the
   only trace of that presence)

Clear-event rows carry an additive `trigger_source` field recording WHO
triggered the clear (`interactive` / `cron` / `tool` / `hook`) — distinct
from the event label itself. A scheduled `/clear` can no longer mask the
operator's real away gap. Legacy rows written before the field exist are
treated as interactive (dated compatibility note, 2026-06-12).

Every physical timestamp (SQLite naive strings, Postgres aware datetimes,
clear-event ISO, amendment `applied_at` aware-UTC) is normalized to naive
local time by a single owner before any comparison.

## Router-First Mornings (the brief-owed marker)

Direct router commands (`/status`, …) never reach the engine but DO bump
session recency. Without protection, a `/status`-first morning would close
the away gap and permanently eat the brief. Fix: when an interactive router
command arrives during an away gap, a tiny `session-brief-owed.json` marker
is written into the state directory BEFORE the recency bump, carrying the
true pre-bump boundary. The first completed ENGINE-turn decision (fired OR
silent) consumes it — exactly once. Failures leave it intact for retry; a
corrupt marker reads as absent. The marker is undelivered-debt bookkeeping,
never an alternate source of away truth.

An interactive `/clear` captures the marker the same way before its own
clear event closes the gap.

## Prompt-Path Mechanics (why not a region)

The brief rides the turn prompt (`RuntimeRequest.prompt`) as a suffix after
any attachment block — the same transport attachments use. It travels via
stdin on the native lane and lands inside the `User task:` block on CLI
lanes, so all providers receive it with zero adapter changes. It is NOT a
system-prompt region: on Windows the system append rides the process
command line under a hard cap and gets tail-truncated exactly when context
is fullest. It never mutates the message text either — persisted chat
history shows ONLY what the operator typed (history purity).

## Day-Floor Freshness Note

Working-memory bullets carry day-resolution dates, so a same-day item the
operator saw before leaving can resurface once (bounded by caps). Episodes
and amendments carry exact instants and use strict comparison. This
asymmetry is intentional and documented rather than inventing per-bullet
timestamps.

## Knobs

| Env var | Default | Meaning |
|---|---|---|
| `SESSION_BRIEF_ENABLED` | `true` | Kill switch. |
| `SESSION_BRIEF_AWAY_HOURS` | `8` | Away gate threshold (hours, inclusive). |
| `SESSION_BRIEF_MIN_FRESH_ITEMS` | `1` | Boredom threshold. |
| `SESSION_BRIEF_MAX_PER_SECTION` | `5` | Per-source item cap. |
| `SESSION_BRIEF_MAX_CHARS` | `2400` | Total block cap (priority semantics above). |

All knobs resolve at call time — an env change takes effect on the next
turn with no restart.

## Log Lines

| Line | Meaning |
|---|---|
| `[SessionBrief] fired: away 8.5h, 3 fresh item(s)` | A brief rode this turn. |
| `[SessionBrief] silent: away 10.2h, nothing fresh` | The boredom receipt — away long enough, nothing worth saying. |
| `[SessionBrief] non-blocking failure: …` | The brief seam failed; the turn proceeded bare. |
| `[SessionBrief] marker seam failure (non-blocking): …` | Marker bookkeeping failed; behavior degrades to pre-marker semantics. |

No line is printed for ordinary suppressions (`not_away`, non-interactive
turns) — that would be per-turn log spam. Every decision, positive or
negative, is still recorded in the turn's trace metadata.

## Failure Modes

| Failure | Behavior |
|---|---|
| Builder/resolver exception | Turn proceeds bare; decision `error`; marker preserved. |
| One source unreadable (ledger, episodes dir, working memory) | That section degrades to empty; the others still render. |
| Corrupt marker file | Reads as absent. |
| Process restart inside a gap | At worst one extra brief (bounded, accepted). |
| Model buries the brief mid-reply | Cosmetic degradation only — no data loss. |
| Runtime error on the brief-carrying turn (quota/auth/kill-switch) | Brief NOT consumed — marker intact, `fired_at` rolled back; next successful turn re-fires it once (#138). |
| `/stop` / cancellation mid-runtime on the brief-carrying turn | `CancelledError` handler rolls back and re-raises — same re-fire guarantee. |
| Second conversation's turn lands while a fired brief is in flight (e.g. Telegram + Discord at wake-up) | Deferred (`suppressed: "brief_in_flight"`); commit/rollback are identity-guarded no-ops for foreign turns. `handle_message` carries the token in a per-turn holder whose `finally` rolls it back on every exception-delivering exit (cancel/close/GC). A retained generator that is later *resumed to exhaustion* commits and frees the slot instead. A consumer that breaks while *retaining* the generator holds the slot — briefs defer with the marker intact, never lost, until close or process restart releases it. Defer, never lose; no time-based reclaim. |
