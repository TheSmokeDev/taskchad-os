# HEARTBEAT.md - Proactive Checklist

_This file defines what to check during heartbeat runs. Pre-loaded into the prompt by heartbeat.py._

## Quick Checks (Every Heartbeat)

Data is pre-fetched via direct API integrations and included in the prompt context.

- [ ] Any urgent emails in the last 2 hours? (via Gmail API)
- [ ] Any calendar events in the next 4 hours? (via Calendar API)
- [ ] Any overdue tasks? (via Asana API)
- [ ] Any important Slack messages in monitored channels? (via Slack API)

## Proactive Suggestions (Every Heartbeat)

After reviewing the data above, think about:
- [ ] What should be prioritized right now given the calendar and deadlines?
- [ ] Is there anything coming up that needs prep and doesn't have it yet?
- [ ] Are there any overdue items that need a specific action (reschedule, delegate, cancel)?
- [ ] Based on the time of day, is there something productive to suggest?

## Anomaly Detection

Flag anything unusual:
- [ ] Spike in unread email count (vs normal baseline)
- [ ] Cancelled or moved meetings that weren't expected
- [ ] Tasks overdue by more than a week (stale - suggest cleanup)
- [ ] No activity in usually active Slack channels

## Periodic Checks (Rotate Through)

### Project Status (1-2x daily)
- [ ] Any blocked tasks that need attention?
- [ ] Any pull requests waiting for review?

### Team Coordination (1x daily, weekday mornings)
- [ ] Any PRs from team members waiting for review?
- [ ] Any blocked tasks assigned to others?
- [ ] Any timezone-relevant follow-ups?

### Memory Maintenance (Daily)
- [ ] Review yesterday's daily log
- [ ] Extract anything worth adding to MEMORY.md

## When to Notify

**Notify immediately:**
- Urgent email from important contacts
- Calendar event starting in < 2 hours with no prep notes
- Overdue tasks

**Batch for next interaction:**
- Non-urgent emails
- Tasks due in > 48 hours
- Interesting but not urgent information

**Stay silent (HEARTBEAT_OK):**
- Nothing new since last check
- Outside active hours unless truly urgent
- Everything is on track

## Check Tracking

Last checks are tracked in `state/heartbeat-state.json`.
Don't repeat a check if it was done < 30 minutes ago.

---

_Update this file to add or remove checks as your needs change._
