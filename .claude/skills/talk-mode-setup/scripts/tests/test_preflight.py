"""Coverage for the talk-mode-setup preflight check.

Two behaviors are worth locking because both are easy to "simplify" into
something wrong:

1. Sidecar resolution asks the INSTALLED lifecycle which interpreter it
   spawns instead of assuming a venv layout, so it stays correct on both the
   OS-aware resolver and older Windows-only builds.
2. The billing check warns when a Codex subscription exists but an API key
   outranks it, which is the difference between riding a subscription and
   being silently metered.

Both are exercised against stubs, so the suite needs no network, no OpenAI
credential, and no built sidecar venv.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "preflight.py"
SPEC = importlib.util.spec_from_file_location("talk_mode_setup_preflight", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class _StubModules:
    """Install fake modules in sys.modules and restore them afterwards."""

    def __init__(self, test: unittest.TestCase) -> None:
        self._test = test

    def set(self, name: str, module) -> None:
        sentinel = object()
        previous = sys.modules.get(name, sentinel)

        def restore() -> None:
            if previous is sentinel:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

        self._test.addCleanup(restore)
        sys.modules[name] = module


class SidecarResolutionTests(unittest.TestCase):
    """`_check_sidecar` must trust the lifecycle, not a layout guess."""

    def setUp(self) -> None:
        self.stubs = _StubModules(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sidecar = Path(tmp.name) / "discord_voice"
        self.sidecar.mkdir(parents=True)

        original = MODULE.SIDECAR_DIR
        self.addCleanup(lambda: setattr(MODULE, "SIDECAR_DIR", original))
        MODULE.SIDECAR_DIR = self.sidecar

    def _make_venv(self, relative: str) -> Path:
        path = self.sidecar / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stub", encoding="utf-8")
        return path

    def _lifecycle_expects(self, relative: str | None) -> None:
        """Stub the lifecycle. ``None`` makes importing it fail."""
        if relative is None:
            self.stubs.set("discord_voice_lifecycle", None)  # import -> TypeError
            return
        module = types.ModuleType("discord_voice_lifecycle")
        module._sidecar_python = lambda: self.sidecar / relative
        self.stubs.set("discord_voice_lifecycle", module)

    def test_os_aware_resolver_with_posix_venv_is_ok(self) -> None:
        self._make_venv(".venv/bin/python")
        self._lifecycle_expects(".venv/bin/python")
        self.assertEqual(MODULE._check_sidecar()["status"], MODULE.OK)

    def test_windows_resolver_with_windows_venv_is_ok(self) -> None:
        self._make_venv(".venv/Scripts/python.exe")
        self._lifecycle_expects(".venv/Scripts/python.exe")
        self.assertEqual(MODULE._check_sidecar()["status"], MODULE.OK)

    def test_posix_venv_against_windows_only_resolver_blocks_and_names_cause(self) -> None:
        """The regression that made `/talk join` fail after a correct uv sync."""
        self._make_venv(".venv/bin/python")
        self._lifecycle_expects(".venv/Scripts/python.exe")
        result = MODULE._check_sidecar()
        self.assertEqual(result["status"], MODULE.BLOCK)
        # Must explain the mismatch, not tell the operator to re-run uv sync.
        self.assertIn("Scripts/python.exe", result["detail"])
        self.assertNotIn("uv sync", result["fix"])

    def test_no_venv_warns_and_says_uv_sync(self) -> None:
        self._lifecycle_expects(".venv/bin/python")
        result = MODULE._check_sidecar()
        self.assertEqual(result["status"], MODULE.WARN)
        self.assertIn("uv sync", result["fix"])

    def test_unimportable_lifecycle_falls_back_to_native_layout(self) -> None:
        native = ".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python"
        self._make_venv(native)
        self._lifecycle_expects(None)
        self.assertEqual(MODULE._check_sidecar()["status"], MODULE.OK)

    def test_missing_sidecar_package_warns_without_blocking_dashboard(self) -> None:
        MODULE.SIDECAR_DIR = self.sidecar / "does-not-exist"
        result = MODULE._check_sidecar()
        self.assertEqual(result["status"], MODULE.WARN)
        self.assertIn("dashboard voice unaffected", result["detail"])


class _AuthTestBase(unittest.TestCase):
    """Shared stubbing for the auth checks. Holds no tests of its own."""

    def setUp(self) -> None:
        self.stubs = _StubModules(self)
        # load_dotenv would let a real .env override the patched environment.
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *args, **kwargs: None
        self.stubs.set("dotenv", dotenv)

    def _stub_framework(self, *, source: str, codex_configured: bool, kill_switch: bool = False) -> None:
        talk_session = types.ModuleType("talk_session")
        talk_session.talk_status = lambda: {
            "configured": True,
            "source": source,
            "detail": f"stub {source}",
            "model": "gpt-realtime-2.1",
            "voice": "cedar",
            "killSwitchVoiceDisabled": kill_switch,
        }
        self.stubs.set("talk_session", talk_session)

        auth = types.ModuleType("runtime.openai_platform_auth")
        auth.openai_platform_auth_status = lambda **kwargs: {
            "configured": codex_configured,
            "source": "codex-oauth" if codex_configured else None,
            "detail": "stub codex",
        }
        runtime = types.ModuleType("runtime")
        runtime.openai_platform_auth = auth
        self.stubs.set("runtime", runtime)
        self.stubs.set("runtime.openai_platform_auth", auth)

    @staticmethod
    def _labels(checks: list[dict]) -> str:
        return " | ".join(check["label"] for check in checks)


class AuthBillingTests(_AuthTestBase):
    """`_check_auth` must surface a subscription that an API key outranks."""

    def test_api_key_winning_over_available_codex_warns_about_billing(self) -> None:
        self._stub_framework(source="env", codex_configured=True)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "stub-credential"}, clear=False):
            checks = MODULE._check_auth()
        self.assertIn("passed over", self._labels(checks))
        warning = next(c for c in checks if "passed over" in c["label"])
        self.assertEqual(warning["status"], MODULE.WARN)
        self.assertIn("OPENAI_API_KEY", warning["fix"])
        # The remedy is the voice-scoped flag, NOT deleting a key that other
        # subsystems still need. Guards against the pre-flag advice returning.
        self.assertIn("TALK_PREFER_CODEX_OAUTH", warning["fix"])
        self.assertNotIn("no prefer-Codex flag", warning["fix"])

    def test_codex_winning_produces_no_billing_warning(self) -> None:
        self._stub_framework(source="codex-oauth", codex_configured=True)
        env = {k: v for k, v in os.environ.items() if k not in {"OPENAI_API_KEY", "TALK_OPENAI_API_KEY"}}
        with mock.patch.dict(os.environ, env, clear=True):
            checks = MODULE._check_auth()
        self.assertNotIn("passed over", self._labels(checks))
        self.assertEqual(checks[0]["status"], MODULE.OK)

    def test_no_codex_available_produces_no_billing_warning(self) -> None:
        """An API key is the only option here, so nothing is being passed over."""
        self._stub_framework(source="env", codex_configured=False)
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "stub-credential"}, clear=False):
            checks = MODULE._check_auth()
        self.assertNotIn("passed over", self._labels(checks))

    def test_talk_scoped_key_is_named_when_it_is_the_winner(self) -> None:
        self._stub_framework(source="configured", codex_configured=True)
        with mock.patch.dict(os.environ, {"TALK_OPENAI_API_KEY": "stub-credential"}, clear=False):
            checks = MODULE._check_auth()
        warning = next(c for c in checks if "passed over" in c["label"])
        self.assertIn("TALK_OPENAI_API_KEY", warning["fix"])

    def test_voice_kill_switch_blocks(self) -> None:
        self._stub_framework(source="codex-oauth", codex_configured=True, kill_switch=True)
        checks = MODULE._check_auth()
        self.assertEqual(checks[0]["status"], MODULE.BLOCK)
        self.assertIn("HOMIE_KILLSWITCH_VOICE", checks[0]["fix"])

    def test_no_credential_blocks_and_recommends_codex_login(self) -> None:
        talk_session = types.ModuleType("talk_session")
        talk_session.talk_status = lambda: {
            "configured": False,
            "source": None,
            "detail": "no credential",
        }
        self.stubs.set("talk_session", talk_session)
        auth = types.ModuleType("runtime.openai_platform_auth")
        auth.openai_platform_auth_status = lambda **kwargs: {"configured": False}
        runtime = types.ModuleType("runtime")
        runtime.openai_platform_auth = auth
        self.stubs.set("runtime", runtime)
        self.stubs.set("runtime.openai_platform_auth", auth)

        checks = MODULE._check_auth()
        self.assertEqual(checks[0]["status"], MODULE.BLOCK)
        self.assertIn("codex login", checks[0]["fix"])


class PreferCodexFlagTests(_AuthTestBase):
    """`TALK_PREFER_CODEX_OAUTH` pins voice to the subscription.

    The flag changes what the preflight should SAY, so these lock the
    reporting; the resolver's own fail-closed guarantee is the framework's
    test, verified separately against the merged auth code.
    """

    def test_flag_on_is_reported_so_the_skill_can_skip_the_billing_question(self) -> None:
        self._stub_framework(source="codex-oauth", codex_configured=True)
        with mock.patch.dict(os.environ, {"TALK_PREFER_CODEX_OAUTH": "true"}, clear=False):
            checks = MODULE._check_auth()
        self.assertTrue(checks[0]["preferCodex"])
        self.assertNotIn("passed over", self._labels(checks))

    def test_flag_off_is_reported_false(self) -> None:
        self._stub_framework(source="codex-oauth", codex_configured=True)
        env = {k: v for k, v in os.environ.items() if k != "TALK_PREFER_CODEX_OAUTH"}
        with mock.patch.dict(os.environ, env, clear=True):
            checks = MODULE._check_auth()
        self.assertFalse(checks[0]["preferCodex"])

    def test_flag_on_with_key_set_still_raises_no_billing_warning(self) -> None:
        """The key is present but the directive means it can never be used."""
        self._stub_framework(source="codex-oauth", codex_configured=True)
        with mock.patch.dict(
            os.environ,
            {"TALK_PREFER_CODEX_OAUTH": "true", "OPENAI_API_KEY": "stub-credential"},
            clear=False,
        ):
            checks = MODULE._check_auth()
        self.assertNotIn("passed over", self._labels(checks))

    def test_unconfigured_under_flag_does_not_advise_setting_a_key(self) -> None:
        """Under the directive an API key is refused, so advising one misleads."""
        talk_session = types.ModuleType("talk_session")
        talk_session.talk_status = lambda: {
            "configured": False,
            "source": None,
            "detail": "pinned to Codex and no usable sign-in was found",
        }
        self.stubs.set("talk_session", talk_session)
        auth = types.ModuleType("runtime.openai_platform_auth")
        auth.openai_platform_auth_status = lambda **kwargs: {"configured": False}
        runtime = types.ModuleType("runtime")
        runtime.openai_platform_auth = auth
        self.stubs.set("runtime", runtime)
        self.stubs.set("runtime.openai_platform_auth", auth)

        with mock.patch.dict(os.environ, {"TALK_PREFER_CODEX_OAUTH": "true"}, clear=False):
            checks = MODULE._check_auth()

        self.assertEqual(checks[0]["status"], MODULE.BLOCK)
        self.assertIn("codex login", checks[0]["fix"])
        self.assertIn("TALK_PREFER_CODEX_OAUTH", checks[0]["fix"])
        self.assertNotIn("TALK_OPENAI_API_KEY", checks[0]["fix"])

    def test_flag_truthiness_matches_the_framework_parser(self) -> None:
        """Must accept the same spellings the framework accepts, and no others."""
        self._stub_framework(source="codex-oauth", codex_configured=True)
        for raw, expected in (
            ("true", True), ("1", True), ("yes", True), ("on", True),
            ("TRUE", True), ("  true  ", True),
            ("false", False), ("0", False), ("", False), ("maybe", False),
        ):
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {"TALK_PREFER_CODEX_OAUTH": raw}, clear=False):
                    checks = MODULE._check_auth()
                self.assertIs(checks[0]["preferCodex"], expected)


class SecretSafetyTests(unittest.TestCase):
    """Nothing the preflight emits may carry a credential."""

    def test_reported_fields_never_contain_a_key_value(self) -> None:
        stubs = _StubModules(self)
        dotenv = types.ModuleType("dotenv")
        dotenv.load_dotenv = lambda *args, **kwargs: None
        stubs.set("dotenv", dotenv)

        secret = "CREDENTIAL-SENTINEL-must-not-appear-in-output"
        talk_session = types.ModuleType("talk_session")
        talk_session.talk_status = lambda: {
            "configured": True,
            "source": "env",
            "detail": "OPENAI_API_KEY environment variable",
            "killSwitchVoiceDisabled": False,
        }
        stubs.set("talk_session", talk_session)
        auth = types.ModuleType("runtime.openai_platform_auth")
        auth.openai_platform_auth_status = lambda **kwargs: {"configured": True}
        runtime = types.ModuleType("runtime")
        runtime.openai_platform_auth = auth
        stubs.set("runtime", runtime)
        stubs.set("runtime.openai_platform_auth", auth)

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=False):
            checks = MODULE._check_auth()

        self.assertNotIn(secret, repr(checks))


if __name__ == "__main__":
    unittest.main()
