#!/usr/bin/env bash
# bot-status.sh — reliable one-shot health check for The Homie bot.
# Checks pid file AND scans by process cmdline. Never trusts only one source.
#
# PRP-7c Phase 3 (lifecycle-surfaces): pid path / log dir resolved through
# personas.services so the script follows the active profile.
#
# Flags:
#   --kill-all-homies    Operator-driven legacy "kill every Homie bot in this
#                        repo" behavior. Use only when you actually want to
#                        clear bots from ALL profiles. Automatic startup uses
#                        the profile-aware cleanup_all_bot_processes() instead.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPTS_DIR="$SCRIPT_DIR/../scripts"

# F1 (R2) — pre-parse --profile/-p/--profile=NAME and export HOMIE_HOME BEFORE
# the resolver subprocess runs. See run_chat.sh for the full justification.
# Without this, ``bash bot-status.sh --profile sales`` reports on the DEFAULT
# profile's paths/pid file rather than the requested profile's.
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
    unset HOMIE_HOME
  else
    _HOMIE_TARGET="${HOME}/.homie/profiles/${_HOMIE_PROFILE_OVERRIDE}"
    if [ ! -d "$_HOMIE_TARGET" ]; then
      echo "ERROR: Profile '$_HOMIE_PROFILE_OVERRIDE' not found at $_HOMIE_TARGET" >&2
      exit 1
    fi
    export HOMIE_HOME="$_HOMIE_TARGET"
  fi
fi

# Optional flag: --kill-all-homies — operator-driven broad cleanup.
KILL_ALL_HOMIES=0
if [ "$1" = "--kill-all-homies" ]; then
  KILL_ALL_HOMIES=1
fi

# Issue #34 — --dry-run/--paths: print resolved paths and exit before the
# kill branch and the status scan. Scans the whole argv so it composes with
# --profile.
_HOMIE_DRY_RUN=0
for arg in "$@"; do
  case "$arg" in
    --dry-run|--paths) _HOMIE_DRY_RUN=1 ;;
  esac
done

# Resolve venv python (Windows path takes priority on Git Bash).
if [ -f "$SCRIPTS_DIR/.venv/Scripts/python.exe" ]; then
  VENV_PYTHON="$SCRIPTS_DIR/.venv/Scripts/python.exe"
elif [ -f "$SCRIPTS_DIR/.venv/bin/python" ]; then
  VENV_PYTHON="$SCRIPTS_DIR/.venv/bin/python"
else
  VENV_PYTHON="python"
fi

export PYTHONPATH="$SCRIPTS_DIR${PYTHONPATH:+:$PYTHONPATH}"

# Issue #34 — path translation. Git Bash (MSYS) converts whole-arg POSIX
# paths before Windows python.exe sees them, but WSL bash passes /mnt/c/...
# verbatim (Windows python reads it as C:\mnt\c\...). The embedded `r'...'`
# sys.path strings in the resolver program text are NOT arg-converted by
# MSYS either, so normalize to Windows form whenever the target python is
# a Windows .exe. Mirrors run_chat.sh.
_to_win_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1" 2>/dev/null || echo "$1"
  elif command -v wslpath >/dev/null 2>&1; then
    wslpath -w "$1" 2>/dev/null || echo "$1"
  else
    echo "$1"
  fi
}
case "$VENV_PYTHON" in
  *.exe)
    SCRIPTS_DIR_PY="$(_to_win_path "$SCRIPTS_DIR")"
    VENV_PYTHON_WIN="$(_to_win_path "$VENV_PYTHON")"
    ;;
  *)
    SCRIPTS_DIR_PY="$SCRIPTS_DIR"
    VENV_PYTHON_WIN="$VENV_PYTHON"
    ;;
esac

# Resolve profile-aware paths via personas.services. Two newline-separated
# paths: pid file, log dir.
#
# F1 (R3) — forward the wrapper's argv ("$@") to the subprocess so
# apply_persona_override() can pre-parse rank-1 (CLI flag). Without this,
# the resolver sees sys.argv=['-c'] and falls through to rank-3 (sticky
# ~/.homie/active_profile), so `bot-status.sh --profile default` reports
# sticky-sales paths instead of default's. See run_chat.sh for the full
# justification.
_PATHS=$("$VENV_PYTHON" -c "
import sys
sys.path.insert(0, r'$SCRIPTS_DIR_PY')
from personas import apply_persona_override
apply_persona_override()
from personas.services import get_bot_pid_path, get_log_dir
print(get_bot_pid_path())
print(get_log_dir())
" "$@" 2>/dev/null)

# Issue #34 — explicit `tr -d '\r'` CR hardening. MSYS2 sed strips the
# trailing CR from the resolver's CRLF output by accident; WSL/non-MSYS sed
# does not, leaving a \r that breaks `test -f` and the stale-pid grep.
PID_FILE=$(echo "$_PATHS" | sed -n '1p' | tr -d '\r')
LOG_DIR=$(echo "$_PATHS" | sed -n '2p' | tr -d '\r')

# F4 — fail loudly if the service resolver could not run. Hardcoded
# install-dir fallbacks were removed because they ship the wrong path for
# named profiles. If the resolver is unreachable, the rest of this script
# would report on the wrong files; better to bail with a clear message.
if [ -z "$PID_FILE" ] || [ -z "$LOG_DIR" ]; then
  echo "ERROR: Service resolver failed — Phase 3 helper unreachable." >&2
  echo "  Could not resolve bot pid path / log dir via personas.services." >&2
  echo "  Check .claude/scripts/.venv (uv sync), PYTHONPATH, and that" >&2
  echo "  personas.services is importable. Re-run after fixing." >&2
  exit 1
fi
LOG_FILE="$LOG_DIR/bot.log"

# Issue #34 — DRY RUN exit. MUST stay ABOVE the --kill-all-homies branch so
# `bot-status.sh --dry-run` can never reach a kill path.
if [ "$_HOMIE_DRY_RUN" = "1" ]; then
  echo "DRY RUN: read-only path resolution"
  echo "PYTHON: $VENV_PYTHON_WIN"
  echo "PID_FILE: $PID_FILE"
  echo "LOG_FILE: $LOG_FILE"
  exit 0
fi

# Operator-driven broad cleanup path — bypass health check, run legacy
# kill-all-homies, exit.
if [ "$KILL_ALL_HOMIES" = "1" ]; then
  echo "==[ --kill-all-homies: legacy broad cleanup across ALL profiles ]=="
  killed=$("$VENV_PYTHON" -c "
import sys
sys.path.insert(0, r'$SCRIPTS_DIR_PY')
from personas import apply_persona_override
apply_persona_override()
from shared import cleanup_all_homie_bots_in_repo
print(','.join(str(p) for p in cleanup_all_homie_bots_in_repo()))
" "$@" 2>/dev/null)
  if [ -n "$killed" ]; then
    echo "  Killed PIDs: $killed"
  else
    echo "  No Homie bots found in this repo."
  fi
  exit 0
fi

pid_file_pid=""
pid_file_alive=false
cmdline_pids=()

# 1. Check pid file
if [ -f "$PID_FILE" ]; then
  # Issue #34 — strip CR/space: a CRLF-written pid file makes `kill -0`
  # fail silently → false [STALE] verdicts and a grep -qx mismatch below.
  pid_file_pid=$(tr -d '\r ' < "$PID_FILE")
  if kill -0 "$pid_file_pid" 2>/dev/null; then
    pid_file_alive=true
  fi
fi

# 2. Profile-aware cmdline scan — delegate to Python helper.
# F1 fix: ``list_bot_pids_in_active_profile()`` reads each candidate
# python process's HOMIE_HOME via psutil.environ() and filters by exact
# match against the active profile's HOMIE_HOME. Two profile bots running
# simultaneously now report only the one belonging to THIS profile, not
# both as "duplicate instances".
#
# FAIL-CLOSED: when ownership cannot be proven (psutil missing, environ
# unreadable), the helper EXCLUDES the PID — better to under-report than
# to mistakenly flag a sibling profile's bot as a duplicate of ours.
while IFS= read -r pid; do
  pid=$(echo "$pid" | tr -d '\r ')
  [ -n "$pid" ] && cmdline_pids+=("$pid")
done < <("$VENV_PYTHON" -c "
import sys
sys.path.insert(0, r'$SCRIPTS_DIR_PY')
from personas import apply_persona_override
apply_persona_override()
from shared import list_bot_pids_in_active_profile
for pid in list_bot_pids_in_active_profile():
    print(pid)
" "$@" 2>/dev/null)

# 3. Report
echo "=============================="
echo "  Homie Bot Status"
echo "=============================="
echo "  PID path:  $PID_FILE"

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
    echo "  WARNING: $PID_FILE ($pid_file_pid) doesn't match live process(es) — pid file is stale"
  fi
else
  echo "  Cmdline scan: no bot process found"
fi

echo ""

# 4. Overall verdict
if [ ${#cmdline_pids[@]} -gt 0 ]; then
  echo "  STATUS: RUNNING (${#cmdline_pids[@]} instance(s))"
  if [ ${#cmdline_pids[@]} -gt 1 ]; then
    echo "  DUPLICATE INSTANCES DETECTED — run run_chat.sh to clean up"
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
