"""Operator-run Postiz canary — proves the publishing lane without publishing.

Three checks against the CONFIGURED instance (POSTIZ_API_URL/KEY in .env):

  T1  status probe    — configured / reachable / auth_ok semantics
  T2  channel list    — connected integrations (ids for channels.yaml binding)
  T3  draft roundtrip — create a DRAFT-type post (never publishes), find it
                        via GET /posts, delete it. Verifies the live payload
                        contract end-to-end with zero external side effects.

Usage:
    cd .claude/scripts && uv run python social/postiz_canary.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Boot-shim: persona override + .env load BEFORE framework imports.
from personas import apply_persona_override  # noqa: E402

apply_persona_override()

import config  # noqa: E402,F401


def _z(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def main() -> int:
    from integrations import postiz_api

    # T1 — status probe (never raises; no network when unconfigured).
    status = postiz_api.get_status()
    print(
        f"T1 status: configured={status.configured} reachable={status.reachable} "
        f"auth_ok={status.auth_ok} channels={status.integrations_count}"
    )
    if not status.configured:
        print("FAIL: POSTIZ_API_URL / POSTIZ_API_KEY not set — nothing to canary.")
        return 1
    if not (status.reachable and status.auth_ok):
        print(f"FAIL: {status.error}")
        return 1

    # T2 — channel list.
    integrations = postiz_api.list_integrations()
    for item in integrations:
        flag = " (disabled)" if item.disabled else ""
        print(f"T2 channel: {item.identifier:22s} id={item.id}{flag}")
    if not integrations:
        print("WARN: no channels connected — connect one from the Social tab.")
        return 0

    # T3 — draft roundtrip on the first active channel. DRAFT type never
    # publishes; minimal settings shape keeps it platform-agnostic.
    target = next((i for i in integrations if not i.disabled), integrations[0])
    post_id = postiz_api.create_post(
        integration_id=target.id,
        content="[canary] The Homie Social lane check — safe to delete.",
        settings={"__type": target.identifier},
        post_type="draft",
    )
    now = datetime.now(timezone.utc)
    window = (_z(now - timedelta(days=1)), _z(now + timedelta(days=1)))
    found = any(
        str(p.get("id")) == post_id for p in postiz_api.list_posts(*window)
    )
    postiz_api.delete_post(post_id)
    gone = not any(
        str(p.get("id")) == post_id for p in postiz_api.list_posts(*window)
    )
    print(f"T3 draft roundtrip: created={bool(post_id)} listed={found} deleted={gone}")

    ok = bool(post_id) and found and gone
    print("CANARY " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
