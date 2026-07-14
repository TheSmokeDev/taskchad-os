"""Browser Homie runner — one-shot out-of-process social post dispatcher.

The chat bot must never drive a browser on its event loop (the 2026-07-13
wedge: a hung agent-browser child froze Telegram, Discord, /health, and the
liveness supervisor at once). The bot only gates, audits, CAS-claims, and
spawns THIS process detached; the runner executes the existing dispatch stack
(post_executor -> BrowserExecutor -> social_write_driver -> agent-browser)
and reports the receipt back to Telegram cross-process via social.notify.

Modes:
    --post-id N [--claimed]   dispatch one post. Without --claimed the runner
                              CAS-claims first and exits 0 (no-op) if it loses
                              the race; --claimed means the spawner already
                              holds the claim for this row.
    --sweep                   stale-claim sweep: rows claimed longer than
                              SOCIAL_RUNNER_CLAIM_TTL_MIN (default 15) that
                              are still 'approved' -> mark failed + notify.

The runner never approves anything: dispatch_post() refuses non-approved rows
and the require_integration_action default-deny gate stays inside
_dispatch_browser. This script must never contain bot lifecycle commands
(Cron Lifecycle Guard).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# Boot-shim: must run BEFORE any framework imports so profile paths resolve.
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

def _log(message: str) -> None:
    print(f"[{datetime.now()}] [browser-homie-runner] {message}", flush=True)


def _send_receipt(text: str) -> None:
    """Cross-process Telegram receipt. Fail-open — a dead receipt never
    changes the dispatch outcome."""
    try:
        from social.notify import send_text_to_telegram

        if not send_text_to_telegram(text):
            _log(f"receipt not delivered: {text}")
    except Exception as exc:  # noqa: BLE001
        _log(f"receipt failed: {type(exc).__name__}: {exc}")


def run_post(post_id: int, *, claimed: bool, db_path: str | None = None) -> int:
    from integrations.capabilities import IntegrationPolicyError
    from social.post_executor import dispatch_post
    from social.service import SocialPostService

    svc = SocialPostService(db_path=db_path)
    post = svc.get_post(post_id)
    if post is None:
        _log(f"post {post_id} not found")
        return 1

    if not claimed and not svc.claim_post(post_id):
        # Someone else owns it (double-tap, cron race) — a no-op, not an error.
        _log(f"post {post_id} already claimed/dispatched — exiting")
        return 0

    label = f"#{post_id} ({post.channel})"
    try:
        # Serialization against other drives happens downstream: the
        # browser_write_lock lives inside post_executor._dispatch_browser,
        # the chokepoint every ingress shares.
        _log(f"dispatching {label}")
        ok = dispatch_post(post_id, db_path=db_path)
    except IntegrationPolicyError as exc:
        _log(f"{label}: blocked by default-deny gate: {exc}")
        _send_receipt(f"⛔ Post {label} blocked by policy: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        _log(f"{label}: dispatch crashed: {type(exc).__name__}: {exc}")
        try:
            refreshed = svc.get_post(post_id)
            if refreshed is not None and refreshed.status == "approved":
                svc.mark_failed(post_id, error=f"runner crash: {exc}")
        except Exception:  # noqa: BLE001
            pass
        _send_receipt(f"❌ Post {label} failed: {type(exc).__name__}: {exc}")
        return 1

    refreshed = svc.get_post(post_id)
    if ok:
        url = (refreshed.post_url if refreshed else "") or "n/a"
        _log(f"{label}: posted — {url}")
        _send_receipt(f"✅ Posted {label}. URL: {url}")
        return 0
    error = (refreshed.error if refreshed else "") or "unknown error"
    _log(f"{label}: failed — {error}")
    _send_receipt(f"❌ Post {label} failed: {error}")
    return 1


def run_sweep(*, db_path: str | None = None) -> int:
    from social.post_executor import sweep_stale_claims

    summary = sweep_stale_claims(db_path=db_path)
    _log(f"sweep: {summary}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Browser Homie social post runner")
    parser.add_argument("--post-id", type=int, help="dispatch this approved post")
    parser.add_argument(
        "--claimed",
        action="store_true",
        help="the spawner already CAS-claimed this post",
    )
    parser.add_argument("--sweep", action="store_true", help="sweep stale claims")
    parser.add_argument("--db-path", default=None, help="override queue DB path (tests)")
    args = parser.parse_args()

    if args.sweep:
        return run_sweep(db_path=args.db_path)
    if args.post_id is not None:
        return run_post(args.post_id, claimed=args.claimed, db_path=args.db_path)
    parser.error("one of --post-id or --sweep is required")
    return 2  # unreachable — parser.error exits


if __name__ == "__main__":
    sys.exit(main())
