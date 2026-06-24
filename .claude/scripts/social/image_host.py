"""Public image hosting via Supabase Storage.

Uploads a PNG to a public Supabase Storage bucket and returns a public URL
that Meta's Graph API can fetch when publishing an Instagram feed post.

Env (resolved at call time, Rule 1):
- NEXT_PUBLIC_SUPABASE_URL    — project base URL
- SUPABASE_SERVICE_ROLE_KEY   — service-role JWT (server-side only)

Fail-open with clear exceptions; the service-role key is never echoed in
error messages.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

_DEFAULT_BUCKET = "social-cards"


class ImageHostError(RuntimeError):
    """Raised when hosting the image publicly fails."""


def _resolve_credentials() -> tuple[str, str]:
    """Read Supabase URL + service-role key at call time."""
    url = (os.getenv("NEXT_PUBLIC_SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url:
        raise ImageHostError(
            "NEXT_PUBLIC_SUPABASE_URL is not set — cannot host image publicly."
        )
    if not key:
        raise ImageHostError(
            "SUPABASE_SERVICE_ROLE_KEY is not set — cannot host image publicly."
        )
    return url, key


def _ensure_bucket(url: str, key: str, bucket: str) -> None:
    """Create the public bucket if missing. Idempotent — 'already exists' is ok."""
    try:
        resp = requests.post(
            f"{url}/storage/v1/bucket",
            json={"id": bucket, "name": bucket, "public": True},
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ImageHostError(f"Bucket create request failed: {exc}") from exc

    if resp.status_code in (200, 201):
        return
    # Supabase returns 409 (or 400 with a "already exists" body) when the
    # bucket is already present — both are success for our purposes.
    body = (resp.text or "").lower()
    if resp.status_code == 409 or "already exists" in body or "duplicate" in body:
        return
    raise ImageHostError(
        f"Bucket create failed (HTTP {resp.status_code}): {_safe_detail(resp)}"
    )


def _safe_detail(resp: requests.Response) -> str:
    """Short, key-free description of a failed response for error messages."""
    text = (resp.text or "").strip()
    return text[:200] if text else f"<empty body, HTTP {resp.status_code}>"


def upload_public(path: Path, *, bucket: str = _DEFAULT_BUCKET) -> str:
    """Upload ``path`` (a PNG) to a public Supabase bucket; return its public URL.

    The object name is derived from the filename so re-uploading the same card
    is idempotent (``x-upsert: true``).
    """
    path = Path(path)
    if not path.is_file():
        raise ImageHostError(f"Image not found: {path}")

    url, key = _resolve_credentials()
    _ensure_bucket(url, key, bucket)

    object_name = path.name
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImageHostError(f"Could not read image bytes: {exc}") from exc

    try:
        resp = requests.post(
            f"{url}/storage/v1/object/{bucket}/{object_name}",
            data=data,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "image/png",
                "x-upsert": "true",
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ImageHostError(f"Upload request failed: {exc}") from exc

    if resp.status_code not in (200, 201):
        raise ImageHostError(
            f"Upload failed (HTTP {resp.status_code}): {_safe_detail(resp)}"
        )

    return f"{url}/storage/v1/object/public/{bucket}/{object_name}"
