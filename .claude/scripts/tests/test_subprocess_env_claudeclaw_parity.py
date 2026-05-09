"""PRD-8 Phase 7b (WS5) — ClaudeClaw ``getScrubbedSdkEnv`` parity tests.

Audits ``runtime/subprocess_env.get_scrubbed_sdk_env()`` against the upstream
ClaudeClaw scrub at ``~/.refs/claudeclaw-os/src/security.ts:200-334``.

Categories enumerated from upstream:
  * SDK_DROP_VARS_NESTED_CLAUDE (security.ts:235-243) — 7 keys:
      CLAUDECODE, CLAUDE_CODE_ENTRYPOINT, CLAUDE_CODE_EXECPATH,
      CLAUDE_CODE_SSE_PORT, CLAUDE_CODE_IPC_PORT,
      CLAUDE_CODE_MAX_OUTPUT_TOKENS, CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS.
      → Critical: dropping these prevents the SDK child from attaching to
      the parent IPC socket (legacy SDK bug) and leaking session metadata.

  * SDK_DROP_VARS_SECRETS (security.ts:246-268) — 21 explicit secret env
    names (DASHBOARD_TOKEN, DAILY_API_KEY, GROQ_API_KEY, etc.).
      → Different model: ClaudeClaw DROPS by name then re-injects auth
      via ``authSecrets`` arg. Our model PRESERVES bot creds via
      ``_BOT_CREDS_PREFIXES`` whitelist (the bot subprocess legitimately
      needs them). DASHBOARD_* is covered by ``_DASHBOARD_ONLY_KEYS``.
      Most others end in ``_TOKEN``/``_KEY`` so they survive their
      respective bot-creds prefix OR are dropped by ``_SECRET_SHAPED_RE``.
      The EXCEPTION is ``PIN_HASH`` (no secret-shaped suffix, no bot-creds
      prefix) — added to ``_EXTRA_EXACT_DROPS`` per codex post-build F3.

  * SDK_SECRET_NAME_PATTERNS (security.ts:274-279) — 4 regex
    (_API_KEY$, _TOKEN$, _SECRET$, ^SECRET_).
      → Our ``_SECRET_SHAPED_RE`` covers _TOKEN, _KEY, _SECRET, _PASSWORD,
      _PASSWD, _PWD, _API, _CREDENTIALS?, _CERT (suffix) PLUS ``^SECRET_``
      (prefix branch added per codex post-build F3 — names like
      ``SECRET_FOO`` were surviving the scrub before).

  * SDK_AUTH_VARS (security.ts:281) — 2 preserve-by-default:
      CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY.
      → Our ``_BOT_CREDS_PREFIXES`` includes CLAUDE_CODE_ and ANTHROPIC_
      (covers both auth vars).

WS5 deliverable (PRD §232 escape hatch): if 5+ categories are missing,
surface as Phase 7b post-merge follow-up artefact in PRPs/planning/. As of
this commit, ZERO categories missing — the nested-Claude-state gap was
closed by adding ``_NESTED_CLAUDE_CODE_STATE_KEYS`` to subprocess_env.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from runtime import subprocess_env

# ──────────────────────────────────────────────────────────────────────
# Category 1: nested Claude-Code-session state (ClaudeClaw security.ts:235-243)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_IPC_PORT",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    ],
)
def test_nested_claude_code_state_var_dropped(key, tmp_path):
    """Each nested Claude-Code-session state var is DROPPED unconditionally.

    Critical: the ``CLAUDE_CODE_`` prefix in ``_BOT_CREDS_PREFIXES`` exists
    for ``CLAUDE_CODE_OAUTH_TOKEN``. WITHOUT the explicit
    ``_NESTED_CLAUDE_CODE_STATE_KEYS`` drop, the prefix would re-admit
    ``CLAUDE_CODE_ENTRYPOINT``/``_EXECPATH``/etc. — which are session
    state, not auth. This test asserts the drop fires BEFORE the prefix
    check.
    """
    parent_env = {key: "should-be-dropped", "PATH": "/bin"}
    out = subprocess_env.get_scrubbed_sdk_env(
        parent_env=parent_env,
        profile_root=tmp_path,
    )
    assert key not in out, (
        f"{key} must be dropped by subprocess_env "
        "(ClaudeClaw security.ts:235-243 parity)"
    )


def test_claudecode_dropped_but_oauth_token_preserved(tmp_path):
    """``CLAUDECODE`` (no underscore) is dropped; ``CLAUDE_CODE_OAUTH_TOKEN`` survives.

    Validates the ordering — nested-state drop fires BEFORE the
    bot-creds prefix check, so the prefix doesn't re-admit session state.
    The OAuth token (a legitimate auth secret) must survive.
    """
    parent_env = {
        "CLAUDECODE": "session-state",
        "CLAUDE_CODE_ENTRYPOINT": "ipc-leak",
        "CLAUDE_CODE_OAUTH_TOKEN": "real-oauth-token",
        "PATH": "/bin",
    }
    out = subprocess_env.get_scrubbed_sdk_env(
        parent_env=parent_env,
        profile_root=tmp_path,
    )
    assert "CLAUDECODE" not in out
    assert "CLAUDE_CODE_ENTRYPOINT" not in out
    assert out.get("CLAUDE_CODE_OAUTH_TOKEN") == "real-oauth-token"


# ──────────────────────────────────────────────────────────────────────
# Category 2: dashboard secrets — DASHBOARD_TOKEN class
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "DASHBOARD_TOKEN",
        "DASHBOARD_BIND",
        "DASHBOARD_PORT",
        "DASHBOARD_DB_PATH",
        "DASHBOARD_DEV_MODE_NO_AUTH",
    ],
)
def test_dashboard_only_key_dropped(key, tmp_path):
    """``_DASHBOARD_ONLY_KEYS`` covers the dashboard-only secrets category.

    ClaudeClaw drops DASHBOARD_TOKEN explicitly via SDK_DROP_VARS_SECRETS;
    we drop the entire DASHBOARD_* dashboard-secret family.
    """
    parent_env = {key: "secret-value", "PATH": "/bin"}
    out = subprocess_env.get_scrubbed_sdk_env(
        parent_env=parent_env,
        profile_root=tmp_path,
    )
    assert key not in out


# ──────────────────────────────────────────────────────────────────────
# Category 3: secret-shape pattern coverage (ClaudeClaw security.ts:274-279)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "RANDOM_API_KEY",       # _API_KEY$
        "WEIRDVENDOR_TOKEN",    # _TOKEN$
        "MAGIC_SECRET",         # _SECRET$
        # PRD-8 Phase 7b codex post-build F3 — ^SECRET_ prefix branch.
        "SECRET_FOO",           # ^SECRET_ prefix
        "SECRET_BAR_BAZ",       # ^SECRET_ prefix, multi-segment name
    ],
)
def test_secret_shaped_keys_dropped_when_no_whitelist(key, tmp_path):
    """Keys matching ``_SECRET_SHAPED_RE`` AND not on bot-creds whitelist drop.

    Coverage of ClaudeClaw SDK_SECRET_NAME_PATTERNS — all 4 branches
    (_API_KEY$, _TOKEN$, _SECRET$, ^SECRET_) — codex post-build F3 added
    the prefix branch.
    """
    parent_env = {key: "secret-value", "PATH": "/bin"}
    out = subprocess_env.get_scrubbed_sdk_env(
        parent_env=parent_env,
        profile_root=tmp_path,
    )
    assert key not in out


# ──────────────────────────────────────────────────────────────────────
# Category 3b: ClaudeClaw exact-name drops not covered by suffix regex
# (codex post-build F3)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "PIN_HASH",  # ClaudeClaw security.ts:266 — escapes both regex + whitelist
    ],
)
def test_extra_exact_drops_dropped(key, tmp_path):
    """Keys in ``_EXTRA_EXACT_DROPS`` drop unconditionally.

    ``PIN_HASH`` is the codex post-build F3 signature case — no
    secret-shaped suffix, no bot-creds prefix, but ClaudeClaw drops it
    explicitly. Without ``_EXTRA_EXACT_DROPS`` it would survive.
    """
    parent_env = {key: "should-be-dropped", "PATH": "/bin"}
    out = subprocess_env.get_scrubbed_sdk_env(
        parent_env=parent_env,
        profile_root=tmp_path,
    )
    assert key not in out, (
        f"{key} must be dropped by _EXTRA_EXACT_DROPS "
        "(ClaudeClaw security.ts:246-268 parity)"
    )


def test_extra_exact_drops_set_matches_claudeclaw_signature_cases():
    """Set parity check — ``_EXTRA_EXACT_DROPS`` must contain the names
    that ClaudeClaw drops via SDK_DROP_VARS_SECRETS but escape our suffix
    regex AND our bot-creds prefix whitelist.

    Currently the documented set is ``{PIN_HASH}``. If a future ClaudeClaw
    audit surfaces another such name, add it to ``_EXTRA_EXACT_DROPS`` AND
    here.
    """
    expected = {"PIN_HASH"}
    actual = set(subprocess_env._EXTRA_EXACT_DROPS)
    missing = expected - actual
    assert not missing, (
        f"_EXTRA_EXACT_DROPS missing ClaudeClaw signature names: {missing}"
    )


# ──────────────────────────────────────────────────────────────────────
# Category 4: SDK auth vars preserved
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"],
)
def test_sdk_auth_var_preserved(key, tmp_path):
    """Both ClaudeClaw SDK_AUTH_VARS survive — they're on bot-creds whitelist.

    Without at least one of these, the SDK subprocess can't authenticate.
    """
    parent_env = {key: "auth-value", "PATH": "/bin"}
    out = subprocess_env.get_scrubbed_sdk_env(
        parent_env=parent_env,
        profile_root=tmp_path,
    )
    assert out.get(key) == "auth-value"


# ──────────────────────────────────────────────────────────────────────
# Category 5: bot-creds whitelist preserved (intentional model deviation)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "TELEGRAM_BOT_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ELEVENLABS_API_KEY",
        "GROQ_API_KEY",
        "DAILY_API_KEY",
        "DISCORD_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
        "GITHUB_TOKEN",  # NOT on whitelist — should be dropped (regression check below)
    ],
)
def test_bot_creds_whitelist_behavior(key, tmp_path):
    """Bot-creds whitelist preserves keys with whitelisted prefix; others drop.

    Documented model deviation from ClaudeClaw — our bot subprocess legitimately
    needs Telegram/voice/LLM provider keys, so we PRESERVE them via prefix
    whitelist instead of dropping + re-injecting. Keys with no whitelist
    prefix that match ``_SECRET_SHAPED_RE`` still drop.
    """
    parent_env = {key: "value", "PATH": "/bin"}
    out = subprocess_env.get_scrubbed_sdk_env(
        parent_env=parent_env,
        profile_root=tmp_path,
    )

    # Compute expected result from the actual whitelist + secret-shape regex.
    is_whitelisted = subprocess_env._is_bot_creds_key(key)
    is_secret_shaped = bool(subprocess_env._SECRET_SHAPED_RE.search(key))

    if is_secret_shaped and not is_whitelisted:
        assert key not in out, f"{key} (secret-shaped, not whitelisted) must drop"
    elif is_whitelisted:
        assert out.get(key) == "value", f"{key} (whitelisted) must survive"


def test_github_token_drops_because_not_whitelisted(tmp_path):
    """``GITHUB_TOKEN`` is a documented gap — NOT on our bot-creds whitelist.

    ClaudeClaw drops it explicitly. Our model drops it via
    ``_SECRET_SHAPED_RE`` (matches ``_TOKEN$``) since ``GITHUB_`` isn't a
    prefix in ``_BOT_CREDS_PREFIXES``.
    """
    parent_env = {"GITHUB_TOKEN": "ghp_secret", "PATH": "/bin"}
    out = subprocess_env.get_scrubbed_sdk_env(
        parent_env=parent_env,
        profile_root=tmp_path,
    )
    assert "GITHUB_TOKEN" not in out


# ──────────────────────────────────────────────────────────────────────
# Category 6: Max OAuth carve-out preserved
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        "HOME",
        "USERPROFILE",
        "USER",
        "USERNAME",
        "LOGNAME",
        "CLAUDE_CONFIG_DIR",
    ],
)
def test_max_oauth_carve_out_preserved(key, tmp_path):
    """Max OAuth carve-out keys survive the scrub.

    Without HOME/USERPROFILE the SDK can't locate ``~/.claude/.credentials.json``.
    R2 NB2 added CLAUDE_CONFIG_DIR for config-dir override.
    """
    parent_env = {key: "value", "PATH": "/bin"}
    out = subprocess_env.get_scrubbed_sdk_env(
        parent_env=parent_env,
        profile_root=tmp_path,
    )
    assert out.get(key) == "value"


# ──────────────────────────────────────────────────────────────────────
# Cross-reference: assert source-of-truth lists ARE the upstream lists
# ──────────────────────────────────────────────────────────────────────


def test_nested_claude_code_state_set_matches_claudeclaw_security_ts():
    """Set parity check — our ``_NESTED_CLAUDE_CODE_STATE_KEYS`` must contain
    every entry from ClaudeClaw security.ts:235-243.
    """
    upstream = {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_IPC_PORT",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    }
    ours = set(subprocess_env._NESTED_CLAUDE_CODE_STATE_KEYS)
    missing = upstream - ours
    assert not missing, (
        f"Missing nested-Claude-state keys from subprocess_env: {missing}. "
        "Add them to _NESTED_CLAUDE_CODE_STATE_KEYS."
    )


def test_claudeclaw_security_ts_file_unchanged_at_audit_lines():
    """Audit-anchor sanity check — assert the ClaudeClaw lines we audited
    against still exist at security.ts:200-334. If upstream restructures the
    file, this test fails and forces a re-audit before the next Phase 7b
    revision lands.

    Time-boxed at WS5 (per PRP escape hatch) — if upstream drift surfaces 5+
    new categories, surface as Phase 7b post-merge follow-up rather than
    expanding scope mid-commit.
    """
    upstream_path = Path("~/.refs/claudeclaw-os/src/security.ts")
    if not upstream_path.is_file():
        pytest.skip("ClaudeClaw upstream not available on this machine")
    text = upstream_path.read_text(encoding="utf-8")
    # Sentinel lines from the audited region.
    assert "SDK_DROP_VARS_NESTED_CLAUDE" in text
    assert "SDK_DROP_VARS_SECRETS" in text
    assert "SDK_SECRET_NAME_PATTERNS" in text
    assert "SDK_AUTH_VARS" in text
    assert "getScrubbedSdkEnv" in text
