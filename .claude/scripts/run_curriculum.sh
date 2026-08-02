#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
uv run python curriculum_tick.py
EXITCODE=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') - Curriculum tick exit=$EXITCODE" >> curriculum_runs.log
exit "$EXITCODE"
