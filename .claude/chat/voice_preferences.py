"""Persistent cross-adapter voice reply preference.

The Homie is an operator-owned runtime, so the preference is intentionally
global: changing it from Telegram changes Discord too (and vice versa).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from config import STATE_DIR
from shared import file_lock, load_state, save_state

VoiceReplyMode = Literal["always", "auto", "off"]

VOICE_REPLY_MODES: tuple[VoiceReplyMode, ...] = ("always", "auto", "off")
VOICE_REPLY_STATE_PATH = Path(STATE_DIR) / "voice-reply-preference.json"
_VOICE_REPLY_LOCK_PATH = VOICE_REPLY_STATE_PATH.with_suffix(".lock")


def prepare_voice_reply_text(text: str) -> str:
    """Turn transcript copy into natural speech without changing the transcript.

    Chat replies intentionally keep exact commands and identifiers in Discord or
    Telegram.  The audio copy should explain them, not pronounce their punctuation.
    This is deterministic so voice delivery never needs a second model call.
    """

    spoken = str(text or "")
    spoken = re.sub(
        r"```[\s\S]*?```",
        " The exact code is in the text. ",
        spoken,
    )
    spoken = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", spoken)
    spoken = re.sub(
        r"https?://\S+",
        "the link in the text",
        spoken,
        flags=re.IGNORECASE,
    )

    def _inline_code(match: re.Match[str]) -> str:
        value = match.group(1).strip()
        if not value:
            return ""
        if re.search(r"[\\/:]", value) or value.startswith(("$", "-")):
            return "the exact syntax in the text"
        return re.sub(r"[_-]+", " ", value)

    spoken = re.sub(r"`([^`]+)`", _inline_code, spoken)
    spoken = re.sub(
        r"\b([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b",
        lambda m: m.group(1).replace("_", " "),
        spoken,
    )
    spoken = re.sub(r"(?<=\w)/(?=\w)", " or ", spoken)
    spoken = re.sub(r"(?<=\w)-(?=\w)", " ", spoken)
    spoken = re.sub(r"^\s{0,3}#{1,6}\s+", "", spoken, flags=re.MULTILINE)
    spoken = re.sub(r"^\s*[-*+]\s+", "", spoken, flags=re.MULTILINE)
    spoken = re.sub(r"\*{1,2}(.+?)\*{1,2}", r"\1", spoken)
    spoken = re.sub(r"_{1,2}(.+?)_{1,2}", r"\1", spoken)
    spoken = re.sub(r"[ \t]+", " ", spoken)
    spoken = re.sub(r"\s*\n\s*", " ", spoken)
    return spoken.strip()


def normalize_voice_reply_mode(value: object) -> VoiceReplyMode:
    """Return a supported mode, failing open to the legacy ``auto`` mode."""

    normalized = str(value or "").strip().lower()
    aliases = {"on": "always", "yes": "always", "true": "always"}
    normalized = aliases.get(normalized, normalized)
    if normalized in VOICE_REPLY_MODES:
        return normalized  # type: ignore[return-value]
    return "auto"


def get_voice_reply_mode() -> VoiceReplyMode:
    """Read the current mode. Missing/corrupt state preserves legacy behavior."""

    state = load_state(VOICE_REPLY_STATE_PATH)
    return normalize_voice_reply_mode(state.get("mode"))


def set_voice_reply_mode(mode: str) -> VoiceReplyMode:
    """Persist one mode atomically for every voice-capable chat adapter."""

    normalized = normalize_voice_reply_mode(mode)
    if mode.strip().lower() not in {*VOICE_REPLY_MODES, "on", "yes", "true"}:
        raise ValueError(f"unsupported voice reply mode: {mode}")
    with file_lock(_VOICE_REPLY_LOCK_PATH):
        save_state({"mode": normalized}, VOICE_REPLY_STATE_PATH)
    return normalized
