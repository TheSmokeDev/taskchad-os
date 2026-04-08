"""Tests for orchestration observability module.

Covers:
- Helper contract: disabled path, enabled path (mocked), expected/unexpected exceptions,
  update_observation no-op and active, Langfuse import failure, team_service import.
- Real-path integration: exercises TeamService and orchestration API through the actual
  orchestration_span call sites with Langfuse disabled (no-op safe verification).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure scripts dir is importable
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _fake_getenv_disabled(key, default=None):
    """Simulate Langfuse disabled (no keys)."""
    overrides = {
        "LANGFUSE_ENABLED": "false",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
    }
    if key in overrides:
        return overrides[key]
    return os.environ.get(key, default)


def _mock_langfuse_client():
    """Create a mock Langfuse client + observation context manager."""
    mock_obs_cm = MagicMock()  # the context manager returned by start_as_current_observation

    mock_client = MagicMock()
    mock_client.start_as_current_observation.return_value = mock_obs_cm
    mock_client.get_current_trace_id.return_value = "mock-trace-abc123"
    mock_client.get_current_observation_id.return_value = "mock-obs-def456"

    # Build a fake langfuse module
    fake_mod = MagicMock()
    fake_mod.get_client.return_value = mock_client
    return fake_mod, mock_client, mock_obs_cm


class TestOrchestrationSpanDisabled:
    """Tests for orchestration_span when Langfuse is disabled."""

    def test_yields_state_dict_with_none_sentinels(self):
        with patch(
            "runtime.langfuse_setup.os.getenv",
            side_effect=_fake_getenv_disabled,
        ):
            from orchestration.observability import orchestration_span

            with orchestration_span("test_span") as state:
                assert "trace_id" in state
                assert "observation_id" in state
                assert state["trace_id"] is None
                assert state["observation_id"] is None

    def test_expected_exception_preserves_trace_ids_disabled(self):
        with patch(
            "runtime.langfuse_setup.os.getenv",
            side_effect=_fake_getenv_disabled,
        ):
            from orchestration.observability import orchestration_span

            with pytest.raises(ValueError, match="expected error"):
                with orchestration_span(
                    "test_err",
                    expected_exceptions=(ValueError,),
                ) as state:
                    assert state["trace_id"] is None
                    raise ValueError("expected error")
            assert state["trace_id"] is None
            assert state["observation_id"] is None

    def test_unexpected_exception_preserves_trace_ids_disabled(self):
        with patch(
            "runtime.langfuse_setup.os.getenv",
            side_effect=_fake_getenv_disabled,
        ):
            from orchestration.observability import orchestration_span

            with pytest.raises(RuntimeError, match="unexpected"):
                with orchestration_span("test_unexpected") as state:
                    assert state["trace_id"] is None
                    raise RuntimeError("unexpected")
            assert state["trace_id"] is None
            assert state["observation_id"] is None


class TestOrchestrationSpanEnabled:
    """Tests for orchestration_span when Langfuse is enabled (mocked)."""

    def test_yields_trace_ids_when_enabled(self):
        fake_mod, mock_client, _ = _mock_langfuse_client()

        with (
            patch("orchestration.observability.is_langfuse_enabled", return_value=True),
            patch("orchestration.observability.init_langfuse"),
            patch.dict("sys.modules", {"langfuse": fake_mod}),
        ):
            from orchestration.observability import orchestration_span

            with orchestration_span("test_enabled") as state:
                assert state["trace_id"] == "mock-trace-abc123"
                assert state["observation_id"] == "mock-obs-def456"

    def test_expected_exception_in_enabled_span(self):
        fake_mod, mock_client, _ = _mock_langfuse_client()

        with (
            patch("orchestration.observability.is_langfuse_enabled", return_value=True),
            patch("orchestration.observability.init_langfuse"),
            patch.dict("sys.modules", {"langfuse": fake_mod}),
        ):
            from orchestration.observability import orchestration_span

            with pytest.raises(ValueError, match="test expected"):
                with orchestration_span(
                    "test_err",
                    metadata={"key": "val"},
                    expected_exceptions=(ValueError,),
                ) as state:
                    assert state["trace_id"] == "mock-trace-abc123"
                    raise ValueError("test expected")

    def test_unexpected_exception_in_enabled_span(self):
        fake_mod, mock_client, _ = _mock_langfuse_client()

        with (
            patch("orchestration.observability.is_langfuse_enabled", return_value=True),
            patch("orchestration.observability.init_langfuse"),
            patch.dict("sys.modules", {"langfuse": fake_mod}),
        ):
            from orchestration.observability import orchestration_span

            with pytest.raises(RuntimeError, match="surprise"):
                with orchestration_span("test_unexpected") as state:
                    assert state["trace_id"] == "mock-trace-abc123"
                    raise RuntimeError("surprise")


class TestUpdateObservation:
    """Tests for update_observation helper."""

    def test_returns_none_ids_when_disabled(self):
        """Returns dict with None IDs when Langfuse is disabled."""
        with patch(
            "runtime.langfuse_setup.os.getenv",
            side_effect=_fake_getenv_disabled,
        ):
            from orchestration.observability import update_observation

            result = update_observation(metadata={"key": "val"})
            assert result["trace_id"] is None
            assert result["observation_id"] is None

    def test_returns_ids_when_enabled(self):
        fake_mod, mock_client, _ = _mock_langfuse_client()

        with (
            patch("orchestration.observability.is_langfuse_enabled", return_value=True),
            patch.dict("sys.modules", {"langfuse": fake_mod}),
        ):
            from orchestration.observability import update_observation

            result = update_observation(metadata={"result": "success"})
            assert result["trace_id"] == "mock-trace-abc123"
            assert result["observation_id"] == "mock-obs-def456"


class TestLangfuseImportFailure:
    """Tests for graceful Langfuse import failure handling."""

    def test_graceful_fallback_on_import_failure(self):
        """Langfuse get_client fails → falls back to no-op, state stays None."""
        fake_langfuse = MagicMock()
        fake_langfuse.get_client.side_effect = RuntimeError("broken")

        with (
            patch("orchestration.observability.is_langfuse_enabled", return_value=True),
            patch("orchestration.observability.init_langfuse"),
            patch.dict("sys.modules", {"langfuse": fake_langfuse}),
        ):
            from orchestration.observability import orchestration_span

            with orchestration_span("test_import_fail") as state:
                # Falls back gracefully — state has None IDs
                assert state["trace_id"] is None
                assert state["observation_id"] is None


class TestSentryDualLane:
    """Tests for Sentry/GlitchTip integration in observability module."""

    def test_capture_sentry_exception_with_dsn(self):
        """_capture_sentry_exception calls sentry_sdk.capture_exception with scope tags."""
        mock_sentry = MagicMock()
        mock_scope = MagicMock()
        mock_sentry.push_scope.return_value.__enter__ = MagicMock(return_value=mock_scope)
        mock_sentry.push_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_sentry.capture_exception.return_value = "sentry-event-id-123"

        with (
            patch.dict("sys.modules", {"sentry_sdk": mock_sentry}),
            patch("orchestration.observability.os.getenv", return_value="https://sentry.example.com/1"),
        ):
            from orchestration.observability import _capture_sentry_exception

            exc = RuntimeError("test error")
            result = _capture_sentry_exception(
                exc,
                span_name="test_span",
                metadata={"team_id": "t1", "convoy_id": 5},
            )

            assert result == "sentry-event-id-123"
            mock_sentry.capture_exception.assert_called_once_with(exc)
            mock_scope.set_tag.assert_any_call("component", "orchestration")
            mock_scope.set_tag.assert_any_call("span_name", "test_span")
            mock_scope.set_tag.assert_any_call("team_id", "t1")
            mock_scope.set_tag.assert_any_call("convoy_id", "5")
            mock_scope.set_context.assert_called_once()

    def test_capture_sentry_exception_no_dsn(self):
        """_capture_sentry_exception returns None when SENTRY_DSN is not set."""
        with patch("orchestration.observability.os.getenv", return_value=None):
            from orchestration.observability import _capture_sentry_exception

            result = _capture_sentry_exception(
                RuntimeError("no dsn"),
                span_name="test",
            )
            assert result is None

    def test_capture_sentry_exception_import_fails(self):
        """_capture_sentry_exception returns None when sentry_sdk import fails."""
        with (
            patch("orchestration.observability.os.getenv", return_value="https://sentry.example.com/1"),
            patch.dict("sys.modules", {"sentry_sdk": None}),
        ):
            from orchestration.observability import _capture_sentry_exception

            result = _capture_sentry_exception(
                RuntimeError("broken"),
                span_name="test",
            )
            assert result is None

    def test_init_sentry_with_dsn(self):
        """init_orchestration_observability initializes Sentry when DSN is set."""
        import orchestration.observability as obs_mod

        mock_sentry = MagicMock()
        original_initialized = obs_mod._SENTRY_INITIALIZED

        try:
            obs_mod._SENTRY_INITIALIZED = False

            with (
                patch("orchestration.observability.init_langfuse"),
                patch("orchestration.observability.os.getenv", side_effect=lambda k, *a: "https://sentry.example.com/1" if k == "SENTRY_DSN" else (a[0] if a else None)),
                patch.dict("sys.modules", {"sentry_sdk": mock_sentry, "config": MagicMock()}),
            ):
                obs_mod.init_orchestration_observability()

            mock_sentry.init.assert_called_once()
            call_kwargs = mock_sentry.init.call_args
            assert call_kwargs[1]["dsn"] == "https://sentry.example.com/1"
            assert obs_mod._SENTRY_INITIALIZED is True
        finally:
            obs_mod._SENTRY_INITIALIZED = original_initialized

    def test_init_sentry_skips_when_already_initialized(self):
        """init_orchestration_observability skips Sentry if already initialized."""
        import orchestration.observability as obs_mod

        mock_sentry = MagicMock()
        original_initialized = obs_mod._SENTRY_INITIALIZED

        try:
            obs_mod._SENTRY_INITIALIZED = True

            with (
                patch("orchestration.observability.init_langfuse"),
                patch("orchestration.observability.os.getenv", return_value="https://sentry.example.com/1"),
                patch.dict("sys.modules", {"sentry_sdk": mock_sentry, "config": MagicMock()}),
            ):
                obs_mod.init_orchestration_observability()

            mock_sentry.init.assert_not_called()
        finally:
            obs_mod._SENTRY_INITIALIZED = original_initialized

    def test_sentry_event_id_populated_on_unexpected_error(self):
        """orchestration_span populates sentry_event_id on unexpected exceptions."""
        mock_sentry = MagicMock()
        mock_scope = MagicMock()
        mock_sentry.push_scope.return_value.__enter__ = MagicMock(return_value=mock_scope)
        mock_sentry.push_scope.return_value.__exit__ = MagicMock(return_value=False)
        mock_sentry.capture_exception.return_value = "sentry-evt-456"

        with (
            patch(
                "runtime.langfuse_setup.os.getenv",
                side_effect=_fake_getenv_disabled,
            ),
            patch.dict("sys.modules", {"sentry_sdk": mock_sentry}),
            patch("orchestration.observability.os.getenv", return_value="https://sentry.example.com/1"),
        ):
            from orchestration.observability import orchestration_span

            with pytest.raises(RuntimeError, match="boom"):
                with orchestration_span("sentry_test") as state:
                    raise RuntimeError("boom")

            assert state["sentry_event_id"] == "sentry-evt-456"
            mock_sentry.capture_exception.assert_called_once()

    def test_sentry_not_called_for_expected_exception(self):
        """orchestration_span does NOT call Sentry for expected exceptions."""
        mock_sentry = MagicMock()

        with (
            patch(
                "runtime.langfuse_setup.os.getenv",
                side_effect=_fake_getenv_disabled,
            ),
            patch.dict("sys.modules", {"sentry_sdk": mock_sentry}),
            patch("orchestration.observability.os.getenv", return_value="https://sentry.example.com/1"),
        ):
            from orchestration.observability import orchestration_span

            with pytest.raises(ValueError, match="expected"):
                with orchestration_span(
                    "sentry_expected_test",
                    expected_exceptions=(ValueError,),
                ) as state:
                    raise ValueError("expected")

            assert state["sentry_event_id"] is None
            mock_sentry.capture_exception.assert_not_called()


class TestTeamServiceImport:
    """Verify that team_service.py can import the observability module."""

    def test_team_service_imports_successfully(self):
        """Module exists and is compatible with team_service.py."""
        from orchestration.team_service import TeamService
        assert callable(TeamService)

    def test_observability_functions_importable(self):
        """The exact import from team_service.py line 27 works."""
        from orchestration.observability import (
            orchestration_span,
            update_observation,
        )
        assert callable(orchestration_span)
        assert callable(update_observation)


class TestRealPathIntegration:
    """Real-path integration tests.

    These exercise the actual TeamService/API/team_memory code paths that contain
    orchestration_span() calls — with Langfuse disabled (as in a fresh checkout).
    This proves the no-op safety contract: instrumented code runs without Langfuse
    and doesn't break.

    These do NOT test Langfuse trace output (that requires a running Langfuse server).
    They DO prove that the real call sites execute cleanly through orchestration_span.
    """

    @pytest.fixture(autouse=True)
    def _disable_langfuse(self):
        """Ensure Langfuse is disabled for all integration tests."""
        with patch(
            "runtime.langfuse_setup.os.getenv",
            side_effect=_fake_getenv_disabled,
        ):
            yield

    def _make_team_service(self):
        """Create a TeamService backed by an in-memory DB."""
        from orchestration.db import OrchestrationDB
        from orchestration.team_service import TeamService
        db = OrchestrationDB(":memory:")
        return TeamService(db)

    def test_team_service_create_session_uses_span(self):
        """TeamService.create_team_session() runs through orchestration_span without error."""
        from orchestration.models import CreateTeamSessionInput

        ts = self._make_team_service()
        inp = CreateTeamSessionInput(team_name="test-obs-team", lead_agent_id="test-agent")
        session = ts.create_team_session(inp)
        assert session.session.team_name == "test-obs-team"
        assert session.session.id is not None

    def test_team_service_list_sessions_uses_span(self):
        """TeamService.list_team_sessions() runs through orchestration_span."""
        from orchestration.models import CreateTeamSessionInput

        ts = self._make_team_service()
        ts.create_team_session(CreateTeamSessionInput(team_name="t1", lead_agent_id="a"))
        sessions = ts.list_team_sessions()
        assert len(sessions) >= 1

    def test_team_service_get_session_uses_span(self):
        """TeamService.get_team_session() runs through orchestration_span."""
        from orchestration.models import CreateTeamSessionInput

        ts = self._make_team_service()
        created = ts.create_team_session(CreateTeamSessionInput(team_name="t2", lead_agent_id="a"))
        fetched = ts.get_team_session(created.session.id)
        assert fetched is not None
        assert fetched.session.team_name == "t2"

    def test_team_service_add_member_uses_span(self):
        """TeamService.add_member() runs through orchestration_span."""
        from orchestration.models import AddTeamMemberInput, CreateTeamSessionInput

        ts = self._make_team_service()
        session = ts.create_team_session(CreateTeamSessionInput(team_name="t3", lead_agent_id="a"))
        member = ts.add_member(
            session.session.id,
            AddTeamMemberInput(agent_id="agent-1", role="worker"),
        )
        assert member.agent_id == "agent-1"

    def test_team_service_shutdown_uses_span(self):
        """TeamService.request_shutdown() runs through orchestration_span."""
        from orchestration.models import CreateTeamSessionInput

        ts = self._make_team_service()
        session = ts.create_team_session(CreateTeamSessionInput(team_name="t4", lead_agent_id="a"))
        result = ts.request_shutdown(session.session.id)
        assert result is not None

    def test_team_memory_write_read_uses_span(self, tmp_path, monkeypatch):
        """team_memory write/read runs through orchestration_span."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))

        from orchestration.team_memory import list_team_memory, read_team_memory, write_team_memory

        write_team_memory(team_id=1, filename="notes.md", content="# Hello from integration test.")
        content = read_team_memory(team_id=1, filename="notes.md")
        assert "Hello from integration test" in content

        files = list_team_memory(team_id=1)
        assert "notes.md" in files

    def test_team_memory_secret_guardrail_uses_span(self, tmp_path, monkeypatch):
        """team_memory secret guardrail fires through orchestration_span."""
        monkeypatch.setenv("VAULT_ROOT", str(tmp_path))

        from orchestration.team_memory import write_team_memory

        with pytest.raises(ValueError):
            # Trigger the api_key pattern guardrail
            write_team_memory(
                team_id=1,
                filename="secrets.md",
                content="api_key: " + "A" * 30,
            )
