"""Postiz HTTP client — sync wrapper over a self-hosted Postiz Public API v1.

Postiz (https://postiz.com) is an AGPL-3.0 multi-platform social publisher.
LICENSE BOUNDARY: The Homie talks to an UNMODIFIED self-hosted instance over
HTTP only. No Postiz source code is ported, read, or embedded — payload
shapes come from the published API docs / OpenAPI spec. Copyleft does not
propagate across this network API boundary.

Mirrors the shape of ``cabinet_api.py`` (call-time env resolution,
``friendly_message`` error hierarchy, injectable client) but SYNC — both
consumers are sync (``social/post_executor.py`` dispatch and the
threadpooled ``dashboard_api.py`` handlers).

Auth gotcha: Postiz expects the RAW API key in the ``Authorization`` header
— NO ``Bearer`` prefix.

Anti-pattern compliance:

* Rule 1: settings resolve at call time via ``config.get_postiz_settings()``
  — no default-bound env values, no import-time reads.
* Rule 2: no module-level cached httpx client. Helpers create a fresh client
  when the caller passes ``None`` and close it, OR use the caller-provided
  client (lifecycle owned by caller; tests inject ``httpx.MockTransport``).
* Connect URLs from :func:`get_connect_url` are SENSITIVE and expire — never
  log them, never write them to audit rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

DEFAULT_CONNECT_TIMEOUT_S = 3.0

__all__ = [
    "PostizAPIError",
    "PostizNotConfigured",
    "PostizUnreachable",
    "PostizAuthFailure",
    "PostizRateLimited",
    "PostizBadRequest",
    "PostizStatus",
    "PostizIntegration",
    "get_status",
    "list_integrations",
    "get_connect_url",
    "upload_file",
    "create_post",
    "list_posts",
    "delete_post",
]


# ---------------------------------------------------------------------------
# Friendly error classes
# ---------------------------------------------------------------------------


class PostizAPIError(Exception):
    """Base class for all postiz_api errors.

    Carries ``friendly_message`` so operator surfaces can show it directly
    instead of leaking a stack trace.
    """

    friendly_message: str = "Postiz API error."


class PostizNotConfigured(PostizAPIError):
    """POSTIZ_API_URL / POSTIZ_API_KEY missing — the transport is off."""

    friendly_message = (
        "Postiz is not configured. Set POSTIZ_API_URL and POSTIZ_API_KEY "
        "in .claude/scripts/.env to enable the Postiz publishing lane."
    )


class PostizUnreachable(PostizAPIError):
    """``httpx.ConnectError`` — the Postiz backend is not answering."""

    friendly_message = (
        "Postiz instance is unreachable. Check that the Postiz stack is "
        "running (its backend can futex-hang on cold boot — restart the "
        "postiz container after its Postgres/Temporal deps are healthy)."
    )


class PostizAuthFailure(PostizAPIError):
    """HTTP 401 — API key missing or wrong.

    Postiz wants the RAW key in ``Authorization`` (no ``Bearer`` prefix); a
    prefixed key also lands here.
    """

    friendly_message = (
        "Postiz rejected the API key — check POSTIZ_API_KEY in .env "
        "(sent raw, no 'Bearer' prefix)."
    )


class PostizRateLimited(PostizAPIError):
    """HTTP 429 — Public API budget exhausted (~30-90 req/hr self-host)."""

    friendly_message = (
        "Postiz API rate limit hit — wait for the hourly window to reset "
        "(tunable via the instance's API_LIMIT env)."
    )


class PostizBadRequest(PostizAPIError):
    """HTTP 4xx — malformed payload or unknown resource."""

    friendly_message = "Postiz rejected the request — see the error detail."


# ---------------------------------------------------------------------------
# Return shapes
# ---------------------------------------------------------------------------


@dataclass
class PostizStatus:
    configured: bool = False
    reachable: bool = False
    auth_ok: bool = False
    integrations_count: int = 0
    error: str = ""


@dataclass
class PostizIntegration:
    id: str = ""
    name: str = ""
    identifier: str = ""
    picture: str = ""
    disabled: bool = False
    profile: str = ""


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------


def _public_base(api_url: str) -> str:
    """Normalize the backend origin into the Public API v1 base."""
    return api_url.rstrip("/") + "/public/v1"


def _check_status(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise PostizAuthFailure(f"HTTP 401: {response.text[:200]}")
    if response.status_code == 429:
        raise PostizRateLimited(f"HTTP 429: {response.text[:200]}")
    if 400 <= response.status_code < 500:
        raise PostizBadRequest(
            f"HTTP {response.status_code}: {response.text[:200]}"
        )
    if response.status_code >= 500:
        raise PostizAPIError(
            f"HTTP {response.status_code}: {response.text[:200]}"
        )


def _request(
    method: str,
    path: str,
    *,
    client: httpx.Client | None = None,
    json_body: Any | None = None,
    params: dict[str, Any] | None = None,
    files: Any | None = None,
) -> httpx.Response:
    """Issue one authed request against the Public API.

    Raises PostizNotConfigured / PostizUnreachable / PostizAuthFailure /
    PostizRateLimited / PostizBadRequest / PostizAPIError.
    """
    import config

    settings = config.get_postiz_settings()
    if not settings.configured:
        raise PostizNotConfigured()

    url = _public_base(settings.api_url) + path
    headers = {"Authorization": settings.api_key}  # raw key, no Bearer
    timeout = httpx.Timeout(
        settings.timeout_s, connect=DEFAULT_CONNECT_TIMEOUT_S
    )

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=timeout)
    try:
        try:
            response = client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                params=params,
                files=files,
            )
        except httpx.ConnectError as exc:
            raise PostizUnreachable(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise PostizAPIError(str(exc)) from exc
        _check_status(response)
        return response
    finally:
        if owns_client:
            client.close()


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def get_status(*, client: httpx.Client | None = None) -> PostizStatus:
    """Probe the instance. Never raises; never touches the network when
    unconfigured. 401 semantics: backend up, key wrong (auth_ok=False)."""
    import config

    settings = config.get_postiz_settings()
    if not settings.configured:
        return PostizStatus(configured=False)

    try:
        integrations = list_integrations(client=client)
    except PostizUnreachable as exc:
        return PostizStatus(configured=True, reachable=False, error=str(exc))
    except PostizAuthFailure as exc:
        return PostizStatus(
            configured=True, reachable=True, auth_ok=False, error=str(exc)
        )
    except PostizAPIError as exc:
        return PostizStatus(
            configured=True, reachable=True, auth_ok=False, error=str(exc)
        )
    return PostizStatus(
        configured=True,
        reachable=True,
        auth_ok=True,
        integrations_count=len(integrations),
    )


def list_integrations(
    *, client: httpx.Client | None = None
) -> list[PostizIntegration]:
    """GET /integrations — the connected channels."""
    response = _request("GET", "/integrations", client=client)
    items = response.json() or []
    result: list[PostizIntegration] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        result.append(
            PostizIntegration(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                identifier=str(item.get("identifier", "")),
                picture=str(item.get("picture", "") or ""),
                disabled=bool(item.get("disabled", False)),
                profile=str(item.get("profile", "") or ""),
            )
        )
    return result


def get_connect_url(
    provider: str, *, client: httpx.Client | None = None
) -> str:
    """GET /social/{provider} — a fresh OAuth connect URL.

    The URL is SENSITIVE and expires: return it to the caller only. Never
    log it, never audit it, never persist it.
    """
    response = _request("GET", f"/social/{provider}", client=client)
    body = response.json() or {}
    url = str(body.get("url", ""))
    if not url:
        raise PostizBadRequest(f"No connect URL returned for '{provider}'")
    return url


def upload_file(
    file_path: str, *, client: httpx.Client | None = None
) -> dict[str, str]:
    """POST /upload — multipart-import local media, return {id, path}.

    (Older docs describe /uploads/upload-from-url; the live API serves
    /upload with a multipart ``file`` field — verified against a running
    instance, 2026-07-06.)
    """
    from pathlib import Path as _Path

    p = _Path(file_path)
    _mime = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".mp4": "video/mp4", ".mov": "video/quicktime",
    }.get(p.suffix.lower(), "image/png")
    with open(p, "rb") as handle:
        response = _request(
            "POST",
            "/upload",
            client=client,
            files={"file": (p.name, handle, _mime)},
        )
    body = response.json() or {}
    media = {"id": str(body.get("id", "")), "path": str(body.get("path", ""))}
    if not media["id"] and not media["path"]:
        raise PostizBadRequest("Upload returned no media id/path")
    return media


def create_post(
    *,
    integration_id: str,
    content: str,
    settings: dict[str, Any],
    media: list[dict[str, str]] | None = None,
    post_type: str = "now",
    scheduled_at: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """POST /posts — create/schedule one post on one integration.

    ``settings`` must carry the platform ``__type`` (see
    ``social/postiz_payload.py``). Returns the Postiz post id. Acceptance
    means ENQUEUED — actual platform publish is async (no webhooks);
    reconcile via :func:`list_posts`.
    """
    date = scheduled_at or datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    payload: dict[str, Any] = {
        "type": post_type,
        "date": date,
        "shortLink": False,
        "tags": [],
        "posts": [
            {
                "integration": {"id": integration_id},
                "value": [
                    {
                        "content": content,
                        "image": list(media or []),
                    }
                ],
                "settings": settings,
            }
        ],
    }
    response = _request("POST", "/posts", client=client, json_body=payload)
    body = response.json()
    if isinstance(body, list) and body and isinstance(body[0], dict):
        post_id = str(body[0].get("postId", ""))
        if post_id:
            return post_id
    raise PostizBadRequest(f"Unexpected create-post response: {str(body)[:200]}")


def list_posts(
    start_iso: str,
    end_iso: str,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """GET /posts — posts in [start, end]. Each item carries ``id``,
    ``state`` (QUEUE|PUBLISHED|ERROR|DRAFT) and ``releaseURL``."""
    response = _request(
        "GET",
        "/posts",
        client=client,
        params={"startDate": start_iso, "endDate": end_iso},
    )
    body = response.json() or {}
    posts = body.get("posts", []) if isinstance(body, dict) else []
    return [p for p in posts if isinstance(p, dict)]


def delete_post(post_id: str, *, client: httpx.Client | None = None) -> None:
    """DELETE /posts/{id} — remove a post (canary cleanup)."""
    _request("DELETE", f"/posts/{post_id}", client=client)
