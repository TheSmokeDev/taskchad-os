"""Postiz publish-outcome reconciliation.

Postiz has NO webhooks: ``POST /posts`` acceptance only means the post was
enqueued (Temporal publishes asynchronously). Dispatch therefore marks the
queue row ``posted`` optimistically with ``external_ref="postiz:<id>"`` and
an empty ``post_url``; this pass closes the loop by polling
``GET /posts`` and either

* filling ``post_url`` from ``releaseURL`` when the platform confirms
  (state ``PUBLISHED``), or
* demoting ``posted -> failed`` + audit + fail-open Telegram notify when
  the platform errored (state ``ERROR``).

Rows still ``QUEUE``/``DRAFT`` stay pending for the next tick. Hooked into
``social/cadence.py`` after dispatch, guarded by
``config.get_postiz_settings().configured`` and wrapped fail-open — a
reconcile failure never breaks the cadence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 48
_EXTERNAL_REF_PREFIX = "postiz:"


def _parse_posted_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def reconcile_postiz_posts(
    *,
    db_path: str | Path | None = None,
    client=None,
    window_hours: int | None = None,
) -> dict:
    """Reconcile optimistic Postiz rows against actual platform outcomes.

    Returns {"checked": n, "confirmed": n, "failed": n, "pending": n,
    "errors": [...]}.
    """
    import config
    from integrations import postiz_api
    from social.audit import append_social_audit_record
    from social.service import SocialPostService

    summary: dict = {
        "checked": 0,
        "confirmed": 0,
        "failed": 0,
        "pending": 0,
        "errors": [],
    }

    if window_hours is None:
        window_hours = DEFAULT_WINDOW_HOURS

    settings = config.get_postiz_settings()
    if not settings.configured:
        summary["skipped"] = "postiz not configured"
        return summary

    svc = SocialPostService(db_path=db_path)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)

    candidates = []
    for post in svc.list_by_status("posted", limit=200):
        ref = post.external_ref or ""
        if not ref.startswith(_EXTERNAL_REF_PREFIX):
            continue
        if post.post_url:
            continue  # already confirmed
        posted_at = _parse_posted_at(post.posted_at)
        if posted_at is not None and posted_at < cutoff:
            continue  # aged out — stop polling forever
        candidates.append(post)

    if not candidates:
        return summary

    start_iso = cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    end_iso = (now + timedelta(hours=1)).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    try:
        remote_posts = postiz_api.list_posts(start_iso, end_iso, client=client)
    except postiz_api.PostizAPIError as exc:
        summary["errors"].append(f"list_posts: {exc}")
        return summary

    remote_by_id = {str(p.get("id", "")): p for p in remote_posts}

    for post in candidates:
        summary["checked"] += 1
        postiz_id = (post.external_ref or "")[len(_EXTERNAL_REF_PREFIX):]
        remote = remote_by_id.get(postiz_id)
        if remote is None:
            summary["pending"] += 1
            continue

        state = str(remote.get("state", "")).upper()
        if state == "PUBLISHED":
            release_url = str(remote.get("releaseURL", "") or "")
            svc.set_post_fields(post.id, post_url=release_url or None)
            append_social_audit_record(
                channel=post.channel,
                action="reconcile",
                post_id=post.id,
                outcome="success",
                post_url=release_url,
            )
            summary["confirmed"] += 1
        elif state == "ERROR":
            error = f"Postiz publish failed (post {postiz_id})"
            try:
                svc.mark_failed(post.id, error=error)
            except ValueError as exc:
                summary["errors"].append(f"post {post.id}: {exc}")
                continue
            append_social_audit_record(
                channel=post.channel,
                action="reconcile",
                post_id=post.id,
                outcome="failed",
                error=error,
            )
            summary["failed"] += 1
            try:
                from social.notify import send_text_to_telegram

                send_text_to_telegram(
                    f"⚠️ Social post #{post.id} ({post.channel}) failed to "
                    f"publish via Postiz. Check the Social tab or /social queue."
                )
            except Exception as exc:  # fail-open — notify never breaks reconcile
                logger.warning("Reconcile notify failed: %s", exc)
        else:
            summary["pending"] += 1

    return summary
