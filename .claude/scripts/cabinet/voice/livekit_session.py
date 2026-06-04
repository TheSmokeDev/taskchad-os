"""LiveKit session/token helpers for the Cabinet voice transport spike.

The Cabinet orchestrator remains the source of truth. This module only mints
room-scoped LiveKit tokens and builds deterministic session metadata for the
browser and the Python LiveKit agent.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from datetime import timedelta


DEFAULT_LIVEKIT_URL = "ws://127.0.0.1:7880"
DEFAULT_AGENT_NAME = "cabinet-livekit-agent"
_ROOM_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")


class LiveKitSessionError(Exception):
    """Base LiveKit spike configuration error."""


class LiveKitDependencyMissing(LiveKitSessionError):
    """The optional LiveKit Python SDK is not installed."""


class LiveKitConfigError(LiveKitSessionError):
    """LiveKit API key/secret or URL is missing/invalid."""


@dataclass(frozen=True)
class LiveKitSessionDescriptor:
    """Browser-facing LiveKit session metadata."""

    meeting_id: int
    chat_id: str
    room_name: str
    server_url: str
    participant_identity: str
    participant_name: str
    participant_token: str
    agent_identity: str
    agent_name: str
    expires_in_s: int

    def to_wire(self) -> dict:
        return {
            "meetingId": self.meeting_id,
            "chatId": self.chat_id,
            "roomName": self.room_name,
            "serverUrl": self.server_url,
            "participantIdentity": self.participant_identity,
            "participantName": self.participant_name,
            "participantToken": self.participant_token,
            "agentIdentity": self.agent_identity,
            "agentName": self.agent_name,
            "expiresInS": self.expires_in_s,
        }


def livekit_server_url() -> str:
    """Resolve the LiveKit signal URL at call time."""

    return (
        os.environ.get("CABINET_LIVEKIT_URL")
        or os.environ.get("LIVEKIT_URL")
        or DEFAULT_LIVEKIT_URL
    ).strip()


def livekit_agent_name() -> str:
    """Resolve the LiveKit agent name used by explicit dispatch/manual runs."""

    return (os.environ.get("CABINET_LIVEKIT_AGENT_NAME") or DEFAULT_AGENT_NAME).strip()


def livekit_token_ttl_seconds() -> int:
    """Resolve browser participant token TTL at call time."""

    raw = os.environ.get("CABINET_LIVEKIT_TOKEN_TTL_S", "1800").strip()
    try:
        ttl = int(raw)
    except ValueError:
        ttl = 1800
    return max(60, min(ttl, 24 * 60 * 60))


def build_room_name(meeting_id: int) -> str:
    """Build a deterministic room name for a Cabinet meeting."""

    if meeting_id <= 0:
        raise LiveKitConfigError("meeting_id must be positive")
    prefix = (os.environ.get("CABINET_LIVEKIT_ROOM_PREFIX") or "cabinet").strip()
    prefix = _ROOM_SAFE_RE.sub("-", prefix).strip("-") or "cabinet"
    return f"{prefix}-{meeting_id}"


def build_participant_identity(meeting_id: int, chat_id: str) -> str:
    """Build a browser participant identity without leaking raw chat text."""

    scope = _ROOM_SAFE_RE.sub("-", (chat_id or "browser").strip()).strip("-")
    scope = scope[:48] or "browser"
    return f"cabinet-browser-{meeting_id}-{scope}"


def _livekit_api_credentials() -> tuple[str, str]:
    key = (os.environ.get("LIVEKIT_API_KEY") or "").strip()
    secret = (os.environ.get("LIVEKIT_API_SECRET") or "").strip()
    if not key or not secret:
        raise LiveKitConfigError(
            "LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required for LiveKit tokens"
        )
    return key, secret


def _mint_room_token(
    *,
    room_name: str,
    identity: str,
    name: str,
    ttl_seconds: int,
) -> str:
    """Mint a room-scoped LiveKit token via the optional official SDK."""

    try:
        from livekit import api  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - dependency optional in CI.
        raise LiveKitDependencyMissing(
            "Install the optional livekit extra to mint LiveKit tokens"
        ) from exc

    api_key, api_secret = _livekit_api_credentials()
    token = (
        api.AccessToken(api_key, api_secret)
        .with_ttl(timedelta(seconds=ttl_seconds))
        .with_identity(identity)
        .with_name(name)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
            )
        )
        .to_jwt()
    )
    if not isinstance(token, str) or not token:
        raise LiveKitConfigError("LiveKit token generation returned an empty token")
    return token


def create_browser_session(
    *,
    meeting_id: int,
    chat_id: str,
    token_factory=None,
) -> LiveKitSessionDescriptor:
    """Create browser session metadata for one Cabinet meeting."""

    server_url = livekit_server_url()
    if not server_url.startswith(("ws://", "wss://")):
        raise LiveKitConfigError("CABINET_LIVEKIT_URL must start with ws:// or wss://")

    ttl_seconds = livekit_token_ttl_seconds()
    room_name = build_room_name(meeting_id)
    identity = build_participant_identity(meeting_id, chat_id)
    participant_name = f"Cabinet Browser {meeting_id}"
    agent_name = livekit_agent_name()
    agent_identity = f"{agent_name}-{meeting_id}"
    factory = token_factory or _mint_room_token
    participant_token = factory(
        room_name=room_name,
        identity=identity,
        name=participant_name,
        ttl_seconds=ttl_seconds,
    )
    return LiveKitSessionDescriptor(
        meeting_id=meeting_id,
        chat_id=chat_id,
        room_name=room_name,
        server_url=server_url,
        participant_identity=identity,
        participant_name=participant_name,
        participant_token=participant_token,
        agent_identity=agent_identity,
        agent_name=agent_name,
        expires_in_s=ttl_seconds,
    )


def redact_for_log(descriptor: LiveKitSessionDescriptor) -> dict:
    """Return session metadata without the bearer token."""

    data = asdict(descriptor)
    data["participant_token"] = "<redacted>"
    return data


__all__ = [
    "DEFAULT_AGENT_NAME",
    "DEFAULT_LIVEKIT_URL",
    "LiveKitConfigError",
    "LiveKitDependencyMissing",
    "LiveKitSessionDescriptor",
    "LiveKitSessionError",
    "build_participant_identity",
    "build_room_name",
    "create_browser_session",
    "livekit_agent_name",
    "livekit_server_url",
    "livekit_token_ttl_seconds",
    "redact_for_log",
]
