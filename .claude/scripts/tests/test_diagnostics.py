"""Tests for The Homie diagnostics collector."""

import json
import sys
from pathlib import Path

import pytest

_CHAT_DIR = str(Path(__file__).parent.parent.parent / "chat")
_SCRIPTS_DIR = str(Path(__file__).parent.parent)
if _CHAT_DIR not in sys.path:
    sys.path.insert(0, _CHAT_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from diagnostics import DiagnosticsReport, check_environment, collect_diagnostics  # noqa: E402


# ---------------------------------------------------------------------------
# Cleanup fixtures shared with test_capabilities.py -- both aggregators are
# triggered by collect_diagnostics() -> _check_capabilities(), so envelope
# tests need the same teardown contract to avoid sys.modules / _AGGREGATORS
# mismatch leaking into downstream tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def cleanup_aggregator():
    """Restore canonical _AGGREGATORS["integrations"] after the test.

    Mirrors test_capabilities.py's fixture of the same name. ``importlib.reload``
    forces module-bottom ``register_aggregator()`` to re-fire so production
    wiring is intact for downstream tests.
    """
    yield

    import importlib
    from runtime import capabilities
    import integrations.registry as reg

    capabilities._AGGREGATORS.pop("integrations", None)
    importlib.reload(reg)
    assert "integrations" in capabilities._AGGREGATORS


@pytest.fixture
def cleanup_overlays_aggregator():
    """Restore canonical _AGGREGATORS["runtime_overlays"] after the test.

    Mirrors test_capabilities.py's fixture of the same name. PRP-1c teardown
    contract: pop, reload, assert restored.
    """
    yield

    import importlib
    from runtime import capabilities
    import runtime.overlays as ov

    capabilities._AGGREGATORS.pop("runtime_overlays", None)
    importlib.reload(ov)
    assert "runtime_overlays" in capabilities._AGGREGATORS


class TestDiagnosticsReport:
    def test_report_has_required_fields(self):
        report = DiagnosticsReport(
            timestamp="2026-03-24T10:00:00",
            uptime_seconds=100.0,
            cognition_available=True,
            cognition_moves={"move1_recall": True},
            recall_last_query=None,
            recall_last_tier=None,
            recall_last_count=0,
            recall_last_latency_ms=None,
            memory_doc_count=42,
            memory_last_indexed=None,
            memory_embedding_status="ready",
            runtime_lanes={"claude_native": "ON", "generic_runtime": "ON"},
            runtime_providers={"claude": "ON"},
            runtime_selected_lane="claude_native",
            runtime_selected_generic_provider=None,
            runtime_generic_text_route=["openai-compatible"],
            runtime_generic_tool_route=["openai-codex"],
            sessions_active=1,
            sessions_total_messages=10,
            sessions_total_cost_usd=0.50,
            adapters_connected={},
        )
        assert report.memory_doc_count == 42
        assert report.cognition_available is True
        assert not hasattr(report, "runtime_default_chain")

    def test_report_defaults(self):
        report = DiagnosticsReport(timestamp="now", uptime_seconds=0.0)
        assert report.cognition_available is False
        assert report.memory_doc_count == 0
        assert report.sessions_active == 0
        assert report.runtime_lanes == {}
        assert report.adapters_connected == {}

    def test_collect_diagnostics_returns_report(self):
        report = collect_diagnostics()
        assert isinstance(report, DiagnosticsReport)
        assert isinstance(report.cognition_moves, dict)
        assert isinstance(report.timestamp, str)
        assert isinstance(report.cognitive_loop, dict)

    def test_collect_diagnostics_runtime_providers(self):
        report = collect_diagnostics()
        assert isinstance(report.runtime_lanes, dict)
        assert isinstance(report.runtime_providers, dict)

    def test_collect_diagnostics_reports_codex_stale_auth(
        self, monkeypatch: pytest.MonkeyPatch,
    ):
        import diagnostics as diagnostics_module
        import runtime.auth_profiles as auth_profiles
        import runtime.health as runtime_health
        import runtime.profiles as profiles

        monkeypatch.setattr(
            auth_profiles,
            "codex_auth_status",
            lambda _profile=None: auth_profiles.AuthProfileStatus(
                False,
                "refresh_token_reused: refresh token has already been used",
            ),
        )
        monkeypatch.setattr(
            profiles,
            "build_profile_for_provider",
            lambda provider, **_kwargs: object() if provider != "openai-codex" else None,
        )
        monkeypatch.setattr(runtime_health, "is_profile_available", lambda _profile: True)
        monkeypatch.setattr(diagnostics_module, "CHAT_DB_PATH", Path("missing.db"))

        report = collect_diagnostics()

        assert report.runtime_providers["openai-codex"] == "OFF"
        issue = report.runtime_auth_issues["openai-codex"]
        assert "Codex CLI auth is stale" in issue
        assert "codex login" in issue
        assert "refresh_token_reused" in issue

    def test_collect_diagnostics_reports_lane_selection(self, monkeypatch):
        import diagnostics as diagnostics_module
        import runtime.profiles as profiles
        import runtime.selection as selection

        monkeypatch.setattr(
            selection,
            "resolve_runtime_selection",
            lambda _env=None: selection.RuntimeSelection(
                lane="generic_runtime",
                generic_provider="openai-codex",
            ),
        )
        monkeypatch.setattr(
            profiles,
            "build_profile_for_provider",
            lambda provider, **_kwargs: object() if provider else None,
        )
        monkeypatch.setattr(diagnostics_module, "CHAT_DB_PATH", Path("missing.db"))

        report = collect_diagnostics()

        assert report.runtime_selected_lane == "generic_runtime"
        assert report.runtime_selected_generic_provider == "openai-codex"
        assert report.runtime_generic_text_route
        assert report.runtime_generic_tool_route

    def test_collect_diagnostics_sessions(self):
        report = collect_diagnostics()
        assert isinstance(report.sessions_active, int)

    def test_collect_diagnostics_reports_clear_lifecycle_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import diagnostics as diagnostics_module

        log_path = tmp_path / "clear-lifecycle-events.jsonl"
        rows = [
            {
                "timestamp": "2026-05-14T10:00:00",
                "session_id": "cli:one:one",
                "events": [
                    {"step": "session-end-flush.py", "status": "warn", "detail": "exit 1"},
                    {"step": "session_delete", "status": "ok", "detail": "deleted"},
                ],
            },
            {
                "timestamp": "2026-05-14T10:01:00",
                "session_id": "cli:two:two",
                "events": [
                    {
                        "step": "session-end-flush.py",
                        "status": "error",
                        "detail": "RuntimeError: flush hook failed again",
                    },
                ],
            },
        ]
        log_path.write_text(
            "\n".join(json.dumps(row) for row in rows),
            encoding="utf-8",
        )
        monkeypatch.setattr(diagnostics_module, "STATE_DIR", tmp_path)
        monkeypatch.setattr(diagnostics_module, "CHAT_DB_PATH", Path("missing.db"))

        report = collect_diagnostics()

        assert report.clear_lifecycle_recent_failures == 2
        assert report.clear_lifecycle_last_failure_at == "2026-05-14T10:01:00"
        assert report.clear_lifecycle_last_failure is not None
        assert "session-end-flush.py" in report.clear_lifecycle_last_failure
        assert "flush hook failed again" in report.clear_lifecycle_last_failure

    def test_report_serializable(self):
        """Ensure report can be serialized to JSON (for API endpoint)."""
        import dataclasses
        import json

        report = collect_diagnostics()
        data = dataclasses.asdict(report)
        json_str = json.dumps(data)
        assert isinstance(json_str, str)
        parsed = json.loads(json_str)
        assert "timestamp" in parsed
        assert "cognition_available" in parsed
        assert "cognitive_loop" in parsed

    def test_collect_diagnostics_includes_cognitive_loop_status(self):
        report = collect_diagnostics()

        assert report.cognitive_loop["overall"] == "partial"
        subsystems = report.cognitive_loop["subsystems"]
        assert subsystems["active_inferences"]["state"] == "live"
        assert subsystems["heartbeat_identity"]["state"] == "live"
        assert subsystems["working_memory"]["state"] == "shadow_only"


class TestEnvironmentCheck:
    def test_returns_list(self):
        issues = check_environment()
        assert isinstance(issues, list)

    def test_issue_format(self):
        issues = check_environment()
        for level, msg, hint in issues:
            assert level in ("error", "warn", "info")
            assert isinstance(msg, str)
            assert isinstance(hint, str)

    def test_python_version_ok(self):
        """Current Python should be 3.12+, so no Python error."""
        issues = check_environment()
        python_errors = [i for i in issues if "Python" in i[1] and i[0] == "error"]
        assert len(python_errors) == 0


# ---------------------------------------------------------------------------
# PRP-1b: capabilities + toolsets envelope tests
# ---------------------------------------------------------------------------


class TestCapabilitiesEnvelope:
    def test_status_json_includes_capabilities_and_toolsets_keys(self):
        """``thehomie status --json`` must surface ``capabilities`` (list,
        possibly empty) and ``toolsets`` (dict, possibly empty) at the top
        level as siblings of the existing keys."""
        import dataclasses

        report = collect_diagnostics()
        data = dataclasses.asdict(report)

        assert "capabilities" in data
        assert isinstance(data["capabilities"], list)
        assert "toolsets" in data
        assert isinstance(data["toolsets"], dict)

    def test_status_json_existing_21_keys_unchanged(self):
        """Regression: lock the envelope against accidental key removal.
        All 21 pre-PRP-1b ``DiagnosticsReport`` field names must remain."""
        import dataclasses

        report = collect_diagnostics()
        data = dataclasses.asdict(report)

        expected_keys = {
            "timestamp",
            "uptime_seconds",
            "cognition_available",
            "cognition_moves",
            "recall_last_query",
            "recall_last_tier",
            "recall_last_count",
            "recall_last_latency_ms",
            "memory_doc_count",
            "memory_last_indexed",
            "memory_embedding_status",
            "runtime_lanes",
            "runtime_providers",
            "runtime_selected_lane",
            "runtime_selected_generic_provider",
            "runtime_generic_text_route",
            "runtime_generic_tool_route",
            "sessions_active",
            "sessions_total_messages",
            "sessions_total_cost_usd",
            "adapters_connected",
        }
        for key in expected_keys:
            assert key in data, f"Missing pre-PRP-1b key: {key!r}"

    def test_status_json_capabilities_default_empty_on_import_failure(self):
        """B4 atomic-build: if ``runtime.capabilities`` import inside
        ``_check_capabilities`` fails, BOTH ``capabilities`` and
        ``toolsets`` stay at dataclass defaults (``[]`` and ``{}``).
        No partial state."""
        import builtins

        real_import = builtins.__import__

        def _failing_import(name, globals=None, locals=None, fromlist=(),
                             level=0):
            # Force the late ``from runtime.capabilities import ...`` inside
            # _check_capabilities to raise.
            if name == "runtime.capabilities" and fromlist:
                if "list_capabilities" in fromlist or "resolve_toolset" in fromlist:
                    raise ImportError("simulated runtime.capabilities import failure")
            return real_import(name, globals, locals, fromlist, level)

        from unittest.mock import patch

        with patch("builtins.__import__", side_effect=_failing_import):
            report = collect_diagnostics()

        # Atomic build: both fields move together or neither moves.
        assert report.capabilities == []
        assert report.toolsets == {}

    def test_status_json_capabilities_default_empty_on_toolsets_import_failure(self):
        """M4 fix — separate failure path: if ``runtime.toolsets`` import
        fails inside ``_check_capabilities`` (even though
        ``runtime.capabilities`` imports cleanly), BOTH ``capabilities``
        and ``toolsets`` stay at dataclass defaults due to atomic build."""
        import builtins

        real_import = builtins.__import__

        def _failing_import(name, globals=None, locals=None, fromlist=(),
                             level=0):
            # Let runtime.capabilities import succeed but make
            # runtime.toolsets fail.
            if name == "runtime.toolsets" and fromlist and "TOOLSETS" in fromlist:
                raise ImportError("simulated runtime.toolsets import failure")
            return real_import(name, globals, locals, fromlist, level)

        from unittest.mock import patch

        with patch("builtins.__import__", side_effect=_failing_import):
            report = collect_diagnostics()

        # Atomic build per B4: even if list_capabilities succeeded, the
        # toolset comprehension never ran, so neither field is mutated.
        assert report.capabilities == []
        assert report.toolsets == {}

    def test_diagnostics_envelope_contains_integrations(self):
        """PRP-1b production contract: collect_diagnostics() surfaces ALL
        integration capabilities via the integrations aggregator, and the
        integrations toolset auto-resolves to those same ids via live_source.

        Existing envelope tests only check top-level keys; this one locks
        the actual content. Catches regressions like the cached-sys.modules
        / popped-_AGGREGATORS mismatch surfaced by Codex Stage 9 review —
        a regression where register_aggregator() doesn't fire would silently
        pass the other envelope tests but FAIL this one (0 caps != 11).
        """
        from integrations.registry import _REGISTRY

        report = collect_diagnostics()

        integration_caps = [
            c for c in report.capabilities
            if c["id"].startswith("integration.")
        ]
        expected_count = len(_REGISTRY)

        assert len(integration_caps) == expected_count, (
            f"Diagnostics envelope missing integrations: expected "
            f"{expected_count} integration.* caps, got "
            f"{len(integration_caps)}: {[c['id'] for c in integration_caps]}"
        )

        assert "integrations" in report.toolsets, (
            "Integrations toolset not in diagnostics envelope — "
            "TOOLSETS or resolve_toolset() wiring broken"
        )
        assert len(report.toolsets["integrations"]) == expected_count, (
            f"Integrations toolset auto-discovery broken: expected "
            f"{expected_count} ids, got "
            f"{len(report.toolsets['integrations'])}: "
            f"{report.toolsets['integrations']}"
        )
        assert all(
            tid.startswith("integration.")
            for tid in report.toolsets["integrations"]
        ), f"Toolset has non-integration ids: {report.toolsets['integrations']}"

    def test_diagnostics_envelope_contains_runtime_overlays(
        self, cleanup_aggregator, cleanup_overlays_aggregator,
    ):
        """PRP-1c production contract: collect_diagnostics() surfaces ALL
        runtime overlay capabilities via the runtime_overlays aggregator.

        Count-based assertion locks the actual content. Imports BOTH
        cleanup fixtures because collect_diagnostics() triggers
        ``import integrations.registry`` AND ``import runtime.overlays``;
        without both fixtures, sys.modules / _AGGREGATORS mismatch can
        break downstream tests. LIFO teardown order is fine -- both
        fixtures are idempotent.
        """
        from runtime import profiles

        report = collect_diagnostics()

        overlay_caps = [
            c for c in report.capabilities
            if c["source"] == "runtime_overlay"
        ]
        # +1 for hardcoded claude-native lane.
        expected_count = len(profiles.GENERIC_PROVIDER_REGISTRY) + 1

        assert len(overlay_caps) == expected_count, (
            f"Diagnostics envelope missing runtime overlays: expected "
            f"{expected_count} runtime_overlay caps, got "
            f"{len(overlay_caps)}: {[c['id'] for c in overlay_caps]}"
        )

        # All ids match the runtime.overlay.* namespace.
        assert all(
            c["id"].startswith("runtime.overlay.")
            for c in overlay_caps
        ), f"Capability has non-overlay id: {[c['id'] for c in overlay_caps]}"

        # Claude is among them.
        assert any(c["id"] == "runtime.overlay.claude" for c in overlay_caps), (
            f"Missing runtime.overlay.claude in: {[c['id'] for c in overlay_caps]}"
        )
