"""Tests for the heartbeat runtime contract."""

from __future__ import annotations

import types

import pytest

import heartbeat
from runtime.base import RUNTIME_LANE_GENERIC, RuntimeResult
from runtime.capabilities import TOOL_REASONING


def _install_quiet_heartbeat(monkeypatch: pytest.MonkeyPatch) -> list[heartbeat.RuntimeRequest]:
    captured_requests: list[heartbeat.RuntimeRequest] = []

    async def fake_gather_heartbeat_context():
        return (
            "## Email\n\nNo urgent emails.\n\n"
            "## Slack\n\nNo important messages.\n\n"
            "## Calendar\n\nNo meetings starting soon.",
            [],
        )

    async def fake_run_with_runtime_lanes(request: heartbeat.RuntimeRequest) -> RuntimeResult:
        captured_requests.append(request)
        return RuntimeResult(
            text="HEARTBEAT_OK",
            runtime_lane=RUNTIME_LANE_GENERIC,
            provider="openai-codex",
            model=request.fallback_model or "gpt-5.5",
        )

    async def fake_recall(**_kwargs):
        return types.SimpleNamespace(formatted_text="")

    monkeypatch.setitem(
        __import__("sys").modules,
        "recall_service",
        types.SimpleNamespace(
            recall=fake_recall,
            reindex_changed=lambda _memory_dir: {"files_indexed": 0},
        ),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "memory_index",
        types.SimpleNamespace(sync_index=lambda: {"files_indexed": 0, "files_skipped": 0}),
    )

    monkeypatch.setattr(heartbeat, "gather_heartbeat_context", fake_gather_heartbeat_context)
    monkeypatch.setattr(heartbeat, "gather_habits_context", lambda: "- [x] Health\n- [x] Work")
    monkeypatch.setattr(
        heartbeat,
        "gather_circle_drafts_context",
        lambda: (
            "### Unreplied Circle DMs\nNone - all DMs are responded to.\n\n"
            "### Recent Circle Posts\nNo recent posts found.",
            [],
            [],
        ),
    )
    monkeypatch.setattr(
        heartbeat,
        "gather_email_drafts_context",
        lambda: "### Recent Emails for Draft Consideration\nNo unreplied emails needing attention.",
    )
    monkeypatch.setattr(heartbeat, "reconcile_active_drafts", lambda *_args: "No active drafts to reconcile.")
    monkeypatch.setattr(heartbeat, "expire_old_drafts", lambda: 0)
    monkeypatch.setattr(heartbeat, "gather_active_drafts_context", lambda: "No active drafts pending review.")
    monkeypatch.setattr(
        heartbeat,
        "_assemble_heartbeat_cognition_section",
        lambda _memory_dir: "## Shared Proactive Brief\n\nNo special context.",
    )
    monkeypatch.setattr(heartbeat, "load_state", lambda _path: {"alert_history": []})
    monkeypatch.setattr(heartbeat, "prune_expired_alerts", lambda _state: [])
    monkeypatch.setattr(heartbeat, "save_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(heartbeat, "append_to_daily_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(heartbeat, "log_hook_execution", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(heartbeat, "run_with_runtime_lanes", fake_run_with_runtime_lanes)

    return captured_requests


@pytest.mark.asyncio
async def test_quiet_heartbeat_still_invokes_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HEARTBEAT_CODEX_MODEL", raising=False)
    captured_requests = _install_quiet_heartbeat(monkeypatch)

    result = await heartbeat.run_heartbeat(test_mode=True)

    assert result is None
    assert len(captured_requests) == 1
    request = captured_requests[0]
    assert request.task_name == "heartbeat"
    assert request.capability == TOOL_REASONING
    assert request.fallback_model == "gpt-5.4-mini"
    assert "No urgent emails." in request.prompt
    assert "No active drafts pending review." in request.prompt


@pytest.mark.asyncio
async def test_heartbeat_model_override_does_not_change_chat_model_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SECOND_BRAIN_CODEX_MODEL", "gpt-5.5")
    monkeypatch.delenv("HEARTBEAT_CODEX_MODEL", raising=False)
    captured_requests = _install_quiet_heartbeat(monkeypatch)

    await heartbeat.run_heartbeat(test_mode=True)

    assert captured_requests[0].fallback_model == "gpt-5.4-mini"
    assert __import__("os").environ["SECOND_BRAIN_CODEX_MODEL"] == "gpt-5.5"


@pytest.mark.asyncio
async def test_heartbeat_model_override_can_be_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HEARTBEAT_CODEX_MODEL", "gpt-5.4-nano")
    captured_requests = _install_quiet_heartbeat(monkeypatch)

    await heartbeat.run_heartbeat(test_mode=True)

    assert captured_requests[0].fallback_model == "gpt-5.4-nano"
