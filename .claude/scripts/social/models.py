"""Social post automation data models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SocialPostStatus = Literal["draft", "approved", "posted", "failed", "rejected"]

VALID_STATUSES: frozenset[str] = frozenset(
    ["draft", "approved", "posted", "failed", "rejected"]
)

SOCIAL_POST_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset(["approved", "rejected"]),
    "approved": frozenset(["posted", "failed"]),
    "posted": frozenset(),
    "failed": frozenset(),
    "rejected": frozenset(),
}


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
    audit_id: str | None = None
