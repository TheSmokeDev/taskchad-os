"""Social dashboard assembly — the logic behind /api/social/* routes.

Keeps ``dashboard_api.py`` handlers thin (same split as browser_control /
dashboard_bot_lifecycle). Read surfaces degrade gracefully when Postiz is
unconfigured/unreachable — the tab renders an onboarding state instead of
a stack trace. Mutations here are STATE TRANSITIONS ONLY (draft/approve/
reject); publishing happens exclusively through the gated
``social/post_executor.py`` dispatch path.

Connect-URL hygiene: OAuth URLs are sensitive and expire. They travel in
the response body only — never logged, never audited (the audit row
records the provider name, not the URL).
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def _iso_z(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_social_status() -> dict[str, Any]:
    """Status card: Postiz probe (booleans/counts only) + queue counts."""
    from integrations import postiz_api
    from social.service import SocialPostService

    status = postiz_api.get_status()
    friendly = ""
    if status.configured and not (status.reachable and status.auth_ok):
        if not status.reachable:
            friendly = postiz_api.PostizUnreachable.friendly_message
        else:
            friendly = postiz_api.PostizAuthFailure.friendly_message

    try:
        queue_counts = SocialPostService().count_by_status()
    except Exception:
        queue_counts = {}

    # The instance's web UI origin (backend origin minus the /api suffix) —
    # powers the embedded Studio view. Admin-only surface; the operator's
    # own instance address, not a credential.
    studio_url = ""
    if status.configured:
        import config

        api_url = config.get_postiz_settings().api_url.rstrip("/")
        studio_url = api_url[:-4] if api_url.endswith("/api") else api_url

    return {
        "postiz": {
            "configured": status.configured,
            "reachable": status.reachable,
            "auth_ok": status.auth_ok,
            "integrations_count": status.integrations_count,
            "error": friendly,
        },
        "studio_url": studio_url,
        "queue": queue_counts,
        "cadence_enabled": os.getenv("SOCIAL_CADENCE_ENABLED", "false").lower()
        == "true",
    }


def build_channels_view() -> dict[str, Any]:
    """channels.yaml registry merged with the instance's connected channels."""
    from integrations import postiz_api
    from social.channels import list_channels

    channels = [
        {
            "channel_id": ch.channel_id,
            "display_name": ch.display_name,
            "execution_method": ch.execution_method,
            "cadence_enabled": ch.cadence_enabled,
            "cadence_interval_hours": ch.cadence_interval_hours,
            "postiz_integration_id": ch.postiz_integration_id,
            "postiz_bound": bool(ch.postiz_integration_id),
        }
        for ch in list_channels()
    ]

    integrations: list[dict[str, Any]] = []
    postiz_error = ""
    try:
        integrations = [
            {
                "id": item.id,
                "name": item.name,
                "identifier": item.identifier,
                "picture": item.picture,
                "disabled": item.disabled,
                "profile": item.profile,
            }
            for item in postiz_api.list_integrations()
        ]
    except postiz_api.PostizAPIError as exc:
        postiz_error = exc.friendly_message

    return {
        "channels": channels,
        "postiz_integrations": integrations,
        "postiz_error": postiz_error,
    }


def build_queue_view(limit: int = 20) -> dict[str, Any]:
    from social.service import SocialPostService

    svc = SocialPostService()
    return {
        "posts": [asdict(p) for p in svc.list_queue(limit=limit)],
        "counts": svc.count_by_status(),
    }


def build_posts_view(days: int = 7) -> dict[str, Any]:
    """Postiz-side posts (scheduled + published) over a +/- window."""
    from integrations import postiz_api

    days = max(1, min(int(days), 30))
    now = datetime.now(timezone.utc)
    try:
        posts = postiz_api.list_posts(
            _iso_z(now - timedelta(days=days)),
            _iso_z(now + timedelta(days=days)),
        )
    except postiz_api.PostizAPIError as exc:
        return {"posts": [], "postiz_error": exc.friendly_message}
    slim = [
        {
            "id": str(p.get("id", "")),
            "content": str(p.get("content", "") or "")[:280],
            "publishDate": p.get("publishDate"),
            "state": p.get("state"),
            "releaseURL": p.get("releaseURL"),
            "integration": {
                "id": str((p.get("integration") or {}).get("id", "")),
                "providerIdentifier": (p.get("integration") or {}).get(
                    "providerIdentifier"
                ),
                "name": (p.get("integration") or {}).get("name"),
            },
        }
        for p in posts
    ]
    return {"posts": slim, "postiz_error": ""}


def compose_draft(
    channel: str,
    title: str,
    body: str,
    scheduled_for: str | None = None,
) -> dict[str, Any]:
    """Create a DRAFT queue row — the approval pipeline is the only path
    from here to a publish."""
    from social.audit import append_social_audit_record
    from social.channels import get_channel
    from social.service import SocialPostService

    channel = (channel or "").strip().lower()
    if get_channel(channel) is None:
        raise ValueError(f"Unknown channel: {channel}")
    if not (body or "").strip():
        raise ValueError("Post body is required")

    post_id = SocialPostService().create_draft(
        channel=channel,
        title=(title or "").strip(),
        body=body.strip(),
        topic_source="dashboard",
        scheduled_for=scheduled_for or None,
    )
    append_social_audit_record(
        channel=channel,
        action="draft",
        post_id=post_id,
        outcome="created",
        operator="dashboard",
        body_preview=body,
    )
    return {"id": post_id, "status": "draft"}


def approve_post(post_id: int) -> dict[str, Any]:
    """Approve AND immediately dispatch through the gated executor.

    Parity with the Telegram "Approve & Post" button (operator decision
    2026-07-06): the dashboard tap IS the per-post operator confirmation.
    The default-deny gate + audit rows still run inside dispatch_post —
    this changes who taps, not what gets checked.
    """
    from integrations.capabilities import IntegrationPolicyError
    from social.audit import append_social_audit_record
    from social.post_executor import dispatch_post
    from social.service import SocialPostService

    svc = SocialPostService()
    post = svc.approve_post(post_id)
    append_social_audit_record(
        channel=post.channel,
        action="approve",
        post_id=post_id,
        outcome="approved",
        operator="dashboard",
    )

    try:
        dispatched = dispatch_post(post_id)
    except IntegrationPolicyError as exc:
        # dispatch_post already marked the row failed + wrote the blocked audit.
        return {
            "id": post_id,
            "status": "failed",
            "dispatched": False,
            "error": str(exc),
        }

    updated = svc.get_post(post_id)
    return {
        "id": post_id,
        "status": updated.status if updated else "unknown",
        "dispatched": bool(dispatched),
        "post_url": (updated.post_url if updated else "") or "",
        "error": (updated.error if updated else "") or "",
    }


def reject_post(post_id: int, reason: str = "") -> dict[str, Any]:
    from social.audit import append_social_audit_record
    from social.service import SocialPostService

    post = SocialPostService().reject_post(post_id, reason=reason)
    append_social_audit_record(
        channel=post.channel,
        action="reject",
        post_id=post_id,
        outcome="rejected",
        operator="dashboard",
    )
    return {"id": post_id, "status": post.status}


def run_reconcile() -> dict[str, Any]:
    """On-demand reconcile pass (same one the cadence tick runs).

    Lets the dashboard chase a just-published post's live URL instead of
    waiting for the next scheduled tick. Idempotent; list-endpoints only
    (never counts against the create-post rate budget).
    """
    from social.postiz_reconcile import reconcile_postiz_posts

    return reconcile_postiz_posts()


def connect_url(provider: str) -> dict[str, Any]:
    """Fresh OAuth connect URL for a provider. Response-body only — the
    audit row carries the provider name, never the URL."""
    from integrations import postiz_api
    from social.audit import append_social_audit_record

    provider = (provider or "").strip().lower()
    if not _PROVIDER_RE.fullmatch(provider):
        raise ValueError("Invalid provider name")

    url = postiz_api.get_connect_url(provider)
    append_social_audit_record(
        channel=provider,
        action="connect_url",
        outcome="issued",
        operator="dashboard",
    )
    return {"url": url}
