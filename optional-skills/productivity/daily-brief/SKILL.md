---
name: daily-brief
description: Compose a morning briefing for the user by pulling today's calendar, open tasks, and relevant memory, then delivering a short prioritized summary over the primary chat channel. Use when the user asks for a daily brief, "what's on today", a standup, or sets up a scheduled morning digest.
version: 1.0.0
author: YourProduct OS
license: MIT
platforms: [linux, macos, windows]
metadata:
  YourProduct:
    category: productivity
    tags: [briefing, calendar, tasks, digest, scheduled]
    related_skills: [flashcards, log-triage]
    mutates: true
    capability_gate: chat.send
---

# Daily Brief

Assemble and deliver a concise morning briefing.

## Sources to pull (in order)

1. **Calendar** — today's events via the Google integration (see
   `.claude/sections/05_*`). If Google OAuth is not configured, skip silently
   and note "no calendar connected".
2. **Open tasks / goals** — read `GOALS.md` and any task list in the vault for
   what's due or in-flight.
3. **Memory** — recall recent decisions or follow-ups flagged for today.

## Composition

Produce a tight brief, not a data dump:

```
☀️ Brief — <weekday, date>

📅 <N> events
  • 09:30 <event> (<duration>)
  • ...
✅ Top 3 today
  1. <highest-leverage task>
  2. ...
🧠 Follow-ups
  • <thing you said you'd do>
```

- Lead with the single most important thing.
- Cap events at the next 6; collapse the rest into a count.
- If the day is empty, say so in one line — don't pad.

## Delivery (gated)

Sending the brief is an **outbound mutation**. Deliver over the primary channel
only through the framework's `chat.send` capability gate with an audit trail —
do not bypass it. For a recurring morning brief, register a scheduled job per
`.claude/sections/03_*` (heartbeat/scheduled jobs) rather than polling.
