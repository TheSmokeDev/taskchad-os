#!/usr/bin/env bash
# Reflection runner for cron/launchd (macOS/Linux)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Run reflection using UV
uv run python memory_reflect.py

# Log the run
echo "$(date '+%Y-%m-%d %H:%M:%S') - Reflection completed" >> reflection_runs.log
