"""Operator learn-drop URL policy for the curriculum engine.

An operator-dropped link is a PRE-ADMITTED single catalog item: the operator's
imperative replaces cognitive admission. It replaces nothing else. The dropped
video still rides the same transcript extraction, untrusted-evidence wrapping,
bounded deep study, and citation validation as any polled source.

Pure module — no runtime, model, ledger, or filesystem imports — so both chat
surfaces can refuse a bad link before any provider call or ledger write.
(`security.untrusted` is pure too — constants and string functions, no config,
no I/O — so importing it preserves that property.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import ParseResult, parse_qs, urlparse

from security.untrusted import neutralize_untrusted_metadata

# The synthetic bundle every operator drop lands in. It is a PHYSICAL ledger
# row only: it never appears in `curriculum.sources` config, so the scheduler
# never polls it and a drop never enters the curated diversity cap.
OPERATOR_DROP_SOURCE_ID = "operator-drops"
OPERATOR_DROP_SOURCE_KIND = "operator_drop"
OPERATOR_DROP_SOURCE_POLICY = "operator"
OPERATOR_DROP_SOURCE_URL = "https://www.youtube.com/"
OPERATOR_DROP_METHOD = "operator-drop"
OPERATOR_DROP_REASON = "pre-admitted by operator imperative (learn drop)"

INGEST_REFUSAL_HINT = (
    "A learn drop accepts one YouTube video link. For an article, a file, or "
    "pasted text use `thehomie persona ingest <persona> <file|text>` instead."
)

_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtube-nocookie.com",
    }
)
_SHORT_HOST = "youtu.be"
_PATH_ID_PREFIXES = frozenset({"shorts", "live", "embed", "v"})
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


class UnsupportedDropURLError(ValueError):
    """Raised when a dropped link is not a single YouTube video URL."""


@dataclass(frozen=True, slots=True)
class YouTubeDrop:
    video_id: str
    canonical_url: str


def parse_youtube_drop(raw_url: str) -> YouTubeDrop:
    """Resolve one dropped link to an exact video identity, or refuse honestly.

    The canonical URL is rebuilt from the resolved id so downstream evidence
    checks (`bundle._preflight_raw`) see a URL whose video identity matches the
    ledger row no matter which YouTube link shape the operator pasted.
    """
    value = (raw_url or "").strip()
    if not value:
        raise UnsupportedDropURLError(f"A YouTube video URL is required. {INGEST_REFUSAL_HINT}")
    parsed = urlparse(value)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise UnsupportedDropURLError(f"Only http(s) links are accepted. {INGEST_REFUSAL_HINT}")
    if parsed.username or parsed.password:
        raise UnsupportedDropURLError("URLs containing credentials are not accepted.")
    host = (parsed.hostname or "").casefold().rstrip(".").removeprefix("www.")
    if not host:
        raise UnsupportedDropURLError(f"That link has no hostname. {INGEST_REFUSAL_HINT}")
    if host == _SHORT_HOST:
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host in _YOUTUBE_HOSTS:
        video_id = _video_id_from_youtube_path(parsed)
    else:
        # R5 MAJOR: `host` is attacker-chosen text, and this refusal is returned
        # by both learn surfaces and PERSISTED as an assistant transcript row
        # that the next turn replays into the system region. Neutralized at the
        # raise site so both callers are covered by construction.
        raise UnsupportedDropURLError(
            f"`{neutralize_untrusted_metadata(host, limit=80)}` is not a YouTube "
            f"video host. {INGEST_REFUSAL_HINT}"
        )
    if not _VIDEO_ID_RE.match(video_id):
        raise UnsupportedDropURLError(
            "That link does not name a single YouTube video — playlists, "
            f"channels, and search pages are not learnable drops. {INGEST_REFUSAL_HINT}"
        )
    return YouTubeDrop(
        video_id=video_id,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _video_id_from_youtube_path(parsed: ParseResult) -> str:
    segments = [segment for segment in parsed.path.split("/") if segment]
    if segments and segments[0].casefold() == "watch":
        return str(parse_qs(parsed.query).get("v", [""])[0])
    if len(segments) >= 2 and segments[0].casefold() in _PATH_ID_PREFIXES:
        return segments[1]
    return ""


__all__ = [
    "INGEST_REFUSAL_HINT",
    "OPERATOR_DROP_METHOD",
    "OPERATOR_DROP_REASON",
    "OPERATOR_DROP_SOURCE_ID",
    "OPERATOR_DROP_SOURCE_KIND",
    "OPERATOR_DROP_SOURCE_POLICY",
    "OPERATOR_DROP_SOURCE_URL",
    "UnsupportedDropURLError",
    "YouTubeDrop",
    "parse_youtube_drop",
]
