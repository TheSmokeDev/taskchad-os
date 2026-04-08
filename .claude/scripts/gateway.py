"""
gateway.py — Secure local WebSocket gateway for YourTech Mission Control

Streams live Claude Code session data to the MC browser dashboard.

SECURITY (lessons from ClawJacked / CVE-2026-25253):
  - Token auth: pre-shared key verified on first message (not URL param — avoids logs)
  - Origin whitelist: only hub.your-domain.example.com accepted (blocks CSWSH)
  - Rate limiting: max 5 auth failures per 60s per IP
  - Bind to 127.0.0.1 only (never 0.0.0.0)
  - For VPS: Caddy terminates TLS; gateway listens on loopback only

Usage:
    cd .claude/scripts
    uv run python gateway.py                    # Start on default port 18789
    uv run python gateway.py --port 18789       # Explicit port
    uv run python gateway.py --host 0.0.0.0    # (VPS only, behind Caddy TLS)

Required env vars (in .env):
    MC_GATEWAY_TOKEN    Cryptographically random shared secret (32+ chars)
    MC_GATEWAY_ORIGINS  Comma-separated allowed origins (default: 
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import os
import sys
import time
from pathlib import Path

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("gateway")

# ── Env ──────────────────────────────────────────────────────────────────────
def load_env() -> dict[str, str]:
    env_path = Path(__file__).parent / ".env"
    env: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env

ENV = load_env()

GATEWAY_TOKEN   = ENV.get("MC_GATEWAY_TOKEN", "")
ALLOWED_ORIGINS = {
    o.strip() for o in ENV.get("MC_GATEWAY_ORIGINS", "https://localhost:3000").split(",")
    if o.strip()
}

# ── Security constants ────────────────────────────────────────────────────────
MAX_AUTH_FAILURES  = 5    # per IP per window
AUTH_WINDOW_S      = 60   # seconds
AUTH_TIMEOUT_S     = 10   # seconds to send auth token after connect
PUSH_INTERVAL_S    = 30   # how often to push session updates to clients

# ── In-memory rate limiter ────────────────────────────────────────────────────
# Maps IP → deque of failure timestamps
_auth_failures: dict[str, collections.deque] = collections.defaultdict(
    lambda: collections.deque(maxlen=MAX_AUTH_FAILURES + 5)
)

def _is_rate_limited(ip: str) -> bool:
    now = time.time()
    dq = _auth_failures[ip]
    # Remove old entries
    while dq and now - dq[0] > AUTH_WINDOW_S:
        dq.popleft()
    return len(dq) >= MAX_AUTH_FAILURES

def _record_failure(ip: str) -> None:
    _auth_failures[ip].append(time.time())

# ── Session scanning (reuses sync_sessions logic) ─────────────────────────────
CLAUDE_HOME  = Path(os.environ.get("MC_CLAUDE_HOME", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_HOME / "projects"

def _safe_parse_jsonl(p: Path, slug: str) -> dict | None:
    """Lightweight JSONL scanner — returns minimal session info."""
    try:
        import json as _json
        lines = [l for l in p.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
    except OSError:
        return None
    if not lines:
        return None

    session_id = model = None
    last_at = None
    msg_count = 0

    for raw in lines:
        try:
            e = _json.loads(raw)
        except Exception:
            continue
        if not session_id and e.get("sessionId"):
            session_id = e["sessionId"]
        if e.get("timestamp"):
            last_at = e["timestamp"]
        role = e.get("type")
        if role in ("user", "assistant"):
            msg_count += 1
            msg = e.get("message", {})
            if msg.get("model"):
                model = msg["model"]

    if not session_id:
        return None

    now = time.time()
    is_active = False
    if last_at:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(last_at.replace("Z", "+00:00"))
            is_active = (now - dt.timestamp()) < 300
        except Exception:
            pass

    return {
        "session_id":    session_id,
        "project_slug":  slug,
        "model":         model,
        "last_message_at": last_at,
        "message_count": msg_count,
        "is_active":     is_active,
    }

def scan_sessions() -> list[dict]:
    if not PROJECTS_DIR.exists():
        return []
    sessions = []
    for proj in PROJECTS_DIR.iterdir():
        if not proj.is_dir():
            continue
        for jsonl in proj.glob("*.jsonl"):
            s = _safe_parse_jsonl(jsonl, proj.name)
            if s:
                sessions.append(s)
    return sessions

# ── WebSocket handler ─────────────────────────────────────────────────────────
async def handle_client(websocket, path=None):
    """Authenticate and stream session data to a connected client."""
    # Retrieve the remote IP — works with websockets ≥ 10
    try:
        ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
    except Exception:
        ip = "unknown"

    # 1. Check rate limit before doing anything
    if _is_rate_limited(ip):
        log.warning("Rate-limited connection rejected from %s", ip)
        await websocket.close(1008, "Rate limited")
        return

    # 2. Validate Origin header (block CSWSH)
    origin = websocket.request_headers.get("Origin", "")
    if ALLOWED_ORIGINS and origin not in ALLOWED_ORIGINS:
        log.warning("Rejected connection from disallowed origin %r (ip=%s)", origin, ip)
        _record_failure(ip)
        await websocket.close(1008, "Origin not allowed")
        return

    log.info("New connection from %s (origin=%s)", ip, origin)

    # 3. Token auth: client must send {"type":"auth","token":"..."} within 10s
    if GATEWAY_TOKEN:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT_S)
            msg = json.loads(raw)
        except asyncio.TimeoutError:
            log.warning("Auth timeout from %s", ip)
            _record_failure(ip)
            await websocket.close(1008, "Auth timeout")
            return
        except Exception:
            log.warning("Invalid auth message from %s", ip)
            _record_failure(ip)
            await websocket.close(1008, "Invalid auth")
            return

        # Constant-time compare
        import hmac
        provided = str(msg.get("token", ""))
        if not hmac.compare_digest(provided, GATEWAY_TOKEN):
            log.warning("Bad token from %s", ip)
            _record_failure(ip)
            await websocket.close(1008, "Unauthorized")
            return

    log.info("Authenticated client from %s", ip)
    await websocket.send(json.dumps({"type": "auth_ok", "ts": int(time.time())}))

    # 4. Push session data loop
    try:
        while True:
            sessions = scan_sessions()
            payload = {
                "type":     "sessions",
                "sessions": sessions,
                "active":   sum(1 for s in sessions if s["is_active"]),
                "total":    len(sessions),
                "ts":       int(time.time()),
            }
            await websocket.send(json.dumps(payload))
            await asyncio.sleep(PUSH_INTERVAL_S)
    except Exception:
        # Client disconnected or error — normal exit
        pass
    finally:
        log.info("Client from %s disconnected", ip)

# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="YourTech MC WebSocket gateway")
    parser.add_argument("--host", default="127.0.0.1",
        help="Bind host. Use 127.0.0.1 (default, behind Caddy) or 0.0.0.0 only on secured infra.")
    parser.add_argument("--port", type=int, default=18789)
    args = parser.parse_args()

    if not GATEWAY_TOKEN:
        log.warning(
            "MC_GATEWAY_TOKEN is not set in .env — gateway accepts any connection. "
            "Set a strong random token (e.g. python -c \"import secrets; print(secrets.token_hex(32))\")."
        )

    if not ALLOWED_ORIGINS:
        log.warning("MC_GATEWAY_ORIGINS is empty — all origins accepted (insecure).")

    log.info(
        "Starting gateway on %s:%d | allowed origins: %s",
        args.host, args.port, ALLOWED_ORIGINS or "ALL"
    )

    try:
        import websockets
    except ImportError:
        print("ERROR: websockets not installed. Run: uv pip install websockets", file=sys.stderr)
        sys.exit(1)

    async def serve():
        async with websockets.serve(handle_client, args.host, args.port):
            log.info("Gateway ready — waiting for connections")
            await asyncio.Future()  # run forever

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        log.info("Gateway stopped")

if __name__ == "__main__":
    main()
