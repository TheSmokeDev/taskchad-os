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
  # Use the venv shim directly — it correctly wires site-packages for the real cpython.
  # The shim spawns a child cpython process (normal Windows venv behavior); bot-status.sh
  # deduplicates the shim+child pair and counts them as one logical instance.
  VENV_PYTHON="$SCRIPTS_DIR/.venv/Scripts/python.exe"
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

# Kill existing bot — check pid file first, then scan by cmdline as fallback.
# The cmdline scan catches bots started outside run_chat.sh (scheduled tasks,
# manual python invocations) that leave bot.pid stale.
_kill_existing() {
  local killed=0

  # 1. Try pid file
  if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "Stopping old bot (PID $OLD_PID from pid file)..."
      kill "$OLD_PID" 2>/dev/null
      sleep 2
      killed=1
    fi
    rm -f "$PID_FILE"
  fi

  # 2. Scan by cmdline — scoped to THIS repo's main.py path so we never kill
  # another Homie bot instance (e.g. nestor) running on the same machine.
  MAIN_PY_WIN=$(echo "$SCRIPT_DIR/main.py" | sed 's|^/\([a-zA-Z]\)/|\1:/|')
  if command -v powershell.exe &>/dev/null; then
    ORPHAN_PIDS=$(powershell.exe -NoProfile -Command \
      "Get-WmiObject Win32_Process | Where-Object { \$_.CommandLine -like '*${MAIN_PY_WIN}*' } | Select-Object -ExpandProperty ProcessId" \
      2>/dev/null | tr -d '\r')
    for pid in $ORPHAN_PIDS; do
      [ -z "$pid" ] && continue
      echo "Stopping orphaned bot (PID $pid, found by cmdline scan)..."
      taskkill.exe /PID "$pid" /F &>/dev/null || kill "$pid" 2>/dev/null
      killed=1
    done
    [ $killed -gt 0 ] && sleep 2
  fi
}
_kill_existing

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

  # Wait for bot to initialize
  sleep 5

  # Capture the real Windows PID of the python process (not the bash job wrapper).
  # On Windows/Git Bash, $! is the bash job PID, not the actual python.exe PID.
  WIN_PID=""
  if command -v powershell.exe &>/dev/null; then
    WIN_PID=$(powershell.exe -NoProfile -Command \
      "Get-WmiObject Win32_Process | Where-Object { \$_.CommandLine -like '*chat/main.py*' } | Sort-Object ProcessId | Select-Object -Last 1 -ExpandProperty ProcessId" \
      2>/dev/null | tr -d '\r ')
  fi
  BOT_PID="${WIN_PID:-$!}"
  echo "$BOT_PID" > "$PID_FILE"

  if [ -n "$WIN_PID" ] && kill -0 "$WIN_PID" 2>/dev/null; then
    echo "Telegram bot started (Windows PID $WIN_PID)"
    echo "Logs: $LOG_FILE"
  elif kill -0 "$!" 2>/dev/null; then
    echo "Telegram bot started (bash PID $!, Windows PID unknown)"
    echo "Logs: $LOG_FILE"
  else
    echo "Bot process exited — check logs:"
    tail -10 "$LOG_FILE"
  fi
fi
