"""Centralized subprocess env scrubbing — absorbed from dashboard_bot_lifecycle Phase 3 seed.

Drops dashboard-only env vars + secret-shaped non-whitelisted keys. Forces
HOMIE_HOME to the target persona's profile root. Preserves HOME/USERPROFILE
(Max OAuth credentials path lookup).

Phase 4 prep: GROQ_, GRADIUM_, DAILY_ added to bot-creds whitelist.
Phase 4 (R1 B5 fix): MISTRAL_ and GOOGLE_ added to bot-creds whitelist so
        Mistral Voxtral STT/TTS and Gemini TTS keys survive the scrub in
        bot/persona subprocesses.
R2 NB2: CLAUDE_CODE_ added to bot-creds whitelist (CLAUDE_CODE_OAUTH_TOKEN
        for CI/container deploys).
R2 NB2: CLAUDE_CONFIG_DIR added to Max OAuth carve-out (config-dir override).

Rule 1 enforced: parent_env=None resolves to os.environ.copy() at call time;
profile_root=None raises ValueError (caller MUST pass an explicit target).

Rule 2 enforced: no module-level cache of env state.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Single source of truth — imported from security/patterns.py. Used by the
# parity test in test_security_patterns_parity.py to confirm subprocess_env
# is a SECRET_PREFIXES consumer.
from security.patterns import SECRET_PREFIXES  # noqa: F401 — used for parity test

# Dashboard-only keys (matches Phase 3 seed verbatim).
_DASHBOARD_ONLY_KEYS: frozenset[str] = frozenset({
    "DASHBOARD_TOKEN",
    "DASHBOARD_BIND",
    "DASHBOARD_PORT",
    "DASHBOARD_DB_PATH",
    "DASHBOARD_DEV_MODE_NO_AUTH",
})

# Bot-creds whitelist — env var prefixes that the BOT subprocess SHOULD inherit.
# Phase 7a additions: GROQ_, GRADIUM_, DAILY_ (Phase 4 prep). ELEVENLABS_ already
# present in the Phase 3 seed. R2 NB2 fix: CLAUDE_CODE_ added so containers/CI
# using CLAUDE_CODE_OAUTH_TOKEN keep SDK auth (documented at .env.example).
_BOT_CREDS_PREFIXES: tuple[str, ...] = (
    "TELEGRAM_",
    "ANTHROPIC_",
    "OPENAI_",
    "GEMINI_",
    "GOOGLE_",        # Phase 4 (R1 B5) — Gemini TTS reuses GOOGLE_API_KEY
    "OPENROUTER_",
    "KIMI_",
    "LANGFUSE_",
    "DISCORD_",
    "SLACK_",
    "WHATSAPP_",
    "ELEVENLABS_",
    "GROQ_",          # Phase 4
    "GRADIUM_",       # Phase 4
    "DAILY_",         # Phase 4 (Daily.co Cabinet voice)
    "MISTRAL_",       # Phase 4 (R1 B5) — Mistral Voxtral STT+TTS shared key
    "CLAUDE_CODE_",   # R2 NB2 — CLAUDE_CODE_OAUTH_TOKEN for CI/container deploys
)

# Secret-shaped key heuristic — env var names that suggest credential material.
# Compared case-insensitively. Anything matching this AND not matching a
# _BOT_CREDS_PREFIXES prefix gets scrubbed.
_SECRET_SHAPED_RE = re.compile(
    r"(?:_TOKEN|_KEY|_SECRET|_PASSWORD|_PASSWD|_PWD|_API|_CREDENTIALS?|_CERT)$",
    re.IGNORECASE,
)

# Max OAuth carve-out — env vars that MUST always be preserved so the Claude
# Agent SDK can locate ~/.claude/.credentials.json. As of 2026-05-07 verification,
# the SDK reads the file relative to $HOME / $USERPROFILE (no dedicated env var
# for the file path). R2 NB2 also adds CLAUDE_CONFIG_DIR (used to override the
# config dir when set) to the carve-out so containers can point to a non-default
# config location without losing auth.
_MAX_OAUTH_CARVE_OUT: frozenset[str] = frozenset({
    "HOME",
    "USERPROFILE",
    "USER",
    "USERNAME",
    "LOGNAME",
    "CLAUDE_CONFIG_DIR",  # R2 NB2 — Claude Agent SDK config-dir override
})


def _is_bot_creds_key(name: str) -> bool:
    """True iff *name* belongs to the bot-creds whitelist (prefix match)."""
    upper = name.upper()
    return any(upper.startswith(p) for p in _BOT_CREDS_PREFIXES)


def get_scrubbed_sdk_env(
    parent_env: dict[str, str] | None = None,
    profile_root: Path | None = None,
) -> dict[str, str]:
    """Return a sanitized env dict for a persona-bot or voice subprocess.

    Drops dashboard-only keys + pattern-matched secret-shaped keys not on the
    bot-creds whitelist. Preserves HOME/USERPROFILE/USER for Max OAuth lookup.
    Forces HOMIE_HOME to *profile_root* so the child resolves the TARGET
    persona's paths.

    Rule 1: both args None-sentineled and resolved in body.
        - parent_env=None  → os.environ.copy() at call time
        - profile_root=None → ValueError (caller MUST pass an explicit target)
    """
    if parent_env is None:
        parent_env = os.environ.copy()
    if profile_root is None:
        raise ValueError(
            "get_scrubbed_sdk_env: profile_root MUST be passed explicitly "
            "(do NOT silently inherit caller's HOMIE_HOME)"
        )

    out: dict[str, str] = {}
    for key, value in parent_env.items():
        # Always preserve Max OAuth carve-out keys.
        if key in _MAX_OAUTH_CARVE_OUT:
            out[key] = value
            continue
        if key in _DASHBOARD_ONLY_KEYS:
            continue
        if _SECRET_SHAPED_RE.search(key) and not _is_bot_creds_key(key):
            continue
        out[key] = value

    out["HOMIE_HOME"] = str(profile_root)
    return out


__all__ = ["get_scrubbed_sdk_env"]
