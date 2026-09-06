from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest
from core_handlers import handle_signal, handle_social


@pytest.mark.asyncio
async def test_signal_authority_status_refresh_and_queue(monkeypatch):
    calls = []
    module = types.ModuleType("business_signal.authority")
    module.get_authority_status = lambda: "AUTHORITY STATUS"
    module.list_authority_queue = lambda: [
        {
            "signal_id": "as_20260903_abcdef1234567890",
            "series": "GEO Signal",
            "score_class": "public_primary",
            "expires_at": "2026-09-10T00:00:00+00:00",
            "source_url": "https://example.com/source",
        }
    ]

    async def refresh():
        calls.append("refresh")
        return SimpleNamespace(as_dict=lambda: {"status": "success"})

    module.run_authority_refresh = refresh
    monkeypatch.setitem(sys.modules, "business_signal.authority", module)

    assert await handle_signal(None, None, "authority status") == "AUTHORITY STATUS"
    queued = await handle_signal(None, None, "authority queue")
    assert "as_20260903_abcdef1234567890" in queued
    refreshed = await handle_signal(None, None, "authority refresh")
    assert '"status": "success"' in refreshed
    assert calls == ["refresh"]


@pytest.mark.asyncio
async def test_social_outcome_command_records_without_causal_attribution(monkeypatch):
    calls = []
    module = types.ModuleType("social.outcomes")
    module.parse_outcome_arguments = lambda value: (
        "post:42",
        {"post_saves": 7, "stars_delta": 2},
        "operator receipt",
    )

    def record(subject, metrics, *, note):
        calls.append((subject, metrics, note))
        return {
            "status": "written",
            "outcome": {
                "outcome_id": "so_20260903T150000Z_abcdef1234567890",
                "subject_id": subject,
                "metrics": metrics,
                "github_attribution": "correlated_movement_not_conversion",
            },
        }

    module.record_outcome = record
    module.list_outcomes = lambda **kwargs: []
    monkeypatch.setitem(sys.modules, "social.outcomes", module)

    result = await handle_social(
        None,
        SimpleNamespace(),
        "outcome post:42 post_saves=7 stars_delta=2 note='operator receipt'",
    )
    assert calls == [
        ("post:42", {"post_saves": 7, "stars_delta": 2}, "operator receipt")
    ]
    assert "Causal attribution: none" in result
    assert "correlated movement, not conversions" in result
