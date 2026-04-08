"""Tests for The Homie diagnostics collector."""

import sys
from pathlib import Path

_CHAT_DIR = str(Path(__file__).parent.parent.parent / "chat")
_SCRIPTS_DIR = str(Path(__file__).parent.parent)
if _CHAT_DIR not in sys.path:
    sys.path.insert(0, _CHAT_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from diagnostics import DiagnosticsReport, check_environment, collect_diagnostics


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
            runtime_providers={"claude": "ON"},
            runtime_default_chain=["claude"],
            sessions_active=1,
            sessions_total_messages=10,
            sessions_total_cost_usd=0.50,
            adapters_connected={},
        )
        assert report.memory_doc_count == 42
        assert report.cognition_available is True

    def test_report_defaults(self):
        report = DiagnosticsReport(timestamp="now", uptime_seconds=0.0)
        assert report.cognition_available is False
        assert report.memory_doc_count == 0
        assert report.sessions_active == 0
        assert report.adapters_connected == {}

    def test_collect_diagnostics_returns_report(self):
        report = collect_diagnostics()
        assert isinstance(report, DiagnosticsReport)
        assert isinstance(report.cognition_moves, dict)
        assert isinstance(report.timestamp, str)

    def test_collect_diagnostics_runtime_providers(self):
        report = collect_diagnostics()
        assert isinstance(report.runtime_providers, dict)

    def test_collect_diagnostics_sessions(self):
        report = collect_diagnostics()
        assert isinstance(report.sessions_active, int)

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
