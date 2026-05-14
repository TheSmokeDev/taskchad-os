"""Cabinet participant execution must resolve real Homie profiles."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import config
from cabinet import meeting_channel as channels_mod
from cabinet.text_orchestrator import handle_text_turn
from dashboard_db import get_connection
from runtime.base import RuntimeResult


@pytest.fixture
def tmp_dashboard_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "dashboard.db"
    monkeypatch.setattr(config, "DASHBOARD_DB_PATH", str(db_path))
    conn = get_connection()
    conn.close()
    return db_path


@pytest.fixture
def tmp_homie_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    homie_root = tmp_path / ".homie"
    monkeypatch.setenv("HOMIE_HOME", str(homie_root))
    return homie_root


@pytest.fixture(autouse=True)
def _reset_channels() -> None:
    channels_mod._reset_channels()
    yield
    channels_mod._reset_channels()


def _make_profile(
    homie_root: Path,
    persona_id: str,
    *,
    tools: list[str] | None = None,
) -> Path:
    profile_root = homie_root / "profiles" / persona_id
    memory_dir = profile_root / "memory"
    for subdir in ("run", "skills", "memory"):
        (profile_root / subdir).mkdir(parents=True, exist_ok=True)
    tool_lines = (
        ["  tools:", *[f"    - {tool}" for tool in tools]]
        if tools
        else ["  tools: []"]
    )
    (profile_root / "config.yaml").write_text(
        "\n".join([
            "persona:",
            f"  display_name: {persona_id.title()}",
            f"  role: {persona_id} role marker",
            "cabinet:",
            *tool_lines,
            "",
        ]),
        encoding="utf-8",
    )
    (memory_dir / "SOUL.md").write_text(
        f"# {persona_id.title()} Soul\n{persona_id.upper()}_SOUL_MARKER",
        encoding="utf-8",
    )
    (memory_dir / "MEMORY.md").write_text(
        f"# {persona_id.title()} Memory\n{persona_id.upper()}_MEMORY_MARKER",
        encoding="utf-8",
    )
    return profile_root


def _make_meeting(roster_snapshot: list[dict]) -> int:
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO cabinet_meetings (mode, chat_id, entry_count) VALUES (?, ?, ?)",
            ("text", "test-chat", 1),
        )
        meeting_id = cur.lastrowid
        conn.execute(
            """INSERT INTO cabinet_text_meetings (meeting_id, roster_json)
               VALUES (?, ?)""",
            (meeting_id, json.dumps(roster_snapshot)),
        )
        conn.commit()
        return meeting_id
    finally:
        conn.close()


def _roster(*persona_ids: str) -> list[dict]:
    return [
        {"id": "default", "name": "Main", "description": "host"},
        *[
            {"id": persona_id, "name": persona_id.title(), "description": ""}
            for persona_id in persona_ids
        ],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("persona_id", ["sales", "marketing"])
async def test_named_cabinet_participant_runs_with_target_profile_context(
    tmp_dashboard_db: Path,
    tmp_homie_root: Path,
    persona_id: str,
) -> None:
    profile_root = _make_profile(tmp_homie_root, persona_id)
    meeting_id = _make_meeting(_roster("sales", "marketing"))
    captured_requests: list = []

    async def fake_run(req):
        captured_requests.append(req)
        return RuntimeResult(
            text=f"{persona_id} response",
            runtime_lane="claude_native",
            provider="claude",
            model="haiku",
        )

    with patch(
        "cabinet.text_orchestrator.lane_router.run_with_runtime_lanes",
        side_effect=fake_run,
    ):
        result = await handle_text_turn(
            meeting_id=meeting_id,
            user_text=f"@{persona_id} check in",
            client_msg_id=f"{persona_id}_profile_ctx",
        )

    assert result.accepted is True
    request = next(
        req for req in captured_requests
        if req.metadata and req.metadata.get("persona_id") == persona_id
    )
    assert request.env is not None
    assert request.env["HOMIE_HOME"] == str(profile_root)
    prompt = request.system_prompt or ""
    assert f"You are `{persona_id}` ({persona_id.title()})" in prompt
    assert "Answer directly as this participant only." in prompt
    assert "Do not say you are Main/default" in prompt
    assert "voice War Room setup" in prompt
    assert prompt.index("Cabinet Room Identity Contract") < prompt.index(
        "Cabinet Participant Profile Context"
    )
    assert f"{persona_id.upper()}_SOUL_MARKER" in (request.system_prompt or "")
    assert f"{persona_id.upper()}_MEMORY_MARKER" in (request.system_prompt or "")
    assert request.metadata["system_prompt_source"] == (
        "cabinet_room_identity+profile_context"
    )


@pytest.mark.asyncio
async def test_snapshot_participant_uses_live_profile_tool_allowlist(
    tmp_dashboard_db: Path,
    tmp_homie_root: Path,
) -> None:
    _make_profile(tmp_homie_root, "sales", tools=["Bash"])
    meeting_id = _make_meeting(_roster("sales"))
    captured_requests: list = []

    async def fake_run(req):
        captured_requests.append(req)
        return RuntimeResult(
            text="sales response",
            runtime_lane="claude_native",
            provider="claude",
            model="haiku",
        )

    with patch(
        "cabinet.text_orchestrator.lane_router.run_with_runtime_lanes",
        side_effect=fake_run,
    ):
        result = await handle_text_turn(
            meeting_id=meeting_id,
            user_text="@sales check tools",
            client_msg_id="sales_profile_tools",
        )

    assert result.accepted is True
    request = next(
        req for req in captured_requests
        if req.metadata and req.metadata.get("persona_id") == "sales"
    )
    assert "Bash" in request.allowed_tools
    assert "Bash" not in request.disallowed_tools
    assert request.metadata["tool_policy"]["allowed_count"] > 0


@pytest.mark.asyncio
async def test_stale_snapshot_profile_does_not_execute_as_default(
    tmp_dashboard_db: Path,
    tmp_homie_root: Path,
) -> None:
    del tmp_homie_root  # HOMIE_HOME is set, but no sales profile exists.
    meeting_id = _make_meeting(_roster("sales"))

    async def fake_run(req):
        raise AssertionError(f"stale profile should not dispatch runtime: {req!r}")

    with patch(
        "cabinet.text_orchestrator.lane_router.run_with_runtime_lanes",
        side_effect=fake_run,
    ):
        result = await handle_text_turn(
            meeting_id=meeting_id,
            user_text="@sales status?",
            client_msg_id="stale_sales_profile",
        )

    assert result.accepted is True
    events = [entry.event for entry in channels_mod.get_channel(meeting_id).since(0)]
    error_events = [event for event in events if event.get("type") == "error"]
    assert error_events
    assert error_events[0]["agentId"] == "sales"
    assert error_events[0]["recoverable"] is True
    assert "not runnable" in error_events[0]["message"]
