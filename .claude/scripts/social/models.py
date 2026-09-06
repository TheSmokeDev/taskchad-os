"""Social post automation data models."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SocialPostStatus = Literal[
    "draft",
    "approved",
    "posted",
    "failed",
    "rejected",
    "superseded",
    "verification_required",
]

VALID_STATUSES: frozenset[str] = frozenset(
    [
        "draft",
        "approved",
        "posted",
        "failed",
        "rejected",
        "superseded",
        "verification_required",
    ]
)

SOCIAL_POST_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset(["approved", "rejected", "superseded"]),
    "approved": frozenset(["posted", "failed", "verification_required"]),
    # posted → failed exists for async transports (Postiz): acceptance is
    # optimistic mark_posted; the reconcile pass demotes on platform error.
    "posted": frozenset(["failed"]),
    "failed": frozenset(),
    "rejected": frozenset(),
    "superseded": frozenset(),
    # A human can reconcile an ambiguous submission after checking LinkedIn,
    # but the dispatcher only accepts `approved`, so this state is never
    # automatically retried.
    "verification_required": frozenset(["posted", "failed"]),
}


def compute_content_digest(title: str, body: str) -> str:
    """Return the canonical SHA-256 for the exact copy being reviewed."""

    payload = f"title\0{title or ''}\0body\0{body or ''}".encode()
    return hashlib.sha256(payload).hexdigest()


def compute_media_digest(media_path: str | None) -> str:
    """Hash approved media bytes without leaking the local path.

    Missing media still gets a deterministic digest, so attaching/replacing a
    file invalidates every earlier approval callback.
    """

    digest = hashlib.sha256()
    if not media_path:
        digest.update(b"none")
        return digest.hexdigest()
    path = Path(media_path).expanduser()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        digest.update(b"missing\0")
        digest.update(str(path).encode("utf-8", errors="replace"))
    return digest.hexdigest()


def approval_binding_digest(post: SocialPost, *, length: int = 12) -> str:
    """Short digest binding a button to revision + exact copy + exact media."""

    content = post.content_digest or compute_content_digest(post.title, post.body)
    media = post.media_digest or compute_media_digest(post.media_path)
    payload = f"{post.revision}:{content}:{media}".encode("ascii")
    return hashlib.sha256(payload).hexdigest()[:length]


@dataclass
class SocialPost:
    id: int = 0
    channel: str = ""
    status: SocialPostStatus = "draft"
    title: str = ""
    body: str = ""
    voice_profile: str = ""
    topic_source: str = ""
    created_at: str = ""
    scheduled_for: str | None = None
    approved_at: str | None = None
    posted_at: str | None = None
    post_url: str | None = None
    rejection_reason: str | None = None
    error: str | None = None
    claimed_at: str | None = None
    audit_id: str | None = None
    # Async-transport reference, e.g. "postiz:<postId>" — set on optimistic
    # accept so the reconcile pass can match platform outcomes back to rows.
    external_ref: str | None = None
    # Rendered/generated media for the post. media_type ∈ {none,image,video}.
    # Lives here (not in the body) so a local path never leaks into a caption.
    media_path: str | None = None
    media_type: str | None = None
    # Exact-review provenance.  Revision changes whenever copy or media changes;
    # callback buttons bind to revision + the two full digests above.
    source_packet_id: str | None = None
    revision: int = 1
    content_digest: str = ""
    media_digest: str = ""
    verification_state: str = "pending"
    receipt_json: str | None = None
    supersede_reason: str | None = None
