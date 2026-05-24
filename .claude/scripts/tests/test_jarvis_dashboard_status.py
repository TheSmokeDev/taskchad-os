"""Tests for the Mission Control Jarvis status proof endpoint."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_jarvis_status_projects_diagnostics_contract(monkeypatch) -> None:
    from diagnostics import DiagnosticsReport
    import dashboard_api
    import diagnostics as diagnostics_module

    report = DiagnosticsReport(
        timestamp="2026-05-23T10:00:00",
        uptime_seconds=12.5,
        cognition_available=True,
        cognitive_loop={
            "overall": "live",
            "source_wiring_overall": "live",
            "autonomy_overall": "live",
            "state_counts": {"live": 2},
            "subsystems": {"heartbeat_identity": {"state": "live"}},
            "autonomous_loop": {
                "overall": "live",
                "subsystems": {"proactive_agency": {"state": "live"}},
            },
            "next_actions": [],
        },
        memory_doc_count=2932,
        memory_embedding_status="ready",
        runtime_lanes={"claude_native": "ON", "generic_runtime": "ON"},
        runtime_providers={"claude": "ON", "openai-codex": "ON"},
        runtime_selected_lane="claude_native",
        runtime_selected_model="claude-sonnet-4-6",
        runtime_configured_models={"claude": "claude-sonnet-4-6"},
        capabilities=[
            {
                "id": "gmail",
                "display_name": "Gmail",
                "enabled": True,
                "source": "integrations",
            },
            {
                "id": "slack",
                "display_name": "Slack",
                "enabled": False,
                "source": "integrations",
            },
        ],
        toolsets={"integrations": ["gmail"]},
        sessions_active=7,
    )

    monkeypatch.setattr(diagnostics_module, "collect_diagnostics", lambda: report)
    monkeypatch.setattr(
        dashboard_api,
        "_collect_profile_lifecycle_summary",
        lambda: {
            "active_profile": "default",
            "orchestration_api_port": 4322,
            "health_check_port": 8787,
            "whatsapp_webhook_port": 8443,
        },
    )
    monkeypatch.setattr(
        dashboard_api,
        "_read_channel_health",
        lambda _port: {
            "status": "ok",
            "reachable": True,
            "adapters": {"telegram": True},
            "sessions_active": 8,
            "runtime_providers": {"claude": "ON", "openai-codex": "ON"},
            "memory_doc_count": 2932,
            "memory_embedding_status": "ready",
        },
    )
    monkeypatch.setattr(
        dashboard_api,
        "_collect_documented_proofs",
        lambda: {
            "langfuse_trace_id": "a" * 32,
            "sentry_event_id": "b" * 32,
            "self_amendment_proposal_id": None,
            "sources": [],
            "lookup_status": "documented_local_proof",
        },
    )

    app = FastAPI()
    app.include_router(dashboard_api.router)
    response = TestClient(app).get("/api/jarvis/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["runtime"]["selected_lane"] == "claude_native"
    assert payload["runtime"]["selected_model"] == "claude-sonnet-4-6"
    assert payload["autonomy"]["autonomous_loop_overall"] == "live"
    assert payload["memory"]["doc_count"] == 2932
    assert payload["capabilities"]["enabled_count"] == 1
    assert payload["channels"]["telegram"]["connected"] is True
    assert payload["channels"]["telegram"]["metadata_alignment"] == {
        "runtime_providers_populated": True,
        "memory_doc_count_matches_cli": True,
    }
    assert payload["observability"]["langfuse_trace_id"] == "a" * 32
