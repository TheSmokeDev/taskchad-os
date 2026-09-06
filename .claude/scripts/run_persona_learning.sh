#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
uv run python persona_learning_tick.py
EXITCODE=$?
if [ "$EXITCODE" -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Persona learning tick completed" >> persona_learning_runs.log
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Persona learning tick FAILED exit=$EXITCODE" >> persona_learning_runs.log
fi
exit $EXITCODE
