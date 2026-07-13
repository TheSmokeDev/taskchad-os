---
name: log-triage
description: Parse bot logs and Langfuse traces into a short incident digest — what broke, how often, and the likely cause. Use when the user asks why the bot errored, wants a summary of recent failures, is debugging a crash, or after heartbeat-monitor reports a red check.
version: 1.0.0
author: YourProduct OS
license: MIT
platforms: [linux, macos, windows]
metadata:
  YourProduct:
    category: devops
    tags: [logs, triage, debugging, langfuse, observability]
    related_skills: [heartbeat-monitor]
    mutates: false
---

# Log Triage

Turn noisy logs into a ranked, human-readable incident digest.

## Sources

- **Bot log** — the chat process log (e.g. `bot.log` / wherever `run_chat.sh`
  writes). Start here.
- **Langfuse** — if `LANGFUSE_*` is configured, traces give per-request timing,
  model calls, and errors. See `.claude/sections/08_*` (observability).

## Triage method

1. **Scope** — last N hours, or since the last known-good marker.
2. **Extract errors** — grep for `ERROR`/`CRITICAL`/`Traceback`/`Unhandled`.
3. **Cluster** — group by normalized message (strip timestamps, IDs, paths) so
   "the same error 200 times" reads as one cluster with a count.
4. **Rank** — by frequency × severity. The top 1–3 clusters are the headline.
5. **Diagnose** — for each headline cluster: first seen, last seen, count, the
   representative stack frame, and a one-line hypothesis.

## Output

```
🔧 Log triage — last <window>

1. <normalized error>  ×<count>
   first <ts> · last <ts>
   at <file:line>
   likely: <hypothesis>

2. ...

Quiet otherwise: <count> warnings, no other errors.
```

Keep it to the signal. Do not paste raw log spans — cite `file:line` and counts.
This skill is read-only; it diagnoses, it does not restart or patch.
