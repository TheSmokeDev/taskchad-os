---
name: heartbeat-monitor
description: External uptime and liveness check for the bot, memory pipelines, and local API. Use when the user wants to monitor whether the agent is alive, set up a health check, get notified when the bot goes down, or debug why scheduled jobs stopped running.
version: 1.0.0
author: TaskChad OS
license: MIT
platforms: [linux, macos, windows]
metadata:
  taskchad:
    category: devops
    tags: [monitoring, uptime, healthcheck, heartbeat, ops]
    related_skills: [log-triage]
    mutates: false
---

# Heartbeat Monitor

Check that the running stack is healthy and report a single green/red verdict.

## What to probe

The framework runs a local API on port 4322 (see
`.claude/sections/03_*` / orchestration). Probe the pieces that matter:

| Component | Probe |
|-----------|-------|
| Local API | `GET http://127.0.0.1:4322/health` returns 200 |
| Chat process | process for `run_chat.sh` is alive |
| Memory heartbeat | last heartbeat timestamp is < 2× the interval old |
| Disk | vault partition has free space |

## Run

```bash
bash optional-skills/devops/heartbeat-monitor/scripts/healthcheck.sh
# exit 0 = all green, exit 1 = something is down
```

The script prints a per-check table and sets a non-zero exit code on any
failure, so it composes with cron / CI / an external uptime service.

## Escalation

- For local use, surface the red verdict in chat.
- For unattended monitoring, point an **external** uptime service (Healthchecks.io,
  UptimeRobot, a cron on another host) at this script or at the `/health` route —
  a monitor running inside the same process it watches can't report its own
  death.
- This skill only reads state; it does not restart anything. Pair with
  `log-triage` to explain *why* a check went red.
