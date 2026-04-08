#!/usr/bin/env bash
# Weekly synthesis runner for cron/launchd (macOS/Linux)
# Schedule: Sunday 20:00 (8 PM)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Run weekly synthesis using UV
uv run python memory_weekly.py

# Log the run
echo "$(date '+%Y-%m-%d %H:%M:%S') - Weekly synthesis completed" >> weekly_runs.log
