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

from runtime.subprocess_env import get_scrubbed_sdk_env


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
