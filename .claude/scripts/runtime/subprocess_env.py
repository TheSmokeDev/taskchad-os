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
Issue #128: get_scrubbed_tool_sandbox_env() added — a second, narrower
        scrubbing contract for the Codex/Gemini CLI tool-sandbox child
        (external coding-tool CLI, not the bot). Does NOT force HOMIE_HOME
        or require profile_root, unlike get_scrubbed_sdk_env.

Rule 1 enforced: parent_env=None resolves to os.environ.copy() at call time;
profile_root=None raises ValueError (caller MUST pass an explicit target).

Rule 2 enforced: no module-level cache of env state.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
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

# PRD-8 Phase 7b WS5 (codex post-build F3) — exact drops mirroring ClaudeClaw
# SDK_DROP_VARS_SECRETS that are NOT covered by either ``_DASHBOARD_ONLY_KEYS``
# (dashboard-secret family) or ``_SECRET_SHAPED_RE`` (suffix matching). PIN_HASH
# is the ClaudeClaw signature case: a credential-shaped env var that does NOT
# end in _TOKEN/_KEY/_SECRET/etc. and so escapes the heuristic regex without
# this explicit drop.
_EXTRA_EXACT_DROPS: frozenset[str] = frozenset({
    "PIN_HASH",  # ClaudeClaw security.ts:266
})

# PRD-8 Phase 7b WS5 — nested Claude-Code-session state (ClaudeClaw security.ts:235-243
# parity). When the parent process is itself running inside Claude Code (e.g. a dev
# session spawning the bot), these env vars expose the parent's IPC/SSE state. A
# child SDK process inheriting them can:
#   - try to attach to the parent's IPC socket (legacy bug in early SDK builds)
#   - leak parent session metadata (entrypoint, execpath) into model context
#   - inherit the parent's max-output-tokens cap (incorrect for the child)
#
# We DROP these unconditionally — they're never useful to a child SDK process and
# they're not auth secrets, just session-state leakage. ``CLAUDECODE`` (no
# underscore) deliberately precedes the prefix-allowlist check so the
# CLAUDE_CODE_OAUTH_TOKEN whitelist doesn't inadvertently re-admit
# ``CLAUDE_CODE_ENTRYPOINT``/``_EXECPATH``/etc. via prefix match.
_NESTED_CLAUDE_CODE_STATE_KEYS: frozenset[str] = frozenset({
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_IPC_PORT",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
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

# Issue #128 — tool-sandbox creds whitelist. Deliberately far narrower than
# _BOT_CREDS_PREFIXES: the Codex/Gemini CLI TOOL_REASONING child (scheduled
# heartbeat/reflection/weekly/dream jobs, danger-full-access sandbox, ingesting
# untrusted email/HARO content) is an EXTERNAL coding-tool CLI, not the persona
# bot. It has no legitimate use for TELEGRAM_/DISCORD_/SLACK_/WHATSAPP_/
# LANGFUSE_/ELEVENLABS_/GROQ_/GRADIUM_/DAILY_/MISTRAL_/CLAUDE_CODE_/KIMI_/
# ANTHROPIC_/OPENROUTER_ creds — only its own provider auth.
#
# Traced against auth_profiles.py, the only secret-shaped keys either CLI child
# actually needs:
#   - GEMINI_API_KEY / GOOGLE_API_KEY   → the "gemini-api-key"/"api-key" branch
#     of gemini_auth_status()
#   - GOOGLE_APPLICATION_CREDENTIALS    → vertex service-account auth (matches
#     the _CREDENTIALS$ regex, so it needs the GOOGLE_ prefix to survive)
# GOOGLE_GENAI_USE_VERTEXAI / GOOGLE_CLOUD_PROJECT are not secret-shaped and
# survive regardless. Codex subscription auth is $HOME-rooted (~/.codex/auth.json)
# and rides _MAX_OAUTH_CARVE_OUT.
#
# OPENAI_ is deliberately ABSENT (deviation from the #128 investigation artifact,
# which kept it "for symmetry"). codex_auth_status() gates on `codex login status`
# (subscription) and never reads OPENAI_API_KEY; the key belongs to the separate
# openai_compatible.py HTTP adapter, which reads it in-process via profile.api_key
# and spawns no child. Issue #128 names OPENAI_API_KEY in the leak set, so it is
# scrubbed. NOTE: a deployment authenticating the Codex CLI by API key rather
# than subscription would need OPENAI_ added back here.
_TOOL_SANDBOX_CREDS_PREFIXES: tuple[str, ...] = (
    "GEMINI_",
    "GOOGLE_",
)


def _is_tool_sandbox_creds_key(name: str) -> bool:
    """True iff *name* belongs to the tool-sandbox creds whitelist (prefix match)."""
    upper = name.upper()
    return any(upper.startswith(p) for p in _TOOL_SANDBOX_CREDS_PREFIXES)


# gate/#140 — credential-FILE-PATH and connection-string shapes that
# _SECRET_SHAPED_RE (a _TOKEN|_KEY|..|_CERT suffix match) MISSES because they end
# in _PATH/_URL/_DSN. The persona-bot scrubber (get_scrubbed_sdk_env) KEEPS these
# — the bot reads TELLER_CERT_PATH/_KEY_PATH for finance sync — but the UNTRUSTED
# Codex/Gemini tool-sandbox child must not receive them. Tight by construction so
# PYTHONPATH / LD_LIBRARY_PATH / GOPATH / PATH and plain endpoint URLs survive: a
# credential token must PRECEDE _PATH/_FILE/_DIR, or it is a known connection-DSN.
_TOOL_SANDBOX_EXTRA_SECRET_RE = re.compile(
    r"(?:CERT|KEY|SECRET|TOKEN|CREDENTIAL|PRIVATE)[A-Z0-9]*_(?:PATH|FILE|DIR)$"
    r"|(?:DATABASE|REDIS|POSTGRES|POSTGRESQL|MYSQL|MONGO|AMQP)[A-Z0-9_]*_URL$"
    r"|_(?:DSN|CONNECTION_STRING)$",
    re.IGNORECASE,
)

# Secret-shaped key heuristic — env var names that suggest credential material.
# Compared case-insensitively. Anything matching this AND not matching a
# _BOT_CREDS_PREFIXES prefix gets scrubbed.
#
# PRD-8 Phase 7b WS5 (codex post-build F3): added ``^SECRET_`` prefix
# branch to mirror ClaudeClaw security.ts:278 exactly. Without it, names
# like ``SECRET_FOO`` would survive the scrub since they have no
# secret-shaped suffix and aren't on any whitelist prefix.
_SECRET_SHAPED_RE = re.compile(
    r"^SECRET_|"
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


def _apply_common_env_filters(
    parent_env: dict[str, str],
    is_creds_key: Callable[[str], bool],
) -> dict[str, str]:
    """Shared drop-precedence chain for both subprocess-env scrubbers.

    Carve-out → dashboard-only → exact drops → nested Claude-Code state →
    secret-shaped-and-not-whitelisted. Callers differ only in *is_creds_key*
    and any post-processing (e.g. ``get_scrubbed_sdk_env`` forcing
    ``HOMIE_HOME``).
    """
    out: dict[str, str] = {}
    for key, value in parent_env.items():
        # Always preserve Max OAuth carve-out keys.
        if key in _MAX_OAUTH_CARVE_OUT:
            out[key] = value
            continue
        if key in _DASHBOARD_ONLY_KEYS:
            continue
        # PRD-8 Phase 7b WS5 (codex post-build F3) — drop ClaudeClaw-mirror
        # exact secrets (PIN_HASH and any future names that escape the
        # suffix regex) BEFORE the creds-prefix check.
        if key in _EXTRA_EXACT_DROPS:
            continue
        # PRD-8 Phase 7b WS5 — drop nested Claude-Code-session state
        # BEFORE the creds-prefix check, so CLAUDE_CODE_ENTRYPOINT/
        # _EXECPATH/_SSE_PORT/_IPC_PORT/_MAX_OUTPUT_TOKENS/
        # _EXPERIMENTAL_AGENT_TEAMS aren't preserved by a
        # ``CLAUDE_CODE_`` whitelist prefix (that prefix exists for
        # CLAUDE_CODE_OAUTH_TOKEN; the IPC/state vars must NOT inherit).
        if key in _NESTED_CLAUDE_CODE_STATE_KEYS:
            continue
        if _SECRET_SHAPED_RE.search(key) and not is_creds_key(key):
            continue
        out[key] = value
    return out


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

    out = _apply_common_env_filters(parent_env, _is_bot_creds_key)
    out["HOMIE_HOME"] = str(profile_root)
    return out


def get_scrubbed_tool_sandbox_env(
    parent_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a sanitized env dict for a generic-lane CLI tool-sandbox child.

    Also drops credential-file-path / connection-string (DSN) shapes that the
    ``_SECRET_SHAPED_RE`` suffix heuristic misses (see gate/#140 below).

    Issue #128 — applied unconditionally to every Codex ``exec`` / Gemini CLI
    child, regardless of request capability or resolved sandbox mode. The
    motivating risk: scheduled jobs (heartbeat/reflection/weekly/dream) run a
    TOOL_REASONING child in a ``danger-full-access`` sandbox with a Bash tool
    while ingesting untrusted content, so a prompt-injected turn could
    ``printenv`` whatever it inherits — but nothing about that risk is unique
    to TOOL_REASONING, so the scrub is not gated on it.

    Tighter than ``get_scrubbed_sdk_env``: that function's whitelist
    (``TELEGRAM_``, ``DISCORD_``, ``SLACK_``, ``LANGFUSE_``, ``ELEVENLABS_``,
    ...) exists for the persona BOT's own subprocess, which legitimately needs
    those integration creds to run itself. This child is an external
    coding-tool CLI — not the bot — and only needs its own provider auth
    (``GEMINI_``/``GOOGLE_``) plus ordinary system env (PATH, HOME, ...).

    Unlike ``get_scrubbed_sdk_env`` this does NOT force ``HOMIE_HOME`` and does
    NOT require ``profile_root`` — the external CLI has no concept of Homie
    profiles.

    Rule 1: ``parent_env=None`` resolves to ``os.environ.copy()`` at call time.
    """
    if parent_env is None:
        parent_env = os.environ.copy()
    out = _apply_common_env_filters(parent_env, _is_tool_sandbox_creds_key)
    # gate/#140 — also drop credential-file-path & connection-string shapes the
    # suffix heuristic misses, UNLESS they are provider auth this child needs.
    return {
        key: value
        for key, value in out.items()
        if _is_tool_sandbox_creds_key(key)
        or not _TOOL_SANDBOX_EXTRA_SECRET_RE.search(key)
    }


def scrub_nested_claude_state(
    parent_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Copy *parent_env* with nested Claude-Code session markers removed and
    EVERYTHING else preserved — including ``HOMIE_HOME`` exactly as-is.

    For SELF-restart of an SDK process (the bot relaunching itself). The child
    must NOT inherit ``CLAUDECODE`` / ``CLAUDE_CODE_ENTRYPOINT`` / etc. or the
    Claude Agent SDK refuses to launch ("cannot be launched inside another
    Claude Code session"). Unlike ``get_scrubbed_sdk_env`` this does NOT force
    ``HOMIE_HOME`` or drop secret-shaped keys: a self-restart keeps the SAME
    profile and the SAME process env, shedding only the nesting markers. Reuses
    ``_NESTED_CLAUDE_CODE_STATE_KEYS`` as the single source of truth.

    Rule 1: ``parent_env=None`` resolves to ``os.environ.copy()`` at call time.
    """
    if parent_env is None:
        parent_env = os.environ.copy()
    return {
        key: value
        for key, value in parent_env.items()
        if key not in _NESTED_CLAUDE_CODE_STATE_KEYS
    }


__all__ = [
    "get_scrubbed_sdk_env",
    "get_scrubbed_tool_sandbox_env",
    "scrub_nested_claude_state",
]
