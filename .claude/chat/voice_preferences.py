"""Persistent cross-adapter voice reply preference.

The Homie is an operator-owned runtime, so the preference is intentionally
global: changing it from Telegram changes Discord too (and vice versa).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from config import STATE_DIR
from shared import file_lock, load_state, save_state

VoiceReplyMode = Literal["always", "auto", "off"]

VOICE_REPLY_MODES: tuple[VoiceReplyMode, ...] = ("always", "auto", "off")
VOICE_REPLY_STATE_PATH = Path(STATE_DIR) / "voice-reply-preference.json"
_VOICE_REPLY_LOCK_PATH = VOICE_REPLY_STATE_PATH.with_suffix(".lock")


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

