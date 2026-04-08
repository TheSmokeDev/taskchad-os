#!/usr/bin/env bash
# cron_entrypoint.sh — Docker scheduler for The Homie background jobs
set -euo pipefail

CRONTAB=$(mktemp)

cat > "$CRONTAB" <<'EOF'
# Heartbeat — every 30 minutes during active hours (8am-10pm)
*/30 8-22 * * * cd /app/scripts && uv run python heartbeat.py 2>&1 | head -50

# Daily reflection — 8 AM
0 8 * * * cd /app/scripts && uv run python memory_reflect.py 2>&1 | head -50

# Weekly synthesis — Sunday 8 PM
0 20 * * 0 cd /app/scripts && uv run python memory_weekly.py 2>&1 | head -50
EOF

echo "Starting scheduler with TZ=${TZ:-UTC}"
exec /usr/local/bin/supercronic "$CRONTAB"
