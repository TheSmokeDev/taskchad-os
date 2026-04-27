"""Tests for the capability/toolset registry — PRP-1a.

Covers:
- Behavior contracts (4): aggregator, composition, cycle, dedup
- Error paths (2): silent-on-missing, graceful-on-import-failure
- Diamond composition (1)
- Auto-discovery + regression + status (3)
- Langfuse span matrix (5): the 4-class pattern from
  test_team_observability_matrix.py + a fail-open import-error case
- R2 Minor 3 (1): single-extension intent dedup is silent (documented behavior)

Total: 16 tests.

PRP reference: PRPs/active/PRP-framework-capability-toolsets-1a.md
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# conftest.py already inserts both .claude/scripts and .claude/chat onto
# sys.path, but be defensive — some CI runners load test files in isolation.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_CHAT_DIR = _SCRIPTS_DIR.parent / "chat"
if str(_CHAT_DIR) not in sys.path:
    sys.path.insert(0, str(_CHAT_DIR))


# ---------------------------------------------------------------------------
# Helpers — mock ExtensionMeta / CommandSpec / IntentSpec without importing
# the full extension_manager dependency chain (config.py, etc.) up front.
# ---------------------------------------------------------------------------


def _make_command(name: str, description: str = ""):
    """Build a minimal CommandSpec-shaped object for the aggregator."""
    cmd = MagicMock()
    cmd.name = name
    cmd.description = description
    return cmd


def _make_intent(command: str):
    """Build a minimal IntentSpec-shaped object for the aggregator."""
    intent = MagicMock()
    intent.command = command
    return intent


def _make_meta(
    ext_id: str,
    *,
    enabled: bool = True,
    status: str = "loaded",
    commands: list | None = None,
    intents: list | None = None,
):
    """Build a minimal ExtensionMeta-shaped object for the aggregator."""
    meta = MagicMock()
    meta.id = ext_id
    meta.enabled = enabled
    meta.status = status
    meta.commands = commands or []
    meta.intents = intents or []
    return meta


def _patch_manager(metas):
    """Return a context manager that patches ``get_manager`` to return a
    manager whose ``get_all_extensions()`` yields the given metas.

    The aggregator imports ``get_manager`` from ``extension_manager`` inside
    the function body, so patching the attribute on the imported module is
    sufficient.
    """
    fake_manager = MagicMock()
    fake_manager.get_all_extensions.return_value = list(metas)
    return patch("extension_manager.get_manager", return_value=fake_manager)


def _fake_disabled(key, default=None):
    """Patch helper for ``runtime.langfuse_setup.os.getenv`` — flips the
    Langfuse-enabled flag to false at the source module."""
    if key in ("LANGFUSE_ENABLED", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        return {"LANGFUSE_ENABLED": "false"}.get(key, "")
    import os
    return os.environ.get(key, default)


def _make_mock_langfuse_module():
    """Build the mock Langfuse module / client / context manager triple used
    by the span matrix tests. Mirrors the helper in
    test_team_observability_matrix.py for parity."""
    ctx = MagicMock()
    client = MagicMock()
    client.start_as_current_observation.return_value = ctx
    client.get_current_trace_id.return_value = "caps-trace-001"
    client.get_current_observation_id.return_value = "caps-obs-001"
    fake_mod = MagicMock()
    fake_mod.get_client.return_value = client
    return fake_mod, client, ctx


# ---------------------------------------------------------------------------
# Behavior contracts
# ---------------------------------------------------------------------------


class TestListCapabilitiesReadsChatExtensions:
    def test_list_capabilities_reads_chat_extensions(self):
        """Aggregator iterates all extensions and emits one Capability per
        CommandSpec and IntentSpec, with the namespaced intent id format
        ``chat.intent.<extension_id>.<command>`` (R1 B4).

        Fixture: extension ``alpha`` (enabled) with 2 commands + 1 intent;
        extension ``beta`` (disabled) with 1 command. Total 4 capabilities.
        """
        from runtime.capabilities import list_capabilities

        alpha = _make_meta(
            "alpha",
            enabled=True,
            commands=[_make_command("budget", "Budget snapshot"),
                      _make_command("ping")],
            intents=[_make_intent("budget")],
        )
        beta = _make_meta(
            "beta",
            enabled=False,
            commands=[_make_command("legacy_help")],
        )

        with _patch_manager([alpha, beta]):
            caps = list_capabilities()

        assert len(caps) == 4

        ids = {c.id for c in caps}
        assert "chat.command.budget" in ids
        assert "chat.command.ping" in ids
        assert "chat.command.legacy_help" in ids
        # B4: intent id is namespaced by the OWNING extension's id, not the
        # bare command name.
        assert "chat.intent.alpha.budget" in ids

        # Enabled flag passes through from owning extension's enabled flag.
        for c in caps:
            if c.extension_id == "alpha":
                assert c.enabled is True
            elif c.extension_id == "beta":
                assert c.enabled is False


class TestResolveToolsetFlattensComposition:
    def test_resolve_toolset_flattens_composition(self):
        """``includes`` recursion produces a sorted union across base and
        extending toolsets."""
        from runtime.capabilities import resolve_toolset

        registry = {
            "base": {"description": "", "tools": ["a", "b"], "includes": []},
            "extended": {
                "description": "",
                "tools": ["c"],
                "includes": ["base"],
            },
        }
        assert resolve_toolset("extended", registry=registry) == ["a", "b", "c"]


class TestResolveToolsetSilentOnCycle:
    def test_resolve_toolset_silent_on_cycle(self):
        """Hermes-faithful: cycles return ``[]`` silently — they are not
        bugs, they are an inevitable consequence of allowing diamonds."""
        from runtime.capabilities import resolve_toolset

        registry = {
            "alpha": {"description": "", "tools": [], "includes": ["beta"]},
            "beta": {"description": "", "tools": [], "includes": ["alpha"]},
        }
        result = resolve_toolset("alpha", registry=registry)
        assert result == []


class TestResolveToolsetDedupViaSet:
    def test_resolve_toolset_dedup_via_set(self):
        """Set-based dedup: shared ids across multiple includes appear once
        in the sorted output."""
        from runtime.capabilities import resolve_toolset

        registry = {
            "left": {"description": "", "tools": ["x", "y"], "includes": []},
            "right": {"description": "", "tools": ["y", "z"], "includes": []},
            "top": {
                "description": "",
                "tools": ["x"],
                "includes": ["left", "right"],
            },
        }
        assert resolve_toolset("top", registry=registry) == ["x", "y", "z"]


# ---------------------------------------------------------------------------
# Error path tests
# ---------------------------------------------------------------------------


class TestResolveToolsetSilentOnMissing:
    def test_resolve_toolset_silent_on_missing(self):
        """Hermes-faithful: missing toolset returns ``[]`` silently — matches
        the "optional plugin not loaded" pattern."""
        from runtime.capabilities import resolve_toolset

        result = resolve_toolset("nonexistent", registry={})
        assert result == []


class TestListCapabilitiesGracefulOnImportFailure:
    def test_list_capabilities_graceful_on_import_failure(self):
        """If ``extension_manager`` cannot be imported, the aggregator
        returns ``[]`` rather than raising — the runtime contract is
        preserved when the chat slice is unavailable."""
        from runtime.capabilities import list_capabilities

        # Patch sys.modules to make the late import inside
        # _aggregate_chat_extensions raise ImportError.
        with patch.dict("sys.modules", {"extension_manager": None}):
            caps = list_capabilities()
        assert caps == []


# ---------------------------------------------------------------------------
# Diamond composition
# ---------------------------------------------------------------------------


class TestResolveToolsetAllowsDiamondComposition:
    def test_resolve_toolset_allows_diamond_composition(self):
        """Diamond composition is legal: the shared subtree is walked
        exactly once across siblings via the shared visited set."""
        from runtime.capabilities import resolve_toolset

        registry = {
            "shared": {"description": "", "tools": ["s"], "includes": []},
            "left": {"description": "", "tools": [], "includes": ["shared"]},
            "right": {"description": "", "tools": [], "includes": ["shared"]},
            "top": {
                "description": "",
                "tools": [],
                "includes": ["left", "right"],
            },
        }
        assert resolve_toolset("top", registry=registry) == ["s"]


# ---------------------------------------------------------------------------
# Auto-discovery + regression + status passthrough
# ---------------------------------------------------------------------------


class TestResolveToolsetLiveSourceFilters:
    def test_resolve_toolset_live_source_filters(self):
        """Auto-discovery contract: a toolset declaring ``live_source`` calls
        ``list_capabilities(sources=[live_source])`` and includes only
        capability ids whose prefix matches ``live_filter``."""
        from runtime import capabilities as caps_mod
        from runtime.capabilities import Capability, resolve_toolset

        registry = {
            "foo": {
                "description": "",
                "tools": [],
                "includes": [],
                "live_source": "chat_extensions",
                "live_filter": "chat.command.",
            },
        }

        fake_caps = [
            Capability(id="chat.command.x", display_name="x",
                       enabled=True, source="chat_extension"),
            Capability(id="chat.intent.alpha.y", display_name="y",
                       enabled=True, source="chat_extension"),
            Capability(id="chat.command.z", display_name="z",
                       enabled=True, source="chat_extension"),
        ]

        # Patch the dispatch dict to inject a fake aggregator. The patcher
        # restores the original aggregator after the test.
        original = dict(caps_mod._AGGREGATORS)
        try:
            caps_mod._AGGREGATORS["chat_extensions"] = lambda: fake_caps
            result = resolve_toolset("foo", registry=registry)
        finally:
            caps_mod._AGGREGATORS.clear()
            caps_mod._AGGREGATORS.update(original)

        # Only chat.command.* ids — the chat.intent. id is filtered out.
        assert result == ["chat.command.x", "chat.command.z"]


class TestLegacyConstantsRemainImportable:
    def test_legacy_constants_remain_importable(self):
        """B5: the 3 pre-existing string constants are part of the public
        API — 16+ files in the codebase import them. A bad refactor would
        silently break the runtime."""
        from runtime.capabilities import (
            TEXT_REASONING,
            TOOL_REASONING,
            VOICE_AUXILIARY,
        )

        assert TEXT_REASONING == "text_reasoning"
        assert TOOL_REASONING == "tool_reasoning"
        assert VOICE_AUXILIARY == "voice_auxiliary"


class TestAggregatorUsesEnabledNotStatus:
    def test_aggregator_uses_enabled_not_status(self):
        """M5: the aggregator reads ``meta.enabled`` only. ``meta.status``
        (e.g. ``missing_env``) is intentionally not consulted in PRP-1a;
        status passthrough is a follow-up. Locking the contract here
        prevents a silent regression that would over-collapse capabilities
        when status reflects a degraded but enabled state."""
        from runtime.capabilities import list_capabilities

        meta = _make_meta(
            "gamma",
            enabled=True,
            status="missing_env",
            commands=[_make_command("ping")],
        )
        with _patch_manager([meta]):
            caps = list_capabilities()

        assert len(caps) == 1
        # enabled is True even though status is degraded — contract locked.
        assert caps[0].enabled is True


# ---------------------------------------------------------------------------
# R2 Minor 3 — single-extension intent collision is silent
# ---------------------------------------------------------------------------


class TestSingleExtensionIntentDedup:
    def test_single_extension_intent_dedup_is_silent(self):
        """R2 Minor 3: B4 namespacing prevents collisions BETWEEN extensions
        but not WITHIN one. If a single extension registers two
        ``IntentSpec`` rows pointing to the same router command (different
        keyword sets), both produce the same id ``chat.intent.<ext>.<cmd>``
        and the second is silently dropped via the implicit set dedup that
        will happen when downstream consumers (toolset resolver) collect
        ids into a set.

        Acceptable behavior in PRP-1a — extensions don't currently do this.
        Documented here so a future regression is caught: the aggregator
        does NOT inject extra disambiguation, AND the resolver-level set
        dedup IS what eats duplicates downstream.
        """
        from runtime.capabilities import list_capabilities, resolve_toolset

        # Same extension, same intent.command, different IntentSpec rows —
        # represents an extension declaring two intents both routing to
        # /lookup with different keyword sets.
        meta = _make_meta(
            "foo",
            enabled=True,
            intents=[_make_intent("lookup"), _make_intent("lookup")],
        )

        with _patch_manager([meta]):
            caps = list_capabilities()

        # Aggregator emits BOTH rows (no extra dedup at the dataclass layer).
        assert len(caps) == 2
        assert caps[0].id == caps[1].id == "chat.intent.foo.lookup"

        # The resolver collapses them to one when collecting via a set —
        # this is the documented dedup behavior.
        registry = {
            "foo_intents": {
                "description": "",
                "tools": [],
                "includes": [],
                "live_source": "chat_extensions",
                "live_filter": "chat.intent.",
            },
        }
        with _patch_manager([meta]):
            ids = resolve_toolset("foo_intents", registry=registry)
        assert ids == ["chat.intent.foo.lookup"]


# ---------------------------------------------------------------------------
# Langfuse span matrix — 5 classes mirroring test_team_observability_matrix.py
# ---------------------------------------------------------------------------


class TestEnabledHappyPath:
    def test_span_emitted_on_list_capabilities(self):
        """M8: span fixture covers a MIX of enabled and disabled extensions.
        Three enabled extensions with 5 caps total + one disabled with 2
        caps = total 7, enabled_count 5. The span metadata records both
        fields and ``enabled_count < total``."""
        fake_mod, client, _ = _make_mock_langfuse_module()

        enabled1 = _make_meta(
            "e1", enabled=True,
            commands=[_make_command("a"), _make_command("b")],
        )
        enabled2 = _make_meta(
            "e2", enabled=True,
            commands=[_make_command("c")],
            intents=[_make_intent("c")],
        )
        enabled3 = _make_meta(
            "e3", enabled=True,
            commands=[_make_command("d")],
        )
        disabled = _make_meta(
            "d1", enabled=False,
            commands=[_make_command("e"), _make_command("f")],
        )

        with (
            patch("runtime.langfuse_setup.is_langfuse_enabled",
                  return_value=True),
            patch("orchestration.observability.init_langfuse"),
            patch.dict("sys.modules", {"langfuse": fake_mod}),
            _patch_manager([enabled1, enabled2, enabled3, disabled]),
        ):
            from runtime.capabilities import list_capabilities
            caps = list_capabilities()

        # 2 + 1 + 1 (commands) + 1 (intent) + 2 (disabled commands) = 7 total
        assert len(caps) == 7
        enabled_count = sum(1 for c in caps if c.enabled)
        assert enabled_count == 5
        assert enabled_count < len(caps)

        # The span was opened and update_observation was called with both
        # the total and enabled_count fields.
        assert client.start_as_current_observation.called

        # Pull the metadata kwargs from update_current_span calls. The
        # observability layer routes update_observation through
        # update_current_span; the second call carries the post-aggregate
        # metadata (total + enabled_count + sources_resolved).
        assert client.update_current_span.called
        metadata_payloads = [
            kwargs.get("metadata")
            for _args, kwargs in client.update_current_span.call_args_list
            if kwargs.get("metadata")
        ]
        # At least one payload must report the total + enabled_count pair.
        matched = [
            md for md in metadata_payloads
            if isinstance(md, dict) and md.get("total") == 7
            and md.get("enabled_count") == 5
        ]
        assert matched, (
            f"Expected metadata with total=7 and enabled_count=5, "
            f"got: {metadata_payloads!r}"
        )


class TestEnabledExpectedException:
    def test_span_survives_aggregate_error(self, monkeypatch):
        """If an aggregator raises, ``orchestration_span`` re-raises after
        capturing trace state. ``list_capabilities`` does not swallow
        aggregator exceptions on the enabled-Langfuse path, but the span
        state is still populated. We confirm that behavior here by patching
        the chat aggregator to raise and checking that:

        - the orchestration span IS entered (start_as_current_observation called)
        - the span context manager's lifecycle runs through __exit__ with the
          exception (proves observation lifecycle completes)
        - the Sentry capture path is intercepted by a no-op patch (proves test
          telemetry doesn't leak to a real Sentry backend when SENTRY_DSN is set)
        """
        from runtime import capabilities as caps_mod

        fake_mod, client, ctx = _make_mock_langfuse_module()
        original = dict(caps_mod._AGGREGATORS)

        # No-op patch for Sentry capture. Proves test isolation actually
        # intercepts the leak that Codex flagged. We track invocations to
        # assert the patch fired (or did not fire — we want NOT fired here
        # because the orchestration_span treats the aggregator error as an
        # unexpected exception and would call _capture_sentry_exception,
        # but our patch ensures it's a no-op).
        sentry_calls: list[BaseException] = []

        def _noop_capture(exc, *, span_name, metadata=None):
            sentry_calls.append(exc)
            return None

        monkeypatch.setattr(
            "orchestration.observability._capture_sentry_exception",
            _noop_capture,
        )

        def _raises():
            raise RuntimeError("aggregator boom")

        try:
            caps_mod._AGGREGATORS["chat_extensions"] = _raises
            with (
                patch("runtime.langfuse_setup.is_langfuse_enabled",
                      return_value=True),
                patch("orchestration.observability.init_langfuse"),
                patch.dict("sys.modules", {"langfuse": fake_mod}),
            ):
                with pytest.raises(RuntimeError, match="aggregator boom"):
                    caps_mod.list_capabilities()
        finally:
            caps_mod._AGGREGATORS.clear()
            caps_mod._AGGREGATORS.update(original)

        # Span IS entered — start_as_current_observation was called with the
        # capabilities_resolved name.
        assert client.start_as_current_observation.called
        observation_call = client.start_as_current_observation.call_args
        assert observation_call.kwargs.get("name") == "capabilities_resolved"

        # The span context manager completed its lifecycle. The mock's
        # __exit__ is invoked with the exception info because the with-block
        # in orchestration_span re-raises. Assert __exit__ was called with
        # a non-None exception type to prove the lifecycle ran.
        assert ctx.__exit__.called
        exit_call = ctx.__exit__.call_args
        # __exit__ is called with (exc_type, exc_value, traceback). Either
        # positional or kwargs depending on python's contextlib internals.
        exit_args = exit_call.args if exit_call.args else (
            exit_call.kwargs.get("exc_type"),
            exit_call.kwargs.get("exc_value"),
            exit_call.kwargs.get("traceback"),
        )
        assert exit_args[0] is RuntimeError, (
            f"Expected RuntimeError as exc_type at __exit__, got {exit_args[0]!r}"
        )

        # Sentry no-op patch was triggered — proves the orchestration_span
        # treated this as an "unexpected exception" path and routed through
        # the Sentry capture helper. Without the patch, this would have hit
        # the real Sentry backend if SENTRY_DSN is configured.
        assert len(sentry_calls) == 1
        assert isinstance(sentry_calls[0], RuntimeError)
        assert "aggregator boom" in str(sentry_calls[0])


class TestDisabledHappyPath:
    def test_no_span_when_langfuse_disabled(self):
        """When Langfuse is disabled, ``list_capabilities`` still returns
        the aggregated rows. The span helper yields a state dict with
        ``trace_id=None`` (no Langfuse client touched)."""
        meta = _make_meta(
            "alpha", enabled=True,
            commands=[_make_command("ping")],
        )
        with (
            patch("runtime.langfuse_setup.os.getenv",
                  side_effect=_fake_disabled),
            _patch_manager([meta]),
        ):
            from runtime.capabilities import list_capabilities
            caps = list_capabilities()
        assert len(caps) == 1
        assert caps[0].id == "chat.command.ping"


class TestDisabledExpectedException:
    def test_no_span_on_error_when_disabled(self, monkeypatch):
        """With Langfuse disabled, aggregator exceptions still propagate
        (no swallowing in the no-Langfuse path either).

        Even on the disabled-Langfuse path, ``orchestration_span`` calls
        ``_capture_sentry_exception`` for unexpected exceptions. Patch it to
        a no-op so this test doesn't leak telemetry to a real Sentry backend
        when ``SENTRY_DSN`` is set in the environment. Assert the no-op
        patch fired so we know the test isolation actually intercepted.
        """
        from runtime import capabilities as caps_mod

        original = dict(caps_mod._AGGREGATORS)

        sentry_calls: list[BaseException] = []

        def _noop_capture(exc, *, span_name, metadata=None):
            sentry_calls.append(exc)
            return None

        monkeypatch.setattr(
            "orchestration.observability._capture_sentry_exception",
            _noop_capture,
        )

        def _raises():
            raise ValueError("expected")

        try:
            caps_mod._AGGREGATORS["chat_extensions"] = _raises
            with patch("runtime.langfuse_setup.os.getenv",
                       side_effect=_fake_disabled):
                with pytest.raises(ValueError, match="expected"):
                    caps_mod.list_capabilities()
        finally:
            caps_mod._AGGREGATORS.clear()
            caps_mod._AGGREGATORS.update(original)

        # Sentry no-op patch was triggered — proves the disabled-Langfuse
        # path also routes through the Sentry capture helper for unexpected
        # aggregator exceptions, and that our isolation kept the call
        # in-process.
        assert len(sentry_calls) == 1
        assert isinstance(sentry_calls[0], ValueError)
        assert "expected" in str(sentry_calls[0])


class TestImportFailure:
    def test_span_helper_import_error_does_not_crash(self):
        """B3 fix: when ``orchestration.observability`` cannot be imported,
        ``list_capabilities`` falls through to ``_list_capabilities_no_span``
        and returns the aggregated capabilities (NOT ``[]``). Proves the
        documented fail-open path."""
        meta = _make_meta(
            "alpha", enabled=True,
            commands=[_make_command("ping"), _make_command("status")],
        )

        # Force the late import inside list_capabilities to raise. We can't
        # rely on patch.dict to make a sub-import raise, so we patch the
        # specific module attribute to raise on access.
        import builtins
        real_import = builtins.__import__

        def _failing_import(name, globals=None, locals=None, fromlist=(),
                             level=0):
            if name == "orchestration":
                raise ImportError("simulated observability import failure")
            if name == "orchestration.observability":
                raise ImportError("simulated observability import failure")
            return real_import(name, globals, locals, fromlist, level)

        with (
            patch("builtins.__import__", side_effect=_failing_import),
            _patch_manager([meta]),
        ):
            from runtime.capabilities import list_capabilities
            caps = list_capabilities()

        # Fail-open returns the aggregated rows, NOT an empty list.
        assert len(caps) == 2
        ids = {c.id for c in caps}
        assert "chat.command.ping" in ids
        assert "chat.command.status" in ids
