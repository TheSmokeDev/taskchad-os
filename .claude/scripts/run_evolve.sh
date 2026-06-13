#!/usr/bin/env bash
# Evolve-loop runner for cron/launchd (macOS/Linux) — Living Self Act 4.
# Runs the SAFE recall `propose` (no identity mutation). The belief rail
# (propose-belief) is Archon-driven, NOT this cron.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

uv run python evolve/evolve_loop.py propose
EXITCODE=$?

echo "$(date '+%Y-%m-%d %H:%M:%S') - Evolve propose completed exit=$EXITCODE" >> evolve_runs.log

exit $EXITCODE
