#!/usr/bin/env bash
# cron_entrypoint.sh — Docker scheduler for The Homie background jobs
#
# PRP-7c Phase 3: spawned cron jobs inherit HOMIE_HOME so the right profile's
# config / vault / state get exercised inside each cron-launched python run.
set -euo pipefail

HOMIE_HOME="${HOMIE_HOME:-/app}"
export HOMIE_HOME

CRONTAB=$(mktemp)

# NOTE: heredoc switched from <<'EOF' (literal) to <<EOF (interpolating) so
# ${HOMIE_HOME} substitutes at write time. Each cron line bakes the env var
# in front of the python invocation so supercronic-spawned children inherit
# the right profile (cron strips most env vars by default).
cat > "$CRONTAB" <<EOF
# Heartbeat — every 30 minutes during active hours (8am-10pm)
*/30 8-22 * * * cd /app/scripts && HOMIE_HOME=${HOMIE_HOME} uv run python heartbeat.py 2>&1 | head -50

# Daily reflection — 8 AM
0 8 * * * cd /app/scripts && HOMIE_HOME=${HOMIE_HOME} uv run python memory_reflect.py 2>&1 | head -50

# Weekly synthesis — Sunday 8 PM
0 20 * * 0 cd /app/scripts && HOMIE_HOME=${HOMIE_HOME} uv run python memory_weekly.py 2>&1 | head -50
EOF

echo "Starting scheduler with TZ=${TZ:-UTC} HOMIE_HOME=${HOMIE_HOME}"
exec /usr/local/bin/supercronic "$CRONTAB"
