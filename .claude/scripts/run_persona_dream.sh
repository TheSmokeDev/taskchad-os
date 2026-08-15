#!/usr/bin/env bash
# Persona dream tick runner for cron/launchd (macOS/Linux) — nightly, ~3:30 AM.
# Fans the FULL 5-phase dream cycle out over every named profile by spawning
# memory_dream.py -p <name> once each, staggered 30 min after the main dream.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# -p default is the boot-shim's rank-1 force-default sentinel (personas/boot.py).
# Without it, an operator's sticky ~/.homie/active_profile (rank 3) or an
# inherited HOMIE_HOME (rank 2) silently makes run_tick's default-profile
# guard refuse and the WHOLE fan-out no-ops for the night.
uv run python persona_dream_tick.py -p default
EXITCODE=$?

# Non-zero means at least one persona's dream FAILED (failed spawn, child
# error, untrustworthy receipt, or a refusal) — the tick catches those so the
# rest of the roster keeps moving, then reports them here. Silent skips
# (recency guard, DREAM_SILENT, kill switch) stay exit 0.
if [ "$EXITCODE" -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Persona dream tick completed exit=$EXITCODE" >> persona_dream_runs.log
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Persona dream tick FAILED exit=$EXITCODE" >> persona_dream_runs.log
fi

exit $EXITCODE
