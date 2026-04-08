#!/usr/bin/env bash
# Start The Homie Telegram bot.
# Uses the real cpython directly (not the venv launcher) to avoid
# Windows double-spawn issues where python.exe is a shim that spawns
# a child python.exe, causing duplicate Telegram polling.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS_DIR="$SCRIPT_DIR/../scripts"
LOG_FILE="$SCRIPT_DIR/bot.log"
PID_FILE="$SCRIPT_DIR/bot.pid"

# Resolve the REAL python binary (skip venv launcher shim on Windows)
if [ -f "$SCRIPTS_DIR/.venv/Scripts/python.exe" ]; then
  # Windows: read the pyvenv.cfg to find the real python, not the launcher
  REAL_PYTHON=$(python -c "
import sys, os
cfg = os.path.join(r'$SCRIPTS_DIR', '.venv', 'pyvenv.cfg')
if os.path.exists(cfg):
    for line in open(cfg):
        if line.startswith('home'):
            home = line.split('=',1)[1].strip()
            print(os.path.join(home, 'python.exe'))
            break
else:
    print(r'$SCRIPTS_DIR/.venv/Scripts/python.exe')
" 2>/dev/null)
  # Fallback if cfg parsing failed
  [ -z "$REAL_PYTHON" ] && REAL_PYTHON="$SCRIPTS_DIR/.venv/Scripts/python.exe"
  VENV_PYTHON="$REAL_PYTHON"
elif [ -f "$SCRIPTS_DIR/.venv/bin/python" ]; then
  VENV_PYTHON="$SCRIPTS_DIR/.venv/bin/python"
else
  echo "Creating venv..."
  cd "$SCRIPTS_DIR" && uv sync
  VENV_PYTHON="$SCRIPTS_DIR/.venv/bin/python"
  [ -f "$SCRIPTS_DIR/.venv/Scripts/python.exe" ] && VENV_PYTHON="$SCRIPTS_DIR/.venv/Scripts/python.exe"
fi

# Set the venv's site-packages so imports work with the real python
export VIRTUAL_ENV="$SCRIPTS_DIR/.venv"
export PATH="$VIRTUAL_ENV/Scripts:$VIRTUAL_ENV/bin:$PATH"

# Kill existing bot if running
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "Stopping old bot (PID $OLD_PID)..."
    kill "$OLD_PID" 2>/dev/null
    sleep 2
  fi
  rm -f "$PID_FILE"
fi

cd "$SCRIPTS_DIR"

if [ "$1" = "--fg" ]; then
  # Foreground mode (for debugging)
  shift
  PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 exec "$VENV_PYTHON" "$SCRIPT_DIR/main.py" "$@"
else
  # Background mode — same approach for both Windows and Unix.
  # Using the real cpython binary (not the venv launcher shim) avoids
  # the double-spawn problem entirely.
  PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 "$VENV_PYTHON" "$SCRIPT_DIR/main.py" "$@" > "$LOG_FILE" 2>&1 &
  BOT_PID=$!
  echo "$BOT_PID" > "$PID_FILE"

  # Wait for bot to initialize
  sleep 5

  if kill -0 "$BOT_PID" 2>/dev/null; then
    echo "Telegram bot started (PID $BOT_PID)"
    echo "Logs: $LOG_FILE"
  else
    echo "Bot process exited — check logs:"
    tail -10 "$LOG_FILE"
  fi
fi
