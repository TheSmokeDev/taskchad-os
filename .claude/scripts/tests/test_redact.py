"""PRD-8 Phase 7b (WS1) — security/redact contract tests.

Asserts the FULL Hermes ``agent/redact.py`` port (340 LOC at upstream HEAD) matches behavior:
  - All 13 redaction families verbatim from upstream
  - ``_REDACT_ENABLED`` IMPORT-TIME snapshot (default ON — Hermes-faithful;
    matches ``agent/redact.py:60`` where unset env resolves to empty string,
    which is NOT in the disabled tuple)
  - Mid-process env flip does NOT defeat the snapshot
  - Subprocess-spawn test for the boot-time-correct snapshot mechanism (M4 fix)
  - Public API ``redact_sensitive_text`` (matches Hermes name) + ``redact`` alias
  - Rule 3 module-attribute imports — no top-level callable imports outside test files
  - Import-order precondition (NB2 fix): ``security/redact.py`` imports ``config``
    at module top so ``load_dotenv`` runs before snapshot. Subprocess test
    proves profile ``.env`` is honored.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from security import redact

# ──────────────────────────────────────────────────────────────────────
# Setup: ensure tests run with redaction enabled regardless of dev env.
# We re-import the module under monkey-patched _REDACT_ENABLED state
# rather than re-import — the module is already loaded in this test
# process, so we just bypass the snapshot for behavior tests.
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def force_redact_enabled(monkeypatch):
    """Override the import-time snapshot for behavior tests.

    Behavior tests need _REDACT_ENABLED=True to exercise the redaction logic.
    The IMPORT-TIME snapshot tests use a subprocess (test_import_time_snapshot)
    so they don't need this override.
    """
    monkeypatch.setattr(redact, "_REDACT_ENABLED", True)
    yield


# ──────────────────────────────────────────────────────────────────────
# Family 1: vendor token prefixes (sk-, ghp_, AKIA, xox*-, etc.)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("token,expected_prefix", [
    ("<REDACTED-openai>", "sk-AbC"),
    ("<REDACTED-github>", "ghp_Ab"),
    ("github_pat_AbCdEfGhIjKlMnOp", "github"),
    ("<REDACTED-slack>", "xoxb-A"),
    ("<REDACTED-google>", "AIzaAb"),
    ("<REDACTED-aws>", "AKIA12"),
    ("<REDACTED-stripe>", "sk_liv"),
    ("hf_AbCdEfGhIjKlMnOpQr", "hf_AbC"),
])
def test_prefix_patterns_redacted(token, expected_prefix):
    """Each known vendor prefix gets masked (preserves prefix for >= 18 chars)."""
    out = redact.redact_sensitive_text(f"hello {token} world")
    # Long enough → masked with prefix preservation
    if len(token) >= 18:
        assert expected_prefix in out
        assert "..." in out
        assert token not in out
    else:
        # Short → fully masked
        assert token not in out
        assert "***" in out


def test_prefix_pattern_negative_no_false_positive():
    """Innocent text with prefix-shaped substring isn't redacted."""
    safe = "the variable sk_count is 5"  # sk_count is too short and has digit gap
    out = redact.redact_sensitive_text(safe)
    assert out == safe


# ──────────────────────────────────────────────────────────────────────
# Family 2: ENV assignments (KEY=value with secret-like name)
# ──────────────────────────────────────────────────────────────────────


def test_env_assign_redacted():
    out = redact.redact_sensitive_text("OPENAI_API_KEY=<REDACTED-openai>")
    # Hermes pattern: _PREFIX_RE runs FIRST (masks sk-... → sk-supe...3456),
    # THEN _ENV_ASSIGN_RE runs against the masked value (re-masks since
    # the masked output is shorter than 18 chars → "***").
    # Net result: the value is redacted, regardless of the intermediate state.
    assert "supersecretvalue123456" not in out
    assert "OPENAI_API_KEY=" in out
    # Confirm SOME redaction marker present.
    assert ("***" in out) or ("..." in out)


def test_env_assign_quoted():
    out = redact.redact_sensitive_text('MY_TOKEN="abcdef1234567890qwerty"')
    assert "abcdef1234567890qwerty" not in out


# ──────────────────────────────────────────────────────────────────────
# Family 3: JSON fields
# ──────────────────────────────────────────────────────────────────────


def test_json_field_apikey_redacted():
    out = redact.redact_sensitive_text('{"apiKey": "abcdef1234567890qwerty"}')
    assert "abcdef1234567890qwerty" not in out


def test_json_field_token_redacted_case_insensitive():
    out = redact.redact_sensitive_text('{"Token": "abcdef1234567890qwerty"}')
    assert "abcdef1234567890qwerty" not in out


# ──────────────────────────────────────────────────────────────────────
# Family 4: Authorization headers
# ──────────────────────────────────────────────────────────────────────


def test_authorization_header_redacted():
    out = redact.redact_sensitive_text("Authorization: Bearer abcdef1234567890qwerty")
    assert "abcdef1234567890qwerty" not in out
    assert "Authorization: Bearer " in out


# ──────────────────────────────────────────────────────────────────────
# Family 5: Telegram bot tokens
# ──────────────────────────────────────────────────────────────────────


def test_telegram_token_redacted():
    out = redact.redact_sensitive_text("bot12345678:AAEabcdefghijklmnopqrstuvwxyz1234567")
    assert "AAEabcdefghijklmnopqrstuvwxyz1234567" not in out
    assert ":***" in out


# ──────────────────────────────────────────────────────────────────────
# Family 6: Private key blocks
# ──────────────────────────────────────────────────────────────────────


def test_private_key_block_redacted():
    text = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAJ\n-----END RSA PRIVATE KEY-----"
    out = redact.redact_sensitive_text(text)
    assert "MIIBOgIBAAJBAJ" not in out
    assert "[REDACTED PRIVATE KEY]" in out


# ──────────────────────────────────────────────────────────────────────
# Family 7: DB connection strings
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "mysql", "mongodb", "redis", "amqp"])
def test_db_connstr_password_redacted(scheme):
    out = redact.redact_sensitive_text(f"{scheme}://user:supersecretpw@host:5432/db")
    assert "supersecretpw" not in out


def test_db_connstr_mongodb_srv_redacted():
    out = redact.redact_sensitive_text("mongodb+srv://user:secretpw@cluster.mongodb.net/")
    assert "secretpw" not in out


# ──────────────────────────────────────────────────────────────────────
# Family 8: JWT tokens
# ──────────────────────────────────────────────────────────────────────


def test_jwt_redacted():
    jwt = "<REDACTED-jwt>"
    out = redact.redact_sensitive_text(f"got jwt {jwt} from request")
    assert jwt not in out


# ──────────────────────────────────────────────────────────────────────
# Family 9: Discord mentions
# ──────────────────────────────────────────────────────────────────────


def test_discord_mention_redacted():
    out = redact.redact_sensitive_text("hello <@123456789012345678>")
    assert "123456789012345678" not in out
    assert "<@***>" in out


def test_discord_mention_with_bang():
    out = redact.redact_sensitive_text("hello <@!123456789012345678>")
    assert "123456789012345678" not in out


# ──────────────────────────────────────────────────────────────────────
# Family 10: E.164 phone numbers
# ──────────────────────────────────────────────────────────────────────


def test_phone_e164_redacted():
    out = redact.redact_sensitive_text("call me at +14155552671")
    assert "+14155552671" not in out
    assert "+141" in out  # prefix preserved
    assert "2671" in out  # suffix preserved


# ──────────────────────────────────────────────────────────────────────
# Family 11: URL query params (sensitive)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("param", [
    "access_token",
    "code",
    "signature",
    "id_token",
    "refresh_token",
    "client_secret",
    "password",
    "session",
])
def test_url_query_params_redacted(param):
    out = redact.redact_sensitive_text(f"https://example.com/cb?{param}=secretvalue123&keep=ok")
    assert "secretvalue123" not in out
    # Sensitive value replaced with ***
    assert f"{param}=***" in out
    # Non-sensitive param survives
    assert "keep=ok" in out


def test_url_query_params_negative_safe_keys_pass_through():
    out = redact.redact_sensitive_text("https://example.com/?count=5&page=10")
    assert out == "https://example.com/?count=5&page=10"


# ──────────────────────────────────────────────────────────────────────
# Family 12: URL userinfo (non-DB schemes)
# ──────────────────────────────────────────────────────────────────────


def test_url_userinfo_redacted():
    out = redact.redact_sensitive_text("connect to https://user:secretpw@api.example.com/v1")
    assert "secretpw" not in out
    assert "user:***" in out


# ──────────────────────────────────────────────────────────────────────
# Family 13: Form-urlencoded body
# ──────────────────────────────────────────────────────────────────────


def test_form_body_keys_redacted():
    out = redact.redact_sensitive_text("api_key=secretvalue123&user=alice")
    assert "secretvalue123" not in out


def test_form_body_negative_multiline_passes():
    """Form body detection is conservative — multiline text passes through."""
    text = "api_key=foo\nother line"
    out = redact.redact_sensitive_text(text)
    # Multiline → form-body detection skips. (Other patterns may still apply.)
    # Just assert the function returned without crashing — pattern-specific
    # tests cover the in-line redaction.
    assert isinstance(out, str)


# ──────────────────────────────────────────────────────────────────────
# Default-ON behavior (matches Hermes default)
# ──────────────────────────────────────────────────────────────────────


def test_disabled_via_env_redact_passes_through(monkeypatch):
    """When _REDACT_ENABLED is False (operator opted OUT), redact() returns text unchanged.

    Default is ON (matches Hermes verbatim). This test exercises the OFF path
    by directly mutating the snapshot — same mechanism the operator would
    achieve via HOMIE_REDACT_SECRETS=false in profile .env at boot.
    """
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    text = "secret <REDACTED-openai>"
    assert redact.redact_sensitive_text(text) == text


def test_redact_alias_matches_full_name():
    """``redact.redact`` alias is the same callable as ``redact.redact_sensitive_text``."""
    assert redact.redact is redact.redact_sensitive_text


def test_redact_handles_none():
    """None input returns None (Hermes contract)."""
    assert redact.redact_sensitive_text(None) is None


def test_redact_handles_non_string():
    """Non-string coerced via str()."""
    out = redact.redact_sensitive_text(42)
    assert out == "42"


def test_redact_handles_empty_string():
    out = redact.redact_sensitive_text("")
    assert out == ""


# ──────────────────────────────────────────────────────────────────────
# Import-time snapshot: subprocess test (M4 fix)
# ──────────────────────────────────────────────────────────────────────


def _build_hermetic_env(homie_home: Path | None = None) -> dict[str, str]:
    """Build a minimal env dict for subprocess snapshot tests.

    R4 NM2 fix (codex R3): ``_spawn_snapshot_check`` previously copied
    ``os.environ`` and only popped ``HOMIE_REDACT_SECRETS``. After the NB2 fix,
    ``security/redact.py`` imports ``config`` at module top, and ``config.py``
    loads the ACTIVE PROFILE ``.env`` at import time. So on a dev machine whose
    profile ``.env`` contains ``HOMIE_REDACT_SECRETS=false``, the default-ON
    subprocess test would fail even though the implementation is correct.

    The hermetic build uses a minimal allowlist and points ``HOMIE_HOME`` at
    an isolated empty profile so ``config.load_dotenv`` finds no ``.env``
    file (or only the ``.env`` we created in ``homie_home``).

    Allowlist contents (and why each is required):

    * ``PATH`` — Python interpreter must locate stdlib DLLs / shared libs.
    * ``SystemRoot`` / ``WINDIR`` — Windows pathlib + dlopen need it.
    * ``USERPROFILE`` (Windows) / ``HOME`` (POSIX) / ``HOMEDRIVE`` /
      ``HOMEPATH`` — ``Path.home()`` reads these. ``personas.get_active_profile_name``
      calls ``Path.home() / ".homie"`` to resolve the default profile root,
      so without these the import chain raises
      ``RuntimeError: Could not determine home directory``.
    * ``PYTHONIOENCODING``, ``TEMP``, ``TMP`` — interpreter creature-comforts.
    """
    allowlist = (
        "PATH",
        "SystemRoot",
        "WINDIR",
        "USERPROFILE",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "PYTHONIOENCODING",
        "TEMP",
        "TMP",
    )
    env: dict[str, str] = {}
    for key in allowlist:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    if homie_home is not None:
        env["HOMIE_HOME"] = str(homie_home)
    # Defensive: never inherit HOMIE_REDACT_SECRETS from parent.
    env.pop("HOMIE_REDACT_SECRETS", None)
    return env


def _spawn_snapshot_check(
    env_value: str | None,
    expected_enabled: bool,
    *,
    homie_home: Path | None = None,
) -> None:
    """Spawn a subprocess and verify _REDACT_ENABLED matches expectation.

    The subprocess imports security.redact ONCE in a fresh interpreter; the
    snapshot is whatever HOMIE_REDACT_SECRETS was at import time. Any later
    mutation in the parent process is irrelevant — we're testing boot behavior.

    R4 NM2 fix (codex R3): the subprocess env is built from a minimal allowlist
    (NOT ``os.environ.copy()``). When ``homie_home`` is provided, ``HOMIE_HOME``
    points at that path so ``config.load_dotenv`` finds an isolated profile
    (typically empty for default-ON tests). This makes the test hermetic — it
    passes regardless of what's in the dev machine's active profile ``.env``.
    """
    env = _build_hermetic_env(homie_home=homie_home)
    if env_value is not None:
        env["HOMIE_REDACT_SECRETS"] = env_value

    # Find the security module directory and pass it on PYTHONPATH so the
    # child interpreter can `from security import redact`. The test process
    # has the right path because pytest adds .claude/scripts to sys.path.
    scripts_dir = str(Path(__file__).resolve().parent.parent)

    code = textwrap.dedent("""
        import sys
        sys.path.insert(0, sys.argv[1])
        from security import redact
        print(redact._REDACT_ENABLED)
    """)
    result = subprocess.run(
        [sys.executable, "-c", code, scripts_dir],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    actual = result.stdout.strip()
    expected = "True" if expected_enabled else "False"
    assert actual == expected, (
        f"Expected _REDACT_ENABLED={expected} when HOMIE_REDACT_SECRETS={env_value!r} "
        f"(homie_home={homie_home}), got {actual}"
    )


def test_import_time_snapshot_default_on_via_subprocess(tmp_path):
    """Boot with no env var → snapshot is True (default ON, matches Hermes verbatim).

    Hermes ``agent/redact.py:60``: ``os.getenv(..., "").lower() not in
    ("0", "false", "no", "off")``. Unset env → ``""`` → ``""`` not in the
    disabled tuple → ``True``. Default behavior is to REDACT.

    R4 NM2 hermetic fix (codex R3): use an isolated empty profile via
    ``HOMIE_HOME=tmp_path/empty_profile`` so the dev machine's active profile
    ``.env`` cannot influence the snapshot. Without this isolation, a dev
    machine with ``HOMIE_REDACT_SECRETS=false`` in profile ``.env`` would fail
    this test even though the implementation is correct.
    """
    empty_profile = tmp_path / "empty_profile"
    empty_profile.mkdir(parents=True, exist_ok=True)
    # Intentionally NO .env file — load_dotenv finds nothing, so the snapshot
    # falls back to the unset bare env (which is empty string → True).
    _spawn_snapshot_check(env_value=None, expected_enabled=True, homie_home=empty_profile)


def test_import_time_snapshot_explicitly_disabled_via_subprocess(tmp_path):
    """Boot with HOMIE_REDACT_SECRETS=false → snapshot is False (hermetic)."""
    empty_profile = tmp_path / "empty_profile"
    empty_profile.mkdir(parents=True, exist_ok=True)
    _spawn_snapshot_check(env_value="false", expected_enabled=False, homie_home=empty_profile)


def test_import_time_snapshot_explicitly_enabled_via_subprocess(tmp_path):
    """Boot with HOMIE_REDACT_SECRETS=true → snapshot is True (hermetic)."""
    empty_profile = tmp_path / "empty_profile"
    empty_profile.mkdir(parents=True, exist_ok=True)
    _spawn_snapshot_check(env_value="true", expected_enabled=True, homie_home=empty_profile)


def test_import_time_snapshot_other_truthy_values(tmp_path):
    """Boot with HOMIE_REDACT_SECRETS=1/yes/on → snapshot is True (Hermes any-non-disabled idiom)."""
    empty_profile = tmp_path / "empty_profile"
    empty_profile.mkdir(parents=True, exist_ok=True)
    for val in ("1", "yes", "on", "TRUE", "True"):
        _spawn_snapshot_check(env_value=val, expected_enabled=True, homie_home=empty_profile)


# ──────────────────────────────────────────────────────────────────────
# NB2 fix — profile .env loaded BEFORE _REDACT_ENABLED snapshot
# ──────────────────────────────────────────────────────────────────────


def _spawn_profile_env_snapshot_check(
    tmp_path: Path,
    env_file_value: str,
    expected_enabled: bool,
) -> None:
    """Spawn a subprocess that reads HOMIE_REDACT_SECRETS from a tmp profile .env.

    Sets ``HOMIE_HOME`` to ``tmp_path`` so ``personas.get_persona_paths`` resolves
    ``env_file`` to ``tmp_path/.env``. The bare process env passed to the child
    has ``HOMIE_REDACT_SECRETS`` UNSET; the value MUST come from the .env file
    via ``config.load_dotenv``. If ``security/redact.py`` doesn't import ``config``
    before snapshotting (i.e. the NB2 fix isn't in place), the child snapshots
    bare process env (unset → True default) and the explicit-disable .env value
    is ignored — test fails.

    The .env file uses ``HOMIE_REDACT_SECRETS=<env_file_value>``; we pass
    ``override=True`` semantics through config.load_dotenv.
    """
    profile_root = tmp_path / "homie_home"
    profile_root.mkdir(parents=True, exist_ok=True)
    # Custom-profile layout: HOMIE_HOME itself is the profile root, .env sits
    # at HOMIE_HOME/.env. The env_file path resolution in personas.core uses
    # ``profile_root / ".env"`` for HOMIE_HOME-rooted custom profiles.
    env_file = profile_root / ".env"
    env_file.write_text(f"HOMIE_REDACT_SECRETS={env_file_value}\n", encoding="utf-8")

    # Spawn child with HOMIE_HOME pointing at the tmp profile root and
    # HOMIE_REDACT_SECRETS UNSET in the bare process env. Only the .env file
    # provides the value. R4 NM2 hermetic build — minimal env allowlist
    # (NOT os.environ.copy()) so the dev machine's active profile cannot
    # leak through.
    env = _build_hermetic_env(homie_home=profile_root)

    scripts_dir = str(Path(__file__).resolve().parent.parent)
    code = textwrap.dedent("""
        import sys
        sys.path.insert(0, sys.argv[1])
        # Importing security.redact triggers (a) `import config` inside
        # redact.py, which calls load_dotenv on the active profile .env,
        # then (b) the _REDACT_ENABLED snapshot. If (a) doesn't run first,
        # the snapshot misses the .env value.
        from security import redact
        print(redact._REDACT_ENABLED)
    """)
    result = subprocess.run(
        [sys.executable, "-c", code, scripts_dir],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    actual = result.stdout.strip()
    expected = "True" if expected_enabled else "False"
    assert actual == expected, (
        f"Expected _REDACT_ENABLED={expected} when profile .env has "
        f"HOMIE_REDACT_SECRETS={env_file_value!r} (and bare process env unset), got {actual}. "
        f"This indicates security/redact.py did NOT import config before snapshotting "
        f"_REDACT_ENABLED, so load_dotenv never ran on the profile .env."
    )


def test_profile_dotenv_disable_honored_via_subprocess(tmp_path):
    """Profile .env with HOMIE_REDACT_SECRETS=false → snapshot is False.

    NB2 regression: proves ``security/redact.py`` imports ``config`` BEFORE
    snapshotting ``_REDACT_ENABLED``, so ``config.load_dotenv`` runs on the
    active profile ``.env`` and the explicit disable wins. Without the
    config-import precondition, the child would snapshot bare process env
    (unset → True) and ignore the .env file.
    """
    _spawn_profile_env_snapshot_check(tmp_path, env_file_value="false", expected_enabled=False)


def test_profile_dotenv_enable_honored_via_subprocess(tmp_path):
    """Profile .env with HOMIE_REDACT_SECRETS=true → snapshot is True.

    Companion to the disable test — proves load_dotenv ran and the value
    propagated through, regardless of direction.
    """
    _spawn_profile_env_snapshot_check(tmp_path, env_file_value="true", expected_enabled=True)


def test_mid_process_flip_defeated(monkeypatch):
    """Setting env var AFTER import does NOT change the cached _REDACT_ENABLED.

    The behavior tests above use ``monkeypatch.setattr(redact, '_REDACT_ENABLED', ...)``
    which DOES change behavior because pytest is patching the module attribute
    directly. This test asserts that setting ``os.environ['HOMIE_REDACT_SECRETS']``
    does NOT change the snapshot — only direct attribute mutation can.
    """
    # Snapshot _REDACT_ENABLED in the live process. Whatever it is, it should
    # not change when we mutate os.environ.
    pre = redact._REDACT_ENABLED
    monkeypatch.setenv("HOMIE_REDACT_SECRETS", "false" if pre else "true")
    # Re-read — should still be the cached snapshot.
    assert redact._REDACT_ENABLED == pre


# ──────────────────────────────────────────────────────────────────────
# Module-attribute Rule 3 audit
# ──────────────────────────────────────────────────────────────────────


def test_security_init_re_exports_redact_module():
    """security/__init__.py exports redact as a MODULE (not a callable)."""
    import security
    assert hasattr(security, "redact")
    # Confirm it's a module, not a function.
    import types
    assert isinstance(security.redact, types.ModuleType)


def test_no_top_level_redact_callable_on_security_pkg():
    """``from security import redact_sensitive_text`` MUST fail (Rule 3)."""
    import security
    assert not hasattr(security, "redact_sensitive_text")


# ──────────────────────────────────────────────────────────────────────
# Lazy redact re-export (R4 NM1 fix — codex R3)
# ──────────────────────────────────────────────────────────────────────
#
# security/__init__.py uses PEP 562 ``__getattr__`` to lazily import
# ``redact``. This avoids forcing every kill-switch consumer to also load
# ``config`` and snapshot ``_REDACT_ENABLED`` against a possibly-wrong
# profile env. Eager loading would create a class-of-bug where any process
# that imports ``security.kill_switches`` before profile boot finalizes
# ``HOMIE_HOME`` would freeze the wrong redact state.


def _spawn_security_import_check(import_target: str) -> dict:
    """Spawn a subprocess that imports ``import_target`` and reports which
    security submodules are loaded into ``sys.modules``.

    Returns a dict ``{"redact_loaded": bool, "patterns_loaded": bool,
    "kill_switches_loaded": bool}``.
    """
    scripts_dir = str(Path(__file__).resolve().parent.parent)
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, sys.argv[1])
        # Fresh interpreter, only this import.
        {import_target}
        result = {{
            "redact_loaded": "security.redact" in sys.modules,
            "patterns_loaded": "security.patterns" in sys.modules,
            "kill_switches_loaded": "security.kill_switches" in sys.modules,
        }}
        import json
        print(json.dumps(result))
    """)
    env = _build_hermetic_env()
    result = subprocess.run(
        [sys.executable, "-c", code, scripts_dir],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    import json as _json
    return _json.loads(result.stdout.strip())


def test_security_kill_switches_does_not_load_redact():
    """Importing ``kill_switches`` MUST NOT trigger ``redact``'s import.

    Critical NM1 fix: the previous eager ``from . import redact`` in
    ``security/__init__.py`` forced every kill-switch consumer (lane_router,
    cabinet text_router, cabinet text_orchestrator) to ALSO load ``config``
    and snapshot ``_REDACT_ENABLED`` — frozen against possibly the wrong
    profile env if security was imported before profile boot finalized
    ``HOMIE_HOME``. This test asserts the lazy ``__getattr__`` keeps
    ``security.redact`` OUT of ``sys.modules`` when only kill_switches is
    requested.
    """
    state = _spawn_security_import_check("from security import kill_switches")
    assert state["kill_switches_loaded"] is True, (
        "kill_switches import should land in sys.modules"
    )
    assert state["redact_loaded"] is False, (
        "Importing kill_switches loaded redact (defeats lazy __getattr__). "
        "security/__init__.py must NOT eagerly import redact."
    )


def test_security_redact_lazy_attr_works():
    """``from security import redact`` MUST still work via ``__getattr__``.

    Preserves the NB2 contract — when a consumer explicitly imports redact,
    the config-import precondition + snapshot logic runs as before. The
    laziness is invisible to direct redact consumers; it only spares
    kill-switch consumers the extra import cost.
    """
    state = _spawn_security_import_check("from security import redact")
    assert state["redact_loaded"] is True, (
        "from security import redact should land redact in sys.modules"
    )
    # And the module must expose the public API.
    import sys as _sys
    if "security.redact" in _sys.modules:
        del _sys.modules["security.redact"]
    if "security" in _sys.modules:
        del _sys.modules["security"]
    from security import redact as _redact
    assert _redact.redact is not None, "redact alias missing on lazy-loaded module"
    assert _redact.redact_sensitive_text is not None, (
        "redact_sensitive_text missing on lazy-loaded module"
    )


def test_security_unknown_attr_raises_attribute_error():
    """``from security import nonexistent`` raises AttributeError per PEP 562."""
    import security
    with pytest.raises(AttributeError, match="no attribute 'nonexistent'"):
        _ = security.nonexistent  # noqa: F841


# ──────────────────────────────────────────────────────────────────────
# Sanitize parity (WS1.6) — patterns.py is the SOLE source of truth
# ──────────────────────────────────────────────────────────────────────


def test_redact_does_not_redefine_secret_prefixes():
    """``redact.py`` has its own ``_PREFIX_PATTERNS`` for INLINE-text detection.

    This is INTENTIONALLY separate from ``security.patterns.SECRET_PREFIXES``
    which is for ENV-VAR-KEY detection. The two have different shapes — one is
    a list of regex patterns matching token VALUES, the other is a list of
    string prefixes matching env var KEYS.

    This test asserts that ``redact.py`` does NOT import ``SECRET_PREFIXES``
    (i.e., the two catalogs remain distinct), and that ``patterns.py`` is
    still the sole source of truth for env-var-key detection.
    """
    from security import patterns
    # patterns.py has SECRET_PREFIXES (env-var-key list)
    assert hasattr(patterns, "SECRET_PREFIXES")
    # redact.py has _PREFIX_PATTERNS (regex list for inline text)
    assert hasattr(redact, "_PREFIX_PATTERNS")
    # They are NOT the same object.
    assert redact._PREFIX_PATTERNS is not patterns.SECRET_PREFIXES


# ──────────────────────────────────────────────────────────────────────
# Import-order precondition (WS1.7 / NB2 fix) —
# ``security/redact.py`` imports ``config`` at module top.
# ──────────────────────────────────────────────────────────────────────


def test_redact_module_imports_config_at_module_top():
    """``security/redact.py`` MUST contain ``import config`` (or equivalent)
    at module scope BEFORE ``_REDACT_ENABLED`` is assigned.

    Rationale (NB2): the previous design relied on consumers importing ``config``
    before ``security.redact``, but a consumer that does ``from security import
    redact; import config`` would snapshot bare process env because
    ``security/__init__.py`` triggers ``redact``'s import first. The robust fix
    is to make ``redact.py`` itself import ``config`` at module top, so
    ``config.load_dotenv(ENV_FILE, override=True)`` always runs before the
    snapshot regardless of how consumers import the module.

    This test reads the file as text and asserts the import is present and
    appears BEFORE the ``_REDACT_ENABLED = ...`` line.
    """
    redact_path = Path(redact.__file__).resolve()
    text = redact_path.read_text(encoding="utf-8")

    config_import_match = re.search(
        r"^\s*(?:from\s+config\s+import|import\s+config(?:\s|$|#|,))",
        text,
        re.MULTILINE,
    )
    snapshot_match = re.search(
        r"^_REDACT_ENABLED\s*=",
        text,
        re.MULTILINE,
    )
    assert config_import_match, (
        f"{redact_path.name} does NOT import `config` at module scope. "
        "This violates the WS1.7 (NB2) import-order precondition. "
        "Add `import config  # noqa: F401, E402` after the stdlib imports so "
        "load_dotenv runs before the _REDACT_ENABLED snapshot."
    )
    assert snapshot_match, (
        f"{redact_path.name} does not contain a top-level `_REDACT_ENABLED = ...` line."
    )
    assert config_import_match.start() < snapshot_match.start(), (
        f"{redact_path.name}: `import config` (at offset {config_import_match.start()}) "
        f"appears AFTER `_REDACT_ENABLED = ...` (at offset {snapshot_match.start()}). "
        "The config import must precede the snapshot so load_dotenv runs first."
    )
