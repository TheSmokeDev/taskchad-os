#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
uv run python persona_learning_tick.py
echo "$(date '+%Y-%m-%d %H:%M:%S') - Persona learning tick completed" >> persona_learning_runs.log
