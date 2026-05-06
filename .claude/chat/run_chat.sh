#!/usr/bin/env bash
# Start The Homie Telegram bot.
# Uses the real cpython directly (not the venv launcher) to avoid
# Windows double-spawn issues where python.exe is a shim that spawns
# a child python.exe, causing duplicate Telegram polling.
#
# PRP-7c Phase 3 (lifecycle-surfaces): pid path / lock path / log dir are
# resolved through ``personas.services`` so the script follows the active
# profile (default profile keeps install-dir paths; named profiles land
# under ``$HOMIE_HOME/run/`` and ``$HOMIE_HOME/logs/``).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS_DIR="$SCRIPT_DIR/../scripts"

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
export PYTHONPATH="$SCRIPTS_DIR${PYTHONPATH:+:$PYTHONPATH}"

# F1 (R2) — pre-parse --profile/-p/--profile=NAME from the wrapper's argv and
# export HOMIE_HOME BEFORE the resolver subprocess runs. Without this the
# resolver's `python -c` invocation has its own argv (just `python -c '...'`),
# so apply_persona_override() inside that subprocess can never see the
# wrapper's --profile flag — it would resolve DEFAULT-profile paths while the
# bot itself (launched at the bottom of this script) DOES see the flag and
# switches to the named profile. End result: pid file / log writes / cleanup
# all run against the wrong profile.
#
# Don't strip the flag from "$@" — the bot's own apply_persona_override() does
# that (consistent argv handling across the wrapper and the binary).
_HOMIE_PROFILE_OVERRIDE=""
_homie_args=("$@")
i=0
while [ $i -lt ${#_homie_args[@]} ]; do
  arg="${_homie_args[$i]}"
  case "$arg" in
    --profile=*)
      _HOMIE_PROFILE_OVERRIDE="${arg#--profile=}"
      break
      ;;
    --profile|-p)
      next=$((i + 1))
      if [ $next -lt ${#_homie_args[@]} ]; then
        _HOMIE_PROFILE_OVERRIDE="${_homie_args[$next]}"
      fi
      break
      ;;
  esac
  i=$((i + 1))
done

if [ -n "$_HOMIE_PROFILE_OVERRIDE" ]; then
  if [ "$_HOMIE_PROFILE_OVERRIDE" = "default" ] || [ "$_HOMIE_PROFILE_OVERRIDE" = "-" ]; then
    # Force default profile by clearing any inherited HOMIE_HOME so the boot
    # shim's rank-4 fallback kicks in.
    unset HOMIE_HOME
  else
    _HOMIE_PROFILES_ROOT="${HOME}/.homie/profiles"
    _HOMIE_TARGET="${_HOMIE_PROFILES_ROOT}/${_HOMIE_PROFILE_OVERRIDE}"
    if [ ! -d "$_HOMIE_TARGET" ]; then
      echo "ERROR: Profile '$_HOMIE_PROFILE_OVERRIDE' not found at $_HOMIE_TARGET" >&2
      echo "  Create it via: thehomie profile create $_HOMIE_PROFILE_OVERRIDE" >&2
      exit 1
    fi
    export HOMIE_HOME="$_HOMIE_TARGET"
  fi
fi

# Resolve profile-aware paths via personas.services. Single python -c call
# emits three newline-separated paths so we don't pay 3x interpreter startup.
#
# F1 (R3) — forward the wrapper's argv ("$@") to the subprocess so
# apply_persona_override() can pre-parse rank-1 (CLI flag) symmetrically
# with the bot launch. Without this forward, the resolver subprocess sees
# sys.argv=['-c'] and falls through to rank-3 (sticky ~/.homie/active_profile).
# That makes `--profile default` resolve sticky-sales paths while the actual
# bot launch (which DOES see argv) correctly forces default. Forwarding argv
# closes the asymmetry — both the resolver AND the bot see the same flag.
_PATHS=$("$VENV_PYTHON" -c "
import sys
sys.path.insert(0, r'$SCRIPTS_DIR')
from personas import apply_persona_override
apply_persona_override()
from personas.services import get_bot_pid_path, get_bot_lock_path, get_log_dir
print(get_bot_pid_path())
print(get_bot_lock_path())
print(get_log_dir())
" "$@" 2>/dev/null)

PID_FILE=$(echo "$_PATHS" | sed -n '1p')
LOCK_FILE=$(echo "$_PATHS" | sed -n '2p')
LOG_DIR=$(echo "$_PATHS" | sed -n '3p')

# F4 — fail loudly if the service resolver could not run. The hardcoded
# install-dir fallback paths were removed (they shipped the wrong location
# for named profiles and silently corrupted the default profile's PID
# file). Better to fail fast and point the operator at the real cause
# than to write to the wrong path.
if [ -z "$PID_FILE" ] || [ -z "$LOG_DIR" ]; then
  echo "ERROR: Service resolver failed — Phase 3 helper unreachable." >&2
  echo "  Could not resolve bot pid path / log dir via personas.services." >&2
  echo "  Check .claude/scripts/.venv (uv sync), PYTHONPATH, and that" >&2
  echo "  personas.services is importable. Re-run after fixing." >&2
  exit 1
fi
LOG_FILE="$LOG_DIR/bot.log"

# Kill existing bot — check pid file first, then delegate to the Python
# profile-aware cleanup helper. The Python helper uses psutil.environ() to
# read each candidate process's HOMIE_HOME and filters by exact match against
# the active profile. FAIL-CLOSED: when ownership cannot be proven, the
# helper does NOT kill (psutil missing or environ unreadable counts as
# "different profile" — killing across profiles is the larger evil).
_kill_existing() {
  # 1. Try pid file (cheap path — kills the canonical recorded PID).
  if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "Stopping old bot (PID $OLD_PID from pid file)..."
      kill "$OLD_PID" 2>/dev/null
      sleep 2
    fi
    rm -f "$PID_FILE"
  fi

  # 2. Delegate to the Python profile-aware cleanup helper. This reuses the
  # ``cleanup_all_bot_processes()`` codepath used by chat/main.py at startup
  # so we don't duplicate ownership logic in shell. The helper uses
  # psutil.Process(pid).environ()['HOMIE_HOME'] to verify ownership and only
  # kills processes that belong to THIS profile.
  KILLED_OUT=$("$VENV_PYTHON" -c "
import sys
sys.path.insert(0, r'$SCRIPTS_DIR')
from personas import apply_persona_override
apply_persona_override()
from shared import cleanup_all_bot_processes
killed = cleanup_all_bot_processes()
if killed:
    print(','.join(str(p) for p in killed))
" "$@" 2>/dev/null)
  if [ -n "$KILLED_OUT" ]; then
    echo "Stopped active-profile bots: $KILLED_OUT"
    sleep 2
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
  mkdir -p "$LOG_DIR"
  PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 "$VENV_PYTHON" "$SCRIPT_DIR/main.py" "$@" > "$LOG_FILE" 2>&1 &

  # Wait for bot to initialize
  sleep 5

  # Capture the real Windows PID of the python process (not the bash job wrapper).
  # On Windows/Git Bash, $! is the bash job PID, not the actual python.exe PID.
  # F1 fix — delegate to the profile-aware Python helper instead of a raw
  # cmdline scan. ``list_bot_pids_in_active_profile()`` uses psutil.environ()
  # to filter by HOMIE_HOME so a sibling profile's bot (started by another
  # operator) never gets recorded as ours.
  WIN_PID=""
  WIN_PID=$("$VENV_PYTHON" -c "
import sys
sys.path.insert(0, r'$SCRIPTS_DIR')
from personas import apply_persona_override
apply_persona_override()
from shared import list_bot_pids_in_active_profile
pids = list_bot_pids_in_active_profile()
if pids:
    print(max(pids))
" "$@" 2>/dev/null | tr -d '\r ')
  BOT_PID="${WIN_PID:-$!}"
  mkdir -p "$(dirname "$PID_FILE")"
  echo "$BOT_PID" > "$PID_FILE"
  # F1 (R2) — echo the resolved PID path symmetric to run_chat.bat. Lets
  # operators (and the wrapper-profile-flag test) confirm which profile the
  # wrapper actually resolved without reading the file directly.
  echo "PID file: $PID_FILE"

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
