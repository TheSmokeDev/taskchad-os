#!/usr/bin/env bash
# Liveness probe for the TaskChad OS stack.
# Exit 0 if every check passes, 1 otherwise.
set -uo pipefail

API_URL="${TASKCHAD_API_URL:-http://127.0.0.1:4322/health}"
VAULT_DIR="${TASKCHAD_VAULT:-vault}"
fail=0

check() {
  # check "<label>" <0-or-1 ok>
  local label="$1" ok="$2"
  if [[ "$ok" -eq 0 ]]; then
    printf '  [OK]   %s\n' "$label"
  else
    printf '  [DOWN] %s\n' "$label"
    fail=1
  fi
}

echo "TaskChad OS heartbeat — $(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 1. Local API health endpoint
if command -v curl >/dev/null 2>&1; then
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$API_URL" 2>/dev/null)"
  [[ "$code" == "200" ]]; check "local API ($API_URL -> ${code:-000})" $?
else
  check "local API (curl not installed)" 1
fi

# 2. Chat process alive
pgrep -f "run_chat.sh" >/dev/null 2>&1; check "chat process (run_chat.sh)" $?

# 3. Vault present and writable
[[ -d "$VAULT_DIR" && -w "$VAULT_DIR" ]]; check "vault writable ($VAULT_DIR)" $?

# 4. Disk space on the vault partition (warn under ~500MB free)
if avail_kb="$(df -Pk "$VAULT_DIR" 2>/dev/null | awk 'NR==2{print $4}')"; then
  [[ -n "$avail_kb" && "$avail_kb" -gt 512000 ]]
  check "disk free ($(( ${avail_kb:-0} / 1024 ))MB)" $?
else
  check "disk free (df failed)" 1
fi

echo
if [[ "$fail" -eq 0 ]]; then
  echo "Verdict: GREEN"
else
  echo "Verdict: RED"
fi
exit "$fail"
