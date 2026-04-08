"""
sync_sessions.py — Push local Claude Code sessions to MC Supabase

Scans ~/.claude/projects/*.jsonl and upserts to mc_claude_sessions via
the mc_bulk_sync_claude_sessions Supabase RPC.

Run manually or wire into the heartbeat / Windows Task Scheduler.

Usage:
    cd .claude/scripts
    uv run python sync_sessions.py
    uv run python sync_sessions.py --dry-run   # Print what would be synced
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
CLAUDE_HOME = Path(os.environ.get("MC_CLAUDE_HOME", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_HOME / "projects"

# ── Supabase config (from .env) ─────────────────────────────────────────────
def load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return {}
    env = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env

ENV = load_env()
SUPABASE_URL = ENV.get("NEXT_PUBLIC_SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = ENV.get("SUPABASE_SERVICE_ROLE_KEY", "")

# ── Pricing (USD per token) ────────────────────────────────────────────────
PRICING = {
    "claude-opus-4-6":    {"input": 15 / 1_000_000, "output": 75 / 1_000_000},
    "claude-sonnet-4-6":  {"input": 3  / 1_000_000, "output": 15 / 1_000_000},
    "claude-haiku-4-5":   {"input": 0.8 / 1_000_000, "output": 4 / 1_000_000},
}
DEFAULT_PRICING = {"input": 3 / 1_000_000, "output": 15 / 1_000_000}
ACTIVE_THRESHOLD_S = 300  # 5 min
RECENT_THRESHOLD_DAYS = 2   # only sync sessions active within last 48h
MIN_MESSAGES = 4             # skip trivially-short sessions (noise)


def parse_session(jsonl_path: Path, project_slug: str) -> dict | None:
    try:
        lines = [l for l in jsonl_path.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
    except OSError:
        return None

    if not lines:
        return None

    session_id = model = git_branch = project_path = None
    first_at = last_at = last_user_prompt = None
    user_msgs = assistant_msgs = tool_uses = 0
    input_tok = output_tok = cache_read = cache_create = 0

    for raw in lines:
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if not session_id and e.get("sessionId"):
            session_id = e["sessionId"]
        if not git_branch and e.get("gitBranch"):
            git_branch = e["gitBranch"]
        if not project_path and e.get("cwd"):
            project_path = e["cwd"]

        ts = e.get("timestamp")
        if ts:
            if not first_at:
                first_at = ts
            last_at = ts

        if e.get("isSidechain"):
            continue

        msg = e.get("message", {})
        role = e.get("type")

        if role == "user":
            user_msgs += 1
            content = msg.get("content", "")
            if isinstance(content, str) and content:
                last_user_prompt = content[:500]

        elif role == "assistant":
            assistant_msgs += 1
            if msg.get("model"):
                model = msg["model"]
            usage = msg.get("usage", {})
            input_tok  += usage.get("input_tokens", 0)
            cache_read  += usage.get("cache_read_input_tokens", 0)
            cache_create += usage.get("cache_creation_input_tokens", 0)
            output_tok += usage.get("output_tokens", 0)
            for block in (msg.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_uses += 1

    if not session_id:
        return None

    p = PRICING.get(model, DEFAULT_PRICING) if model else DEFAULT_PRICING
    cost = (
        input_tok  * p["input"] +
        cache_read  * p["input"] * 0.1 +
        cache_create * p["input"] * 1.25 +
        output_tok * p["output"]
    )

    now = time.time()
    is_active = False
    last_dt_ts = None
    if last_at:
        try:
            from datetime import datetime, timezone
            last_dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            last_dt_ts = last_dt.timestamp()
            is_active = (now - last_dt_ts) < ACTIVE_THRESHOLD_S
        except Exception:
            pass

    # Noise vs signal: skip old and trivially short sessions
    if last_dt_ts is not None and (now - last_dt_ts) > RECENT_THRESHOLD_DAYS * 86400:
        return None
    if last_dt_ts is None and not is_active:
        return None
    total_msgs = user_msgs + assistant_msgs
    if total_msgs < MIN_MESSAGES:
        return None

    return {
        "session_id":        session_id,
        "project_slug":      project_slug,
        "project_path":      project_path,
        "model":             model,
        "git_branch":        git_branch,
        "user_messages":     user_msgs,
        "assistant_messages": assistant_msgs,
        "tool_uses":         tool_uses,
        "input_tokens":      input_tok + cache_read + cache_create,
        "output_tokens":     output_tok,
        "estimated_cost":    round(cost, 6),
        "first_message_at":  first_at,
        "last_message_at":   last_at,
        "last_user_prompt":  last_user_prompt,
        "is_active":         is_active,
        "scanned_at":        int(now),
    }


def scan_sessions() -> list[dict]:
    if not PROJECTS_DIR.exists():
        print(f"[sync_sessions] No projects dir at {PROJECTS_DIR}", file=sys.stderr)
        return []

    sessions = []
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        slug = project_dir.name
        for jsonl in project_dir.glob("*.jsonl"):
            s = parse_session(jsonl, slug)
            if s:
                sessions.append(s)

    return sessions


def push_to_supabase(sessions: list[dict]) -> bool:
    """Call mc_bulk_sync_claude_sessions RPC to upsert all sessions."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("[sync_sessions] ERROR: NEXT_PUBLIC_SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set", file=sys.stderr)
        return False

    try:
        import urllib.request
        url = f"{SUPABASE_URL}/rest/v1/rpc/mc_bulk_sync_claude_sessions"
        payload = json.dumps({"p_sessions": sessions}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "apikey": SUPABASE_SERVICE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "Prefer": "return=representation",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            print(f"[sync_sessions] Supabase RPC OK: {body[:200]}")
            return True
    except Exception as e:
        print(f"[sync_sessions] Supabase RPC failed: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Sync local Claude Code sessions to Supabase")
    parser.add_argument("--dry-run", action="store_true", help="Print sessions without pushing")
    args = parser.parse_args()

    print(f"[sync_sessions] Scanning {PROJECTS_DIR} ...")
    sessions = scan_sessions()
    print(f"[sync_sessions] Found {len(sessions)} session(s), {sum(1 for s in sessions if s['is_active'])} active")

    if args.dry_run:
        for s in sessions[:5]:
            print(f"  {s['session_id'][:8]}  {s['project_slug']}  {s['model']}  active={s['is_active']}")
        if len(sessions) > 5:
            print(f"  ... ({len(sessions) - 5} more)")
        return

    if not sessions:
        print("[sync_sessions] Nothing to sync")
        return

    ok = push_to_supabase(sessions)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
