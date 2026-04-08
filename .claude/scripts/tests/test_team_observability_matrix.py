"""Helper contract validation for orchestration_span().

Tests all branches of the context manager: enabled/disabled × happy/expected-error/unexpected-error,
plus import failure and standalone update_observation.

NOTE: These tests validate the helper's branch coverage with mocked Langfuse.
They do NOT constitute proof that real CLI/API paths produce Langfuse traces.
Real-path integration tests are in test_team_observability.py::TestRealPathIntegration.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _fake_disabled(key, default=None):
    if key in ("LANGFUSE_ENABLED", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        return {"LANGFUSE_ENABLED": "false"}.get(key, "")
    import os
    return os.environ.get(key, default)


def _make_mock_client():
    ctx = MagicMock()  # the context manager from start_as_current_observation
    client = MagicMock()
    client.start_as_current_observation.return_value = ctx
    client.get_current_trace_id.return_value = "matrix-trace-001"
    client.get_current_observation_id.return_value = "matrix-obs-001"
    fake_mod = MagicMock()
    fake_mod.get_client.return_value = client
    return fake_mod, client, ctx


class TestEnabledHappyPath:
    def test_yields_trace_ids(self):
        fake_mod, _, _ = _make_mock_client()
        with (
            patch("orchestration.observability.is_langfuse_enabled", return_value=True),
            patch("orchestration.observability.init_langfuse"),
            patch.dict("sys.modules", {"langfuse": fake_mod}),
        ):
            from orchestration.observability import orchestration_span
            with orchestration_span("s1", metadata={"test": True}) as state:
                pass
        assert state["trace_id"] == "matrix-trace-001"
        assert state["observation_id"] == "matrix-obs-001"


class TestEnabledExpectedException:
    def test_preserves_trace_ids_on_expected_error(self):
        fake_mod, _, _ = _make_mock_client()
        with (
            patch("orchestration.observability.is_langfuse_enabled", return_value=True),
            patch("orchestration.observability.init_langfuse"),
            patch.dict("sys.modules", {"langfuse": fake_mod}),
        ):
            from orchestration.observability import orchestration_span
            with pytest.raises(ValueError, match="expected"):
                with orchestration_span("s2", expected_exceptions=(ValueError,)) as state:
                    raise ValueError("expected")
        assert state["trace_id"] == "matrix-trace-001"


class TestEnabledUnexpectedException:
    def test_preserves_trace_ids_and_captures_sentry(self):
        fake_mod, _, _ = _make_mock_client()
        with (
            patch("orchestration.observability.is_langfuse_enabled", return_value=True),
            patch("orchestration.observability.init_langfuse"),
            patch.dict("sys.modules", {"langfuse": fake_mod}),
        ):
            from orchestration.observability import orchestration_span
            with pytest.raises(RuntimeError, match="surprise"):
                with orchestration_span("s3") as state:
                    raise RuntimeError("surprise")
        assert state["trace_id"] == "matrix-trace-001"


class TestDisabledHappyPath:
    def test_yields_none_ids(self):
        with patch("runtime.langfuse_setup.os.getenv", side_effect=_fake_disabled):
            from orchestration.observability import orchestration_span
            with orchestration_span("s4") as state:
                pass
        assert state["trace_id"] is None
        assert state["observation_id"] is None


class TestDisabledExpectedException:
    def test_yields_none_ids_on_expected_error(self):
        with patch("runtime.langfuse_setup.os.getenv", side_effect=_fake_disabled):
            from orchestration.observability import orchestration_span
            with pytest.raises(ValueError, match="expected"):
                with orchestration_span("s5", expected_exceptions=(ValueError,)) as state:
                    raise ValueError("expected")
        assert state["trace_id"] is None


class TestDisabledUnexpectedException:
    def test_yields_none_ids_on_unexpected_error(self):
        with patch("runtime.langfuse_setup.os.getenv", side_effect=_fake_disabled):
            from orchestration.observability import orchestration_span
            with pytest.raises(RuntimeError, match="surprise"):
                with orchestration_span("s6") as state:
                    raise RuntimeError("surprise")
        assert state["trace_id"] is None


class TestImportFailure:
    def test_falls_back_to_noop_on_get_client_failure(self):
        fake_langfuse = MagicMock()
        fake_langfuse.get_client.side_effect = RuntimeError("broken")
        with (
            patch("orchestration.observability.is_langfuse_enabled", return_value=True),
            patch("orchestration.observability.init_langfuse"),
            patch.dict("sys.modules", {"langfuse": fake_langfuse}),
        ):
            from orchestration.observability import orchestration_span
            with orchestration_span("s7") as state:
                pass
        assert state["trace_id"] is None
        assert state["observation_id"] is None


class TestUpdateObservationStandalone:
    def test_returns_none_ids_when_disabled(self):
        from orchestration.observability import update_observation
        result = update_observation(metadata={"should": "noop"})
        assert result["trace_id"] is None
        assert result["observation_id"] is None
