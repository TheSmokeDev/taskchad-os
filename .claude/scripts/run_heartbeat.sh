#!/usr/bin/env bash
# Heartbeat runner for cron/launchd (macOS/Linux)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure uv is in PATH (cron uses minimal PATH)
export PATH="$PATH:/root/.local/bin"

# Run heartbeat using UV and only log on success
uv run python heartbeat.py \
  && echo "$(date '+%Y-%m-%d %H:%M:%S') - Heartbeat completed" >> heartbeat_runs.log \
  || echo "$(date '+%Y-%m-%d %H:%M:%S') - Heartbeat FAILED" >> heartbeat_runs.log
