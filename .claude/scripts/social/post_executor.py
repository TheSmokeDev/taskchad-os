"""Unified social post executor — dispatches approved posts to channel executors.

LinkedIn: existing BrowserExecutor
Facebook: direct API (social_media.post_to_facebook)
X: manual only (no dispatch — draft is delivered via Telegram)

All external posts are gated via require_integration_action() with pre-send audit records.
"""

from __future__ import annotations

import logging
from pathlib import Path

from integrations.capabilities import IntegrationPolicyError, require_integration_action
from social.audit import append_social_audit_record
from social.channels import get_channel
from social.service import SocialPostService

logger = logging.getLogger(__name__)


def dispatch_post(
    post_id: int,
    *,
    db_path: str | Path | None = None,
) -> bool:
    """Dispatch an approved post to its channel executor.

    Returns True on success, False on failure. Updates the queue record
    and writes an audit row in both cases. Raises IntegrationPolicyError
    (after cleanup) when the default-deny gate blocks the post.
    """
    svc = SocialPostService(db_path=db_path)
    post = svc.get_post(post_id)
    if post is None:
        raise ValueError(f"Post {post_id} not found")
    if post.status != "approved":
        raise ValueError(
            f"Post {post_id} has status '{post.status}', expected 'approved'"
        )

    channel = get_channel(post.channel)
    if channel is None:
        svc.mark_failed(post_id, error=f"Unknown channel: {post.channel}")
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post_id,
            outcome="failed",
            error=f"Unknown channel: {post.channel}",
        )
        return False

    if channel.execution_method == "manual":
        svc.mark_failed(
            post_id,
            error=f"{channel.display_name} is manual-only. Copy the draft and post manually.",
        )
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post_id,
            outcome="failed",
            error="Manual channel — no auto-dispatch",
        )
        return False

    if channel.execution_method == "api":
        return _dispatch_api(svc, post, channel)

    if channel.execution_method == "browser":
        return _dispatch_browser(svc, post, channel)

    if channel.execution_method == "postiz":
        return _dispatch_postiz(svc, post, channel)

    svc.mark_failed(post_id, error=f"Unknown execution method: {channel.execution_method}")
    return False


def dispatch_due_posts(
    *,
    db_path: str | Path | None = None,
) -> dict:
    """Dispatch all approved posts whose scheduled_for has passed.

    Each post is individually gated through require_integration_action.
    Returns summary: {dispatched: int, failed: int, blocked: int, errors: list}.
    """
    svc = SocialPostService(db_path=db_path)
    due = svc.list_due()
    summary: dict = {"dispatched": 0, "failed": 0, "blocked": 0, "errors": []}

    for post in due:
        try:
            ok = dispatch_post(post.id, db_path=db_path)
            if ok:
                summary["dispatched"] += 1
            else:
                summary["failed"] += 1
        except IntegrationPolicyError as exc:
            summary["blocked"] += 1
            summary["errors"].append(f"Post {post.id}: blocked — {exc}")
        except ValueError as exc:
            summary["errors"].append(f"Post {post.id}: {exc}")
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"Post {post.id}: {exc}")

    return summary


def _dispatch_api(
    svc: SocialPostService,
    post: "SocialPost",  # noqa: F821
    channel: "SocialChannel",  # noqa: F821
) -> bool:
    # Map channel to integration action
    action_map = {
        "facebook": "post_facebook",
        "fb": "post_facebook",
        "instagram": "post_instagram",
        "ig": "post_instagram",
        "linkedin": "post_linkedin",
        "li": "post_linkedin",
        "x": "post_x",
        "twitter": "post_x",
        "reddit": "post_reddit",
    }
    action_name = action_map.get(post.channel.lower(), f"post_{post.channel.lower()}")

    try:
        # Gate check: require integration action before external post
        require_integration_action("social", action_name, surface="operator_confirmed", caller="dispatch_api")

        # Write pre-send gate record (audit trail of send attempt)
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post.id,
            outcome="pending",
            body_preview=post.body,
        )

        # Resolve the media asset to attach. A video draft (media_type=video)
        # hosts the rendered MP4 → IG Reel / FB video; else Instagram gets a
        # branded quote card. A hosting/render failure MUST fail the post.
        image_url = ""
        video_url = ""
        if post.media_type == "video" and post.media_path:
            try:
                from pathlib import Path as _Path

                from social.image_host import upload_public

                video_url = upload_public(_Path(post.media_path))
            except Exception as exc:
                error = f"Video host upload failed: {exc}"
                svc.mark_failed(post.id, error=error)
                append_social_audit_record(
                    channel=post.channel, action="post", post_id=post.id,
                    outcome="failed", error=error,
                )
                return False
        elif post.channel.lower() in ("instagram", "ig"):
            try:
                from social.image_host import upload_public

                if post.media_type == "image" and post.media_path:
                    from pathlib import Path as _Path

                    image_url = upload_public(_Path(post.media_path))
                else:
                    from social.quote_card import render_quote_card

                    card_path = render_quote_card(post.body, title=post.title)
                    image_url = upload_public(card_path)
            except Exception as exc:
                error = f"Quote-card generation/upload failed: {exc}"
                svc.mark_failed(post.id, error=error)
                append_social_audit_record(
                    channel=post.channel,
                    action="post",
                    post_id=post.id,
                    outcome="failed",
                    error=error,
                )
                return False

        # Now attempt the external post
        from integrations.social_media import post_to_platform

        result = post_to_platform(
            post.channel, post.body, image_url=image_url, video_url=video_url
        )
        if result.success:
            svc.mark_posted(post.id, post_url=result.post_url)
            # Update audit record with success
            append_social_audit_record(
                channel=post.channel,
                action="post",
                post_id=post.id,
                outcome="success",
                body_preview=post.body,
                post_url=result.post_url,
            )
            return True
        else:
            svc.mark_failed(post.id, error=result.message)
            # Update audit record with failure
            append_social_audit_record(
                channel=post.channel,
                action="post",
                post_id=post.id,
                outcome="failed",
                error=result.message,
            )
            return False
    except IntegrationPolicyError as exc:
        svc.mark_failed(post.id, error=str(exc))
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post.id,
            outcome="blocked",
            error=f"Policy gate: {exc}",
        )
        raise
    except Exception as exc:
        svc.mark_failed(post.id, error=str(exc))
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post.id,
            outcome="failed",
            error=str(exc),
        )
        return False


def _dispatch_postiz(
    svc: SocialPostService,
    post: "SocialPost",  # noqa: F821
    channel: "SocialChannel",  # noqa: F821
) -> bool:
    """Publish through the Postiz lane (optimistic accept + reconcile).

    Same contract as _dispatch_api: gate FIRST, pre-send pending audit,
    mark_posted/mark_failed + audit, blocked re-raise. Acceptance means
    ENQUEUED — social/postiz_reconcile.py closes the loop on the next
    cadence tick (Postiz has no webhooks).
    """
    action_map = {
        "facebook": "post_facebook",
        "fb": "post_facebook",
        "instagram": "post_instagram",
        "ig": "post_instagram",
        "linkedin": "post_linkedin",
        "li": "post_linkedin",
        "x": "post_x",
        "twitter": "post_x",
        "reddit": "post_reddit",
    }
    action_name = action_map.get(post.channel.lower(), f"post_{post.channel.lower()}")

    try:
        # Gate check: require integration action before external post
        require_integration_action("social", action_name, surface="operator_confirmed", caller="dispatch_postiz")

        # Write pre-send gate record (audit trail of send attempt)
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post.id,
            outcome="pending",
            body_preview=post.body,
        )

        from social.postiz_payload import (
            VIDEO_REQUIRED_PLATFORMS,
            build_platform_settings,
            resolve_platform_type,
        )

        if not channel.postiz_integration_id:
            error = (
                f"No postiz_integration_id configured for '{post.channel}' — "
                "bind it in social/channels.yaml (ids come from the instance's "
                "channel list; see the dashboard Social tab)."
            )
            svc.mark_failed(post.id, error=error)
            append_social_audit_record(
                channel=post.channel,
                action="post",
                post_id=post.id,
                outcome="failed",
                error=error,
            )
            return False

        platform_type = resolve_platform_type(channel)
        # Video-required platforms (youtube) are fine WHEN a video is attached;
        # only refuse when the draft has no video to satisfy them.
        if platform_type in VIDEO_REQUIRED_PLATFORMS and not (
            post.media_type == "video" and post.media_path
        ):
            error = (
                f"'{platform_type}' requires a video attachment, but the draft "
                "has no rendered video (media_type=video / media_path)."
            )
            svc.mark_failed(post.id, error=error)
            append_social_audit_record(
                channel=post.channel,
                action="post",
                post_id=post.id,
                outcome="failed",
                error=error,
            )
            return False

        from integrations import postiz_api

        # Attach media: a rendered video (Reels/Shorts) uploads straight into
        # Postiz media storage; else Instagram gets a branded quote card.
        media: list[dict] = []
        if post.media_type == "video" and post.media_path:
            try:
                media = [postiz_api.upload_file(str(post.media_path))]
            except Exception as exc:
                error = f"Video upload to Postiz failed: {exc}"
                svc.mark_failed(post.id, error=error)
                append_social_audit_record(
                    channel=post.channel, action="post", post_id=post.id,
                    outcome="failed", error=error,
                )
                return False
        elif platform_type in ("instagram", "instagram-standalone"):
            try:
                if post.media_type == "image" and post.media_path:
                    media = [postiz_api.upload_file(str(post.media_path))]
                else:
                    from social.quote_card import render_quote_card

                    card_path = render_quote_card(post.body, title=post.title)
                    media = [postiz_api.upload_file(str(card_path))]
            except Exception as exc:
                error = f"Quote-card generation/upload failed: {exc}"
                svc.mark_failed(post.id, error=error)
                append_social_audit_record(
                    channel=post.channel,
                    action="post",
                    post_id=post.id,
                    outcome="failed",
                    error=error,
                )
                return False

        settings = build_platform_settings(platform_type, post, channel)
        postiz_post_id = postiz_api.create_post(
            integration_id=channel.postiz_integration_id,
            content=post.body,
            settings=settings,
            media=media,
        )

        # Optimistic accept: enqueued, not yet platform-confirmed. post_url
        # stays empty until the reconcile pass fills releaseURL.
        svc.mark_posted(
            post.id, external_ref=f"postiz:{postiz_post_id}"
        )
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post.id,
            outcome="success",
            body_preview=post.body,
        )
        return True
    except IntegrationPolicyError as exc:
        svc.mark_failed(post.id, error=str(exc))
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post.id,
            outcome="blocked",
            error=f"Policy gate: {exc}",
        )
        raise
    except Exception as exc:
        svc.mark_failed(post.id, error=str(exc))
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post.id,
            outcome="failed",
            error=str(exc),
        )
        return False


def _dispatch_browser(
    svc: SocialPostService,
    post: "SocialPost",  # noqa: F821
    channel: "SocialChannel",  # noqa: F821
) -> bool:
    # Map channel to integration action
    action_map = {
        "linkedin": "post_linkedin",
        "li": "post_linkedin",
        "reddit": "post_reddit",
    }
    action_name = action_map.get(post.channel.lower(), f"post_{post.channel.lower()}")

    try:
        # Gate check: require integration action before external post
        require_integration_action("social", action_name, surface="operator_confirmed", caller="dispatch_browser")

        # Write pre-send gate record (audit trail of send attempt)
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post.id,
            outcome="pending",
            body_preview=post.body,
        )

        # Now attempt the external post via browser
        from orchestration.browser_executor import BrowserExecutor
        from orchestration.models import SocialWriteTask, Subtask

        task = SocialWriteTask(
            workflow_id=channel.browser_workflow_id or f"{post.channel}.post.create",
            target_url="",
            payload_text=post.body,
            action="post",
        )

        subtask = Subtask(
            id=post.id,
            convoy_id=0,
            title=post.title,
            metadata=__import__("json").dumps({
                "workflow_id": task.workflow_id,
                "target_url": task.target_url,
                "payload_text": task.payload_text,
                "action": task.action,
            }),
        )

        # The driver lives in the chat slice. Import it as a package when
        # .claude/ is on sys.path; otherwise fall back to the flat-slice
        # convention (chat/ on sys.path) used everywhere else (#53).
        try:
            from chat.social_write_driver import make_social_write_driver  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            # Only fall back for the EXPECTED package-resolution miss (the chat
            # slice not importable as `chat.*`). A real broken dependency inside
            # social_write_driver (e.g. browser_control) must surface, not be
            # silently retried and masked behind a confusing second error.
            if exc.name not in ("chat", "chat.social_write_driver"):
                raise
            import sys

            chat_dir = Path(__file__).resolve().parents[2] / "chat"
            if str(chat_dir) not in sys.path:
                sys.path.insert(0, str(chat_dir))
            from social_write_driver import make_social_write_driver  # type: ignore[no-redef]

        driver = make_social_write_driver()
        executor = BrowserExecutor(driver)
        receipt = executor.dispatch(subtask)

        if receipt.status == "completed":
            post_url = ""
            if receipt.metadata:
                post_url = receipt.metadata.get("post_url", "")
            svc.mark_posted(post.id, post_url=post_url)
            append_social_audit_record(
                channel=post.channel,
                action="post",
                post_id=post.id,
                outcome="success",
                body_preview=post.body,
                post_url=post_url,
            )
            return True
        else:
            svc.mark_failed(post.id, error=receipt.error or "Browser executor failed")
            append_social_audit_record(
                channel=post.channel,
                action="post",
                post_id=post.id,
                outcome="failed",
                error=receipt.error or "Browser executor failed",
            )
            return False
    except IntegrationPolicyError as exc:
        svc.mark_failed(post.id, error=str(exc))
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post.id,
            outcome="blocked",
            error=f"Policy gate: {exc}",
        )
        raise
    except Exception as exc:
        svc.mark_failed(post.id, error=str(exc))
        append_social_audit_record(
            channel=post.channel,
            action="post",
            post_id=post.id,
            outcome="failed",
            error=str(exc),
        )
        return False
