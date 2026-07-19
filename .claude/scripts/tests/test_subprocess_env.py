"""PRD-8 Phase 7a (WS3) — runtime/subprocess_env.get_scrubbed_sdk_env tests.

Asserts:
  - dashboard-only keys dropped (DASHBOARD_TOKEN/BIND/PORT/DB_PATH/DEV_MODE_NO_AUTH)
  - secret-shaped non-whitelisted keys dropped
  - bot-creds whitelist preserved (TELEGRAM_, ANTHROPIC_, OPENAI_, ELEVENLABS_,
    Phase 4 prep keys GROQ_, GRADIUM_, DAILY_, R2 NB2 CLAUDE_CODE_)
  - HOMIE_HOME forced to profile_root
  - parent_env=None resolves at call time (Rule 1)
  - profile_root=None raises ValueError (Rule 1)
  - Max OAuth carve-out preserved (HOME, USERPROFILE, CLAUDE_CONFIG_DIR)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from runtime.subprocess_env import get_scrubbed_sdk_env, get_scrubbed_tool_sandbox_env


def test_drop_dashboard_only_keys(tmp_path: Path) -> None:
    parent = {
        "DASHBOARD_TOKEN": "abc",
        "DASHBOARD_BIND": "127.0.0.1",
        "DASHBOARD_PORT": "4322",
        "DASHBOARD_DB_PATH": "/tmp/x.db",
        "DASHBOARD_DEV_MODE_NO_AUTH": "true",
        "PATH": "/usr/bin",
    }
    out = get_scrubbed_sdk_env(parent_env=parent, profile_root=tmp_path)
    for key in (
        "DASHBOARD_TOKEN",
        "DASHBOARD_BIND",
        "DASHBOARD_PORT",
        "DASHBOARD_DB_PATH",
        "DASHBOARD_DEV_MODE_NO_AUTH",
    ):
        assert key not in out
    assert out["PATH"] == "/usr/bin"


def test_drop_secret_shaped_non_whitelisted(tmp_path: Path) -> None:
    parent = {
        "RANDOM_API_KEY": "leaked",
        "STRIPE_SECRET": "sk_live_secret",
        "PATH": "/usr/bin",
    }
    out = get_scrubbed_sdk_env(parent_env=parent, profile_root=tmp_path)
    assert "RANDOM_API_KEY" not in out
    assert "STRIPE_SECRET" not in out
    assert out["PATH"] == "/usr/bin"


def test_whitelist_preserves_telegram_bot_token(tmp_path: Path) -> None:
    parent = {"TELEGRAM_BOT_TOKEN": "12345:abc"}
    out = get_scrubbed_sdk_env(parent_env=parent, profile_root=tmp_path)
    assert out["TELEGRAM_BOT_TOKEN"] == "12345:abc"


def test_whitelist_preserves_phase4_keys(tmp_path: Path) -> None:
    """Phase 4 (voice cascade) — GROQ_, GRADIUM_, DAILY_ all whitelisted."""
    parent = {
        "GROQ_API_KEY": "gsk_x",
        "GRADIUM_API_KEY": "gr_y",
        "DAILY_API_KEY": "daily_z",
    }
    out = get_scrubbed_sdk_env(parent_env=parent, profile_root=tmp_path)
    assert out["GROQ_API_KEY"] == "gsk_x"
    assert out["GRADIUM_API_KEY"] == "gr_y"
    assert out["DAILY_API_KEY"] == "daily_z"


def test_whitelist_preserves_elevenlabs_key(tmp_path: Path) -> None:
    """Regression — ELEVENLABS_ already shipped Phase 3, must remain."""
    parent = {"ELEVENLABS_API_KEY": "sk_eleven"}
    out = get_scrubbed_sdk_env(parent_env=parent, profile_root=tmp_path)
    assert out["ELEVENLABS_API_KEY"] == "sk_eleven"


def test_whitelist_preserves_claude_code_oauth_token(tmp_path: Path) -> None:
    """R2 NB2 — CLAUDE_CODE_OAUTH_TOKEN must survive scrub for CI/container deploys."""
    parent = {"CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth-tok"}
    out = get_scrubbed_sdk_env(parent_env=parent, profile_root=tmp_path)
    assert out["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-tok"


def test_homie_home_forced_to_profile_root(tmp_path: Path) -> None:
    parent = {"HOMIE_HOME": "/wrong/path"}
    target = tmp_path / "target_profile"
    out = get_scrubbed_sdk_env(parent_env=parent, profile_root=target)
    assert out["HOMIE_HOME"] == str(target)


def test_parent_env_none_resolves_at_call_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 1 — parent_env=None reads os.environ.copy() AT CALL TIME, not def time."""
    monkeypatch.setenv("HOMIE_PHASE7A_TEST_MARKER", "first")
    out1 = get_scrubbed_sdk_env(profile_root=tmp_path)
    assert out1.get("HOMIE_PHASE7A_TEST_MARKER") == "first"

    monkeypatch.setenv("HOMIE_PHASE7A_TEST_MARKER", "second")
    out2 = get_scrubbed_sdk_env(profile_root=tmp_path)
    assert out2.get("HOMIE_PHASE7A_TEST_MARKER") == "second"


def test_profile_root_none_raises_valueerror() -> None:
    """Rule 1 — profile_root=None must raise (no silent inheritance)."""
    with pytest.raises(ValueError, match="profile_root MUST be passed explicitly"):
        get_scrubbed_sdk_env(parent_env={"FOO": "bar"}, profile_root=None)


def test_max_oauth_carve_out_preserved(tmp_path: Path) -> None:
    """HOME / USERPROFILE / USER / USERNAME / LOGNAME / CLAUDE_CONFIG_DIR preserved."""
    parent = {
        "HOME": "/home/user",
        "USERPROFILE": r"C:\Users\user",
        "USER": "user",
        "USERNAME": "user",
        "LOGNAME": "user",
        "CLAUDE_CONFIG_DIR": "/etc/claude",
    }
    out = get_scrubbed_sdk_env(parent_env=parent, profile_root=tmp_path)
    for key, value in parent.items():
        assert out[key] == value


def test_max_oauth_subprocess_can_locate_credentials_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 M1 — real subprocess can resolve ~/.claude/.credentials.json via HOME/USERPROFILE.

    Creates a fake credentials file under tmp_path/.claude/.credentials.json,
    sets HOME (or USERPROFILE on Windows), runs scrubbed env through a real
    subprocess, asserts the subprocess can resolve the file. Skipped if the
    test environment lacks Python or the os.path resolution.
    """
    cred_dir = tmp_path / ".claude"
    cred_dir.mkdir()
    cred_file = cred_dir / ".credentials.json"
    cred_file.write_text("{}", encoding="utf-8")

    parent = os.environ.copy()
    if sys.platform == "win32":
        parent["USERPROFILE"] = str(tmp_path)
    else:
        parent["HOME"] = str(tmp_path)

    profile_target = tmp_path / "profile"
    out = get_scrubbed_sdk_env(parent_env=parent, profile_root=profile_target)
    if sys.platform == "win32":
        assert out["USERPROFILE"] == str(tmp_path)
    else:
        assert out["HOME"] == str(tmp_path)
    # Spawn a real subprocess and assert it can locate the file.
    code = (
        "import os\n"
        "from pathlib import Path\n"
        "h = os.environ.get('HOME') or os.environ.get('USERPROFILE')\n"
        "p = Path(h) / '.claude' / '.credentials.json'\n"
        "assert p.exists(), f'creds file not found at {p}'\n"
    )
    import subprocess
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=out,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"Subprocess could not resolve credentials path. stderr: {result.stderr}"
    )


def test_secret_prefixes_imported_from_security_patterns(tmp_path: Path) -> None:
    """Three-layer parity — subprocess_env imports SECRET_PREFIXES."""
    import runtime.subprocess_env as se
    # Smoke: the module-level import succeeded and the constant exists.
    assert hasattr(se, "SECRET_PREFIXES") or "SECRET_PREFIXES" in dir(se)


# ---------------------------------------------------------------------------
# Issue #128 — get_scrubbed_tool_sandbox_env (generic-lane CLI tool sandbox)
# ---------------------------------------------------------------------------


def test_tool_sandbox_env_drops_persona_bot_creds() -> None:
    """The Codex/Gemini CLI child is an external coding-tool CLI, not the bot —
    it has no legitimate use for the bot's integration creds, unlike
    get_scrubbed_sdk_env's persona-bot-subprocess whitelist."""
    parent = {
        "TELEGRAM_BOT_TOKEN": "12345:abc",
        "LANGFUSE_SECRET_KEY": "sk-lf-secret",
        "DISCORD_BOT_TOKEN": "discord-tok",
        "SLACK_BOT_TOKEN": "xoxb-slack",
        "WHATSAPP_API_KEY": "wa-key",
        "ELEVENLABS_API_KEY": "el-key",
        "GROQ_API_KEY": "gsk_x",
        "MISTRAL_API_KEY": "mistral-x",
        "ANTHROPIC_API_KEY": "sk-ant-x",
        "OPENROUTER_API_KEY": "or-x",
        "CLAUDE_CODE_OAUTH_TOKEN": "claude-oauth",
        "PATH": "/usr/bin",
    }
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "LANGFUSE_SECRET_KEY",
        "DISCORD_BOT_TOKEN",
        "SLACK_BOT_TOKEN",
        "WHATSAPP_API_KEY",
        "ELEVENLABS_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        assert key not in out
    assert out["PATH"] == "/usr/bin"


def test_tool_sandbox_env_drops_openai_api_key() -> None:
    """Issue #128 names OPENAI_API_KEY in the leak set. codex_auth_status()
    gates on `codex login status` (subscription) and never reads it; the key
    belongs to the separate openai_compatible.py HTTP adapter, which reads it
    in-process and spawns no child. So the CLI child must not receive it."""
    parent = {"OPENAI_API_KEY": "<REDACTED-openai>", "PATH": "/usr/bin"}
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    assert "OPENAI_API_KEY" not in out


def test_tool_sandbox_env_preserves_gemini_provider_auth() -> None:
    """Acceptance criterion — Gemini generic-lane runs still succeed. These are
    the only secret-shaped keys auth_profiles.py reads for the CLI child."""
    parent = {
        "GEMINI_API_KEY": "gm-key",
        "GOOGLE_API_KEY": "g-key",
        # Matches the _CREDENTIALS$ regex — needs the GOOGLE_ prefix to survive,
        # or vertex service-account auth breaks.
        "GOOGLE_APPLICATION_CREDENTIALS": "/etc/gcp/sa.json",
    }
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    assert out["GEMINI_API_KEY"] == "gm-key"
    assert out["GOOGLE_API_KEY"] == "g-key"
    assert out["GOOGLE_APPLICATION_CREDENTIALS"] == "/etc/gcp/sa.json"


def test_tool_sandbox_env_preserves_non_secret_shaped_runtime_vars() -> None:
    """Vertex/GCP selectors and Windows spawn vars aren't secret-shaped and must
    pass through untouched."""
    parent = {
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_CLOUD_PROJECT": "my-project",
        "SystemRoot": r"C:\Windows",
        "COMSPEC": r"C:\Windows\system32\cmd.exe",
        "TEMP": r"C:\Temp",
    }
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    for key, value in parent.items():
        assert out[key] == value


def test_tool_sandbox_env_preserves_home_for_subscription_auth() -> None:
    """Both CLIs' subscription auth is $HOME-rooted (~/.codex/auth.json,
    ~/.gemini/oauth_creds.json) — the carve-out must survive."""
    parent = {"HOME": "/home/user", "USERPROFILE": r"C:\Users\user"}
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    assert out["HOME"] == "/home/user"
    assert out["USERPROFILE"] == r"C:\Users\user"


def test_tool_sandbox_env_drops_generic_secret_shaped_keys() -> None:
    """Non-whitelisted secret-shaped keys are dropped, same heuristic as the
    bot scrubber."""
    parent = {
        "STRIPE_SECRET": "sk_live_x",
        "RANDOM_API_KEY": "leaked",
        "SECRET_FOO": "prefix-form",
        "PIN_HASH": "claudeclaw-signature-case",
        "DASHBOARD_TOKEN": "dash",
        "PATH": "/usr/bin",
    }
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    for key in ("STRIPE_SECRET", "RANDOM_API_KEY", "SECRET_FOO", "PIN_HASH", "DASHBOARD_TOKEN"):
        assert key not in out
    assert out["PATH"] == "/usr/bin"


def test_tool_sandbox_env_drops_all_dashboard_only_keys() -> None:
    """Mirrors test_drop_dashboard_only_keys (get_scrubbed_sdk_env) for the new
    scrubber — 4 of these 5 keys are NOT secret-shaped by suffix, so this is
    the only test that exercises the _DASHBOARD_ONLY_KEYS branch itself rather
    than the secret-shape regex catching it by coincidence."""
    parent = {
        "DASHBOARD_TOKEN": "abc",
        "DASHBOARD_BIND": "127.0.0.1",
        "DASHBOARD_PORT": "4322",
        "DASHBOARD_DB_PATH": "/tmp/x.db",
        "DASHBOARD_DEV_MODE_NO_AUTH": "true",
        "PATH": "/usr/bin",
    }
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    for key in (
        "DASHBOARD_TOKEN",
        "DASHBOARD_BIND",
        "DASHBOARD_PORT",
        "DASHBOARD_DB_PATH",
        "DASHBOARD_DEV_MODE_NO_AUTH",
    ):
        assert key not in out
    assert out["PATH"] == "/usr/bin"


def test_tool_sandbox_env_drops_nested_claude_code_state() -> None:
    parent = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "PATH": "/usr/bin"}
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    assert "CLAUDECODE" not in out
    assert "CLAUDE_CODE_ENTRYPOINT" not in out


def test_tool_sandbox_env_does_not_force_homie_home() -> None:
    """Unlike get_scrubbed_sdk_env, no profile_root is required or forced — the
    external CLI has no concept of Homie profiles, so HOMIE_HOME passes through
    as-is rather than being overwritten."""
    parent = {"HOMIE_HOME": "/whatever/it/was", "PATH": "/usr/bin"}
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    assert out["HOMIE_HOME"] == "/whatever/it/was"


def test_tool_sandbox_env_requires_no_profile_root() -> None:
    """Contrast with get_scrubbed_sdk_env, which raises without profile_root."""
    out = get_scrubbed_tool_sandbox_env(parent_env={"PATH": "/usr/bin"})
    assert out == {"PATH": "/usr/bin"}


def test_tool_sandbox_whitelist_is_narrower_than_bot_whitelist(tmp_path: Path) -> None:
    """The two scrubbers intentionally diverge: same key, different verdict,
    because the consumers have different threat models. Locks in that widening
    one does not silently widen the other."""
    parent = {"TELEGRAM_BOT_TOKEN": "12345:abc"}
    bot_env = get_scrubbed_sdk_env(parent_env=parent, profile_root=tmp_path)
    sandbox_env = get_scrubbed_tool_sandbox_env(parent_env=parent)
    assert bot_env["TELEGRAM_BOT_TOKEN"] == "12345:abc"  # bot IS the telegram bot
    assert "TELEGRAM_BOT_TOKEN" not in sandbox_env      # external CLI is not


def test_tool_sandbox_env_parent_env_none_resolves_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 1 — parent_env=None reads os.environ.copy() AT CALL TIME."""
    monkeypatch.setenv("HOMIE_ISSUE128_TEST_MARKER", "first")
    out1 = get_scrubbed_tool_sandbox_env()
    assert out1.get("HOMIE_ISSUE128_TEST_MARKER") == "first"

    monkeypatch.setenv("HOMIE_ISSUE128_TEST_MARKER", "second")
    out2 = get_scrubbed_tool_sandbox_env()
    assert out2.get("HOMIE_ISSUE128_TEST_MARKER") == "second"


# ---------------------------------------------------------------------------
# gate/#140 — credential-file-path & connection-string shapes the suffix
# heuristic (_SECRET_SHAPED_RE) misses because they end in _PATH/_URL/_DSN.
# ---------------------------------------------------------------------------


def test_tool_sandbox_env_drops_credential_file_paths_and_dsns() -> None:
    """gate/#140 — credential-FILE-PATH and connection-string (DSN) shapes end in
    _PATH/_URL/_DSN, so the _TOKEN|_KEY|..|_CERT suffix regex NEVER catches them.
    The persona-bot scrubber KEEPS these (finance sync reads TELLER_CERT_PATH /
    TELLER_KEY_PATH), but the UNTRUSTED tool-sandbox child must not receive them."""
    parent = {
        "TELLER_CERT_PATH": "/certs/teller.pem",
        "TELLER_KEY_PATH": "/certs/teller.key",
        "DATABASE_URL": "postgres://u:p@host:5432/db",
        "SENTRY_DSN": "https://key@o0.ingest.sentry.io/1",
        "PATH": "/usr/bin",
    }
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    for key in ("TELLER_CERT_PATH", "TELLER_KEY_PATH", "DATABASE_URL", "SENTRY_DSN"):
        assert key not in out
    assert out["PATH"] == "/usr/bin"


def test_tool_sandbox_env_preserves_operational_path_vars() -> None:
    """gate/#140 — the extra regex is tight by construction: a credential token
    must PRECEDE _PATH/_FILE/_DIR. PYTHONPATH / LD_LIBRARY_PATH / GOPATH / PATH
    have no such token, so they survive (breaking them would break every child)."""
    parent = {
        "PYTHONPATH": "/opt/lib",
        "LD_LIBRARY_PATH": "/usr/local/lib",
        "GOPATH": "/home/user/go",
        "PATH": "/usr/bin",
    }
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    for key, value in parent.items():
        assert out[key] == value


def test_tool_sandbox_env_provider_auth_wins_over_extra_secret_regex() -> None:
    """gate/#140 — the GEMINI_/GOOGLE_ whitelist is checked BEFORE the new regex,
    so provider auth the child legitimately needs survives even though
    GOOGLE_APPLICATION_CREDENTIALS is credential-shaped."""
    parent = {
        "GEMINI_API_KEY": "gm-key",
        "GOOGLE_API_KEY": "g-key",
        "GOOGLE_APPLICATION_CREDENTIALS": "/etc/gcp/sa.json",
    }
    out = get_scrubbed_tool_sandbox_env(parent_env=parent)
    assert out["GEMINI_API_KEY"] == "gm-key"
    assert out["GOOGLE_API_KEY"] == "g-key"
    assert out["GOOGLE_APPLICATION_CREDENTIALS"] == "/etc/gcp/sa.json"
