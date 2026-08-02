#!/usr/bin/env bash
# Dream-cycle runner for cron/launchd (macOS/Linux) — nightly, ~3 AM (issue #170).
# Runs the 5-phase dream cycle (Orient/Gather/Consolidate/Prune/Belief-Evolve).
# No --force: DREAM_MIN_INTERVAL_HOURS dedupes the Sunday-weekly overlap.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

uv run python memory_dream.py
EXITCODE=$?

echo "$(date '+%Y-%m-%d %H:%M:%S') - Dream cycle completed exit=$EXITCODE" >> dream_runs.log

exit $EXITCODE
