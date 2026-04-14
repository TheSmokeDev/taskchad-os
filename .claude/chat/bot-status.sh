#!/usr/bin/env bash
# bot-status.sh — reliable one-shot health check for The Homie bot.
# Checks pid file AND scans by process cmdline. Never trusts only one source.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$SCRIPT_DIR/bot.pid"
LOG_FILE="$SCRIPT_DIR/bot.log"

pid_file_pid=""
pid_file_alive=false
cmdline_pids=()

# 1. Check pid file
if [ -f "$PID_FILE" ]; then
  pid_file_pid=$(cat "$PID_FILE")
  if kill -0 "$pid_file_pid" 2>/dev/null; then
    pid_file_alive=true
  fi
fi

# 2. Scan by cmdline (Windows), scoped to THIS repo only.
# Uses the absolute path of main.py derived from this script's location so that
# multiple Homie bots running on the same machine (e.g. thehomie + nestor)
# never count each other's processes. Also deduplicates venv shim+child pairs.
MAIN_PY_WIN=$(echo "$SCRIPT_DIR/main.py" | sed 's|^/\([a-zA-Z]\)/|\1:/|')

if command -v powershell.exe &>/dev/null; then
  while IFS= read -r pid; do
    pid=$(echo "$pid" | tr -d '\r ')
    [ -n "$pid" ] && cmdline_pids+=("$pid")
  done < <(powershell.exe -NoProfile -Command "
    \$procs = Get-WmiObject Win32_Process |
      Where-Object { \$_.Name -like 'python*' -and \$_.CommandLine -like '*${MAIN_PY_WIN}*' }
    \$parent_pids = @(\$procs | ForEach-Object { \$_.ParentProcessId })
    \$procs | Where-Object { \$parent_pids -notcontains \$_.ProcessId } | ForEach-Object { \$_.ProcessId }
  " 2>/dev/null)
fi

# 3. Report
echo "=============================="
echo "  Homie Bot Status"
echo "=============================="

if [ -n "$pid_file_pid" ]; then
  if $pid_file_alive; then
    echo "  PID file:  $pid_file_pid  [ALIVE]"
  else
    echo "  PID file:  $pid_file_pid  [STALE - process dead]"
  fi
else
  echo "  PID file:  not found"
fi

if [ ${#cmdline_pids[@]} -gt 0 ]; then
  echo "  Cmdline scan: ${cmdline_pids[*]}  [ALIVE]"
  # Flag if pid file doesn't match
  if [ -n "$pid_file_pid" ] && ! printf '%s\n' "${cmdline_pids[@]}" | grep -qx "$pid_file_pid"; then
    echo "  ⚠ WARNING: bot.pid ($pid_file_pid) doesn't match live process(es) — pid file is stale"
  fi
else
  echo "  Cmdline scan: no bot process found"
fi

echo ""

# 4. Overall verdict
if [ ${#cmdline_pids[@]} -gt 0 ]; then
  echo "  STATUS: RUNNING (${#cmdline_pids[@]} instance(s))"
  if [ ${#cmdline_pids[@]} -gt 1 ]; then
    echo "  ⚠ DUPLICATE INSTANCES DETECTED — run run_chat.sh to clean up"
  fi
elif $pid_file_alive; then
  echo "  STATUS: RUNNING (pid file only — cmdline scan unavailable)"
else
  echo "  STATUS: DEAD"
fi

echo ""

# 5. Last log lines
echo "  Last log:"
tail -5 "$LOG_FILE" 2>/dev/null | sed 's/^/    /'
echo "=============================="
