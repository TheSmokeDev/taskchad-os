"""Channel-uniform file-send marker pattern.

VERBATIM PORT of ClaudeClaw `src/bot.ts:288-317` extractFileMarkers(text). Agent
emits a literal text marker like `[SEND_FILE:/abs/path|caption]`,
`[SEND_PHOTO|https://example.com/x.png]`, or bare `SEND_FILE:/tmp/a.pdf`; the
adapter parses the markers and dispatches as media via the channel-specific
file-send API. Same agent reply works on Telegram, Discord, Slack, WhatsApp,
Web/Relay, and CLI.

Two-pattern shape (port from bot.ts:296-299):
  1. Bracketed canonical: `[SEND_(FILE|PHOTO)[:|]<path>(|<caption>)?]`
  2. Bare/URL form:       `(?:^|\\s)SEND_(FILE|PHOTO)[:|]<https?:|/<path>>(|<caption>)?`

Tolerant variants exist because malformed agent replies should still render
media instead of leaking the raw command into chat. (R1 B3: prior single-pattern
regex dropped pipe separators, captions, bare markers, and URL paths.)

Type mapping matches upstream bot.ts:303-307: SEND_FILE -> 'document',
SEND_PHOTO -> 'photo'. (R1 M5: kind values are 'document'/'photo' so adapters
can map directly to Telegram sendDocument/sendPhoto without reinventing.)

Path-traversal defense (R3 NM2 / NB5): 5-step validation:
  1. Reject any `..` path-traversal segment
  2. Allow http(s):// URLs (no filesystem access)
  3. Reject POSIX absolute filesystem paths (`/etc/passwd`)
  4. Reject Windows absolute paths (`C:/Windows/...`)
  5. Allow relative paths (`./report.pdf`, `report.pdf`, `./images/x.png`)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SendMarker:
    """Parsed media-send marker.

    kind: 'document' or 'photo' (matches upstream bot.ts:303-307 type mapping).
    path: absolute path, relative path, or http(s) URL.
    caption: optional caption text (None if not provided).
    """

    kind: str  # 'document' | 'photo'
    path: str
    caption: str | None = None


# Port bot.ts:296-299 verbatim — both patterns. Regex captures (kind, path, optional caption).
# Pattern 1: canonical bracketed form: [SEND_FILE:path], [SEND_PHOTO|path], [...|caption]
_PATTERN_BRACKETED: re.Pattern[str] = re.compile(
    r"\[SEND_(FILE|PHOTO)[:|]\s*([^\]|]+?)(?:\s*\|\s*([^\]]*))?\]",
    re.IGNORECASE,
)

# Pattern 2: bare/URL form: SEND_FILE:/abs/path, SEND_PHOTO|https://example.com/x.png
_PATTERN_BARE: re.Pattern[str] = re.compile(
    r"(?:^|\s)SEND_(FILE|PHOTO)\s*[:|]\s*((?:https?://|/)[^\s|\]]+)(?:\s*\|\s*([^\n]+))?",
    re.IGNORECASE,
)

_PATTERNS: tuple[re.Pattern[str], ...] = (_PATTERN_BRACKETED, _PATTERN_BARE)


def _is_allowed_absolute(path: str) -> bool:
    """Allow only paths under workspace dirs — reject /etc/passwd etc.

    Default policy: reject all absolute filesystem paths (only relative paths
    and http(s) URLs allowed). Defense-in-depth aligned with Phase 7a sanitizer.
    """
    return False


def parse_send_markers(text: str) -> list[SendMarker]:
    """Return list of SendMarker structures found in text.

    Port bot.ts:288-317 extractFileMarkers verbatim. Returns parsed markers in
    document/photo order, preserving caption text from either `:` or `|`
    separator forms. Path-traversal defense applied per-marker (defense-in-depth
    aligned with Phase 7a sanitizer).

    R3 NM2 / NB5 fix: 5-step path validation:
      1. Reject `..` path-traversal segment
      2. http(s):// URLs always allowed
      3. POSIX absolute paths (`/etc/passwd`) — rejected
      4. Windows absolute paths (`C:/...`, `C:\\...`) — rejected
      5. Relative paths — allowed
    """
    out: list[SendMarker] = []
    for pattern in _PATTERNS:
        for m in pattern.finditer(text):
            raw_kind = m.group(1).upper()
            raw_path = m.group(2).strip()
            raw_caption = m.group(3)
            caption = raw_caption.strip() if raw_caption else None

            # Step 1: Reject any `..` path-traversal segment
            if ".." in raw_path:
                continue
            # Step 2: http(s):// URLs are always allowed (no filesystem access)
            if raw_path.startswith(("http://", "https://")):
                pass  # allowed
            # Step 3: POSIX absolute filesystem paths (/etc/passwd, etc.) — reject
            elif raw_path.startswith("/"):
                if not _is_allowed_absolute(raw_path):
                    continue
            # Step 4: Windows absolute paths (C:/Windows/..., C:\\Windows\\...) — reject
            elif len(raw_path) >= 3 and raw_path[1:3] in (":/", ":\\"):
                if not _is_allowed_absolute(raw_path):
                    continue
            # Step 5: Relative paths — allowed (./report.pdf, report.pdf, ./images/x.png)

            kind = "photo" if raw_kind == "PHOTO" else "document"
            out.append(SendMarker(kind=kind, path=raw_path, caption=caption))
    return out


def strip_send_markers(text: str) -> str:
    """Return text with all markers removed (port bot.ts:301-314).

    Mirrors upstream `cleaned.replace(pattern, ...)` then collapse-blank-lines
    behavior. Returns trimmed text suitable for the text-channel send.

    NOTE: this strips ALL marker matches — including ones that would be
    rejected by parse_send_markers' path-traversal defense — so the rejected
    raw command does NOT leak into chat as visible text.
    """
    cleaned = text
    for pattern in _PATTERNS:
        cleaned = pattern.sub("", cleaned)
    # Collapse extra blank lines left by stripped markers (port bot.ts:314).
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


__all__ = [
    "SendMarker",
    "parse_send_markers",
    "strip_send_markers",
]
