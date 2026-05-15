"""Single source of truth for secret-prefix patterns.

Imported by:
  - scripts/sanitize.py (LEAK_PATTERNS expansion) — see sys.path bootstrap there
  - runtime/subprocess_env.py (env-var key heuristic + scrub parity)
  - (future Phase 7b) runtime/redact.py (log-line scrubber)

Phase 2 identity_payload class-of-bug at the security layer: when a new vendor
key shape ships, ONE constant updates and all consumers auto-pick it up.

Rule 3 N/A here (no SDK touched).
Rule 1 N/A (no functions with config defaults).
Rule 2 N/A (no module-level state caching).

R1 B2 fix: explicit ≥27-prefix catalog including Stripe/SendGrid/Mailgun/
Heroku/Postmark shapes that the original 22-tuple omitted.
R1 B3 fix: ordered descending by prefix length so most-specific prefix wins
('sk-ant-' labels Anthropic, NOT 'openai').
"""

from __future__ import annotations

import re

# R1 B3 fix — explicit priority order. We sort BY DESCENDING prefix length so
# more-specific prefixes are evaluated first. `sk-ant-` (7 chars) wins over
# `sk-` (3 chars); `sk-proj-` (8 chars) wins over `sk-`; `sk_live_` (8 chars)
# wins over `sk_` (3 chars). Existing scrub_content() at scripts/sanitize.py
# applies REPLACEMENTS in declared order — by anchoring on length-sorted
# order at module load time, we guarantee correctness regardless of dict
# ordering.
_RAW_PREFIX_VENDOR_MAP: dict[str, str] = {
    # Anthropic / OpenAI / Langfuse — most-specific FIRST (length-sorted later)
    "sk-proj-": "openai",          # OpenAI new (2024+)
    "sk-ant-": "anthropic",        # Anthropic
    "sk-lf-": "langfuse",          # Langfuse secret key
    "pk-lf-": "langfuse",          # Langfuse public key
    "sk-": "openai",               # OpenAI legacy (catch-all — runs LAST)
    # Stripe — R1 B2 added (live + test variants)
    "sk_live_": "stripe",
    "sk_test_": "stripe",
    "rk_live_": "stripe",
    "pk_live_": "stripe",
    # ElevenLabs / Groq / Gradium — Phase 4 prep
    "sk_": "elevenlabs",           # ElevenLabs (catch-all 'sk_' — runs after Stripe)
    "gsk_": "groq",
    "gr_": "gradium",
    # Slack
    "xoxb-": "slack",
    "xoxp-": "slack",
    "xapp-": "slack",
    # GitHub
    "ghp_": "github",
    "gho_": "github",
    "ghu_": "github",
    "ghs_": "github",
    "ghr_": "github",
    # AWS
    "AKIA": "aws",
    "arn:aws:": "aws-arn",         # R1 minor — ARN classified separately (identifier, not credential)
    # Google
    "AIza": "google",
    "ya29.": "google",
    # JWT
    "eyJ": "jwt",
    # Package registries
    "npm_": "npm",
    "dckr_": "docker",
    "glpat-": "gitlab",
    # R1 B2 additions — common vendor key shapes the original 22-tuple omitted
    "SG.": "sendgrid",             # SendGrid API key
    "key-": "mailgun",             # Mailgun API key
    "HRKU-": "heroku",             # Heroku API key
    "pcp_": "postmark",            # Postmark account/server token
}

# R1 B3 fix — ordered tuple: descending prefix length. Most-specific prefix
# evaluated first when scrubbing. Tuple is IMMUTABLE — a list would allow
# runtime mutation by an adversary (defense-in-depth).
SECRET_PREFIXES: tuple[str, ...] = tuple(
    sorted(_RAW_PREFIX_VENDOR_MAP.keys(), key=lambda p: (-len(p), p))
)

# Compiled patterns — for sanitize.py LEAK_PATTERNS use. Each prefix is
# anchored to a 16+ char alphanum/dot/dash tail (matches existing sk-proj-
# shape at sanitize.py:316). Built in SECRET_PREFIXES order (length-desc) so
# consumers iterating in tuple order get correct precedence.
LEAK_PATTERN_REGEX: tuple[re.Pattern[str], ...] = tuple(
    re.compile(r"(?<![A-Za-z0-9_])" + re.escape(prefix) + r"[A-Za-z0-9_.\-]{16,}")
    for prefix in SECRET_PREFIXES
)

# Replacement map — vendor name per prefix for `<REDACTED-{vendor}>`
# placeholders. Iterated via SECRET_PREFIXES order (length-desc) → most-
# specific labels apply first. dict() preserves insertion order in Python
# 3.7+ but consumers MUST iterate via SECRET_PREFIXES, NOT via
# PREFIX_VENDOR_MAP.keys(), to guarantee precedence.
PREFIX_VENDOR_MAP: dict[str, str] = {
    prefix: _RAW_PREFIX_VENDOR_MAP[prefix] for prefix in SECRET_PREFIXES
}

# R1 minor — separate ARN classification list (ARNs are identifiers, not
# always leaked secrets; flagging ARNs for visibility but with a different
# label).
ARN_PREFIXES: tuple[str, ...] = ("arn:aws:",)


def contains_leak_pattern(sample: str) -> bool:
    """Pure helper used by sanitize.py + sanitize_test.py.

    Returns True iff *sample* contains any LEAK_PATTERN_REGEX match. R1 M2
    fix: avoids the test "invent a private helper" trap. validate_output()
    and tests both call this function.
    """
    return any(p.search(sample) for p in LEAK_PATTERN_REGEX)


__all__ = [
    "ARN_PREFIXES",
    "LEAK_PATTERN_REGEX",
    "PREFIX_VENDOR_MAP",
    "SECRET_PREFIXES",
    "contains_leak_pattern",
]
