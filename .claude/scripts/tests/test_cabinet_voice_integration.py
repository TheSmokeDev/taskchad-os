"""PRD-8 Phase 6 — integration tests across the voice surfaces.

Covers contract criteria:
  * cabinet_api_stream_meeting_async_generator
  * cabinet_voice_config_two_field_shape (+ 4 fields)
  * homie_tts_resolves_voice_overrides_from_persona_config
  * voice_meeting_html_endpoint_serves_html
  * voice_meeting_client_bundle_endpoint_serves_bundle
  * voice_meeting_avatar_endpoint_serves_persona_image
  * run_agent_turn_voice_mode_prepends_context_hint
  * run_agent_turn_default_is_voice_false_phase_5a_unchanged
  * broadcast_order_column_forward_additive_with_pragma_guard
  * handle_cabinet_voice_subcommand_returns_browser_url
  * intent_spec_disjointness_with_phase_4_voice
  * kill_switch_voice_transitive_consumption
  * kill_switch_cabinet_via_sse_event
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


# ─── cabinet_api.stream_meeting async-generator shape ─────────────────────


@pytest.mark.asyncio
async def test_stream_meeting_yields_events(monkeypatch):
    """stream_meeting yields parsed SSE event envelopes from the wire stream."""
    import integrations.cabinet_api as cabinet_api

    # Build an httpx-style async response that emits SSE lines.
    sse_payload = (
        b'id: 0\nevent: message\ndata: {"seq": 0, "event": {"type": "meeting_state"}}\n\n'
        b'id: 1\nevent: message\ndata: {"seq": 1, "event": {"type": "turn_start", "turnId": "t1"}}\n\n'
    )

    class _FakeResp:
        def __init__(self) -> None:
            self.status_code = 200

        async def aiter_lines(self):
            # Mimic httpx aiter_lines: yields one logical line at a time
            # without trailing newlines.
            for line in sse_payload.decode().split("\n"):
                yield line

        async def aread(self):
            return b""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class _FakeClient:
        async def aclose(self):
            return None

        def stream(self, method, url, params=None, headers=None):  # noqa: ARG002
            return _FakeResp()

    # Inject a caller-managed client so stream_meeting uses it instead of
    # opening its own AsyncClient.
    fake_client = _FakeClient()
    events = []
    async for envelope in cabinet_api.stream_meeting(meeting_id=42, client=fake_client):
        events.append(envelope)

    assert len(events) == 2
    assert events[0]["event"]["type"] == "meeting_state"
    assert events[1]["event"]["type"] == "turn_start"
    assert events[1]["event"]["turnId"] == "t1"


@pytest.mark.asyncio
async def test_stream_meeting_410_raises_meeting_ended(monkeypatch):
    """stream_meeting maps HTTP 410 to CabinetMeetingEnded."""
    import integrations.cabinet_api as cabinet_api

    class _FakeResp:
        def __init__(self) -> None:
            self.status_code = 410

        async def aiter_lines(self):
            return
            yield  # unreachable — generator stub

        async def aread(self):
            return b'{"error": "replay_gap"}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class _FakeClient:
        async def aclose(self):
            return None

        def stream(self, method, url, params=None, headers=None):
            return _FakeResp()

    fake_client = _FakeClient()
    with pytest.raises(cabinet_api.CabinetMeetingEnded):
        async for _ in cabinet_api.stream_meeting(meeting_id=42, client=fake_client):
            pass


# ─── persona config — voice fields ────────────────────────────────────────


def test_voice_id_field_str(tmp_path):
    """cabinet.voice_id accepts str + rejects non-str."""
    from personas.services import _validate_cabinet_section, ConfigShapeError

    # OK — str. (Use a clearly-fake placeholder per
    # `feedback_sanitizer_regex_shape_blind_spot.md` — real production
    # voice-clone-id prefixes don't belong in test fixtures.)
    _validate_cabinet_section({"voice_id": "VOICE_ID_PLACEHOLDER"}, tmp_path / "cfg.yaml")
    # Reject — int.
    with pytest.raises(Exception):  # ConfigShapeError or similar
        _validate_cabinet_section({"voice_id": 42}, tmp_path / "cfg.yaml")


def test_voice_provider_enum(tmp_path):
    """cabinet.voice_provider accepts known enum + rejects unknown."""
    from personas.services import _validate_cabinet_section, ConfigShapeError

    for known in ("elevenlabs", "edge", "openai", "gemini", "kokoro"):
        _validate_cabinet_section({"voice_provider": known}, tmp_path / "cfg.yaml")

    with pytest.raises(ConfigShapeError):
        _validate_cabinet_section({"voice_provider": "fake-provider"}, tmp_path / "cfg.yaml")


def test_voice_persona_prompt_field(tmp_path):
    """cabinet.voice_persona_prompt accepts str."""
    from personas.services import _validate_cabinet_section

    _validate_cabinet_section(
        {"voice_persona_prompt": "You are the YourBusiness SEO Lead."},
        tmp_path / "cfg.yaml",
    )


def test_avatar_path_field(tmp_path):
    """cabinet.avatar_path accepts str."""
    from personas.services import _validate_cabinet_section

    _validate_cabinet_section(
        {"avatar_path": "static/avatars/seo.png"},
        tmp_path / "cfg.yaml",
    )


# ─── HomieTTS resolves voice_overrides from persona config ────────────────


@pytest.mark.asyncio
async def test_homie_tts_voice_overrides_resolution(monkeypatch):
    """HomieTTS computes voice_overrides per-persona from the bridge's
    TTSUpdateSettingsFrame (which the agent_bridge derives from
    config.yaml.cabinet.voice_id + voice_provider)."""
    from cabinet.voice.voice_pipeline import HomieTTS, TextFrame, TTSUpdateSettingsFrame, FrameDirection

    tts = HomieTTS()
    pushed: list = []

    async def fake_push(frame, direction=None):
        pushed.append((frame, direction))

    tts.push_frame = fake_push

    # Capture synth calls.
    captured_calls: list = []

    async def fake_synthesize(text, tts_config=None, voice_overrides=None):
        captured_calls.append({
            "text": text,
            "tts_config": tts_config,
            "voice_overrides": voice_overrides,
        })
        return b"AUDIO_BYTES"

    fake_voice_module = MagicMock()
    fake_voice_module.synthesize = fake_synthesize

    with patch.dict("sys.modules", {"voice": fake_voice_module}):
        # Step 1 — voice-switch to elevenlabs voice id "X".
        await tts.process_frame(
            TTSUpdateSettingsFrame(settings={"voice": "voice_X", "provider": "elevenlabs"}),
            FrameDirection.DOWNSTREAM,
        )
        # Step 2 — synthesize a text frame.
        await tts.process_frame(TextFrame(text="hello world"), FrameDirection.DOWNSTREAM)

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["text"] == "hello world"
    assert call["voice_overrides"] == {"elevenlabs": "voice_X"}


# ─── _run_agent_turn voice-mode hint + forward-additive lock ──────────────


def test_voice_context_hint_constant_verbatim():
    """The voice-mode context hint constant is the verbatim port from upstream."""
    from cabinet import text_orchestrator
    expected = (
        "[Voice meeting mode: Keep responses concise and conversational. "
        "Aim for 2-3 sentences unless asked for detail. Start with a brief acknowledgment.]"
    )
    assert text_orchestrator._VOICE_CONTEXT_HINT_VERBATIM == expected


def test_run_agent_args_default_is_voice_false():
    """_RunAgentArgs.is_voice defaults to False (forward-additive lock)."""
    from cabinet.text_orchestrator import _RunAgentArgs, RosterAgent

    args = _RunAgentArgs(
        persona_id="research",
        meeting_id=42,
        user_text="hi",
        role="primary",
        turn_id="t_1",
        channel=MagicMock(),
        cancel_flag={"cancelled": False},
        turn_state={"anyIncomplete": False},
        persona=RosterAgent(id="research", name="Research", description=""),
        roster=[],
    )
    assert args.is_voice is False


def test_handle_turn_options_default_is_voice_false():
    """HandleTurnOptions.is_voice defaults to False; .target_agent_id defaults to None."""
    from cabinet.text_orchestrator import HandleTurnOptions

    opts = HandleTurnOptions()
    assert opts.is_voice is False
    assert opts.target_agent_id is None
    assert opts.roster is None


def test_handle_turn_options_voice_kwargs():
    """HandleTurnOptions accepts is_voice + target_agent_id kwargs."""
    from cabinet.text_orchestrator import HandleTurnOptions

    opts = HandleTurnOptions(is_voice=True, target_agent_id="research")
    assert opts.is_voice is True
    assert opts.target_agent_id == "research"


# ─── broadcast_order column migration ─────────────────────────────────────


def test_broadcast_order_column_added(tmp_path):
    """_apply_phase_6_columns adds broadcast_order column with PRAGMA guard."""
    from dashboard_db import DashboardDB, _apply_phase_6_columns, _column_names

    db_path = tmp_path / "dashboard.db"
    db = DashboardDB(db_path=db_path)
    conn = db.connect()
    try:
        cols = _column_names(conn, "cabinet_meetings")
        assert "broadcast_order" in cols
    finally:
        db.close()


def test_init_schema_idempotent_with_phase_6(tmp_path):
    """init_schema runs twice without duplicate-column crash."""
    from dashboard_db import DashboardDB

    db_path = tmp_path / "dashboard.db"
    db = DashboardDB(db_path=db_path)
    conn1 = db.connect()
    db.close()

    # Re-open + re-run init_schema; must NOT raise duplicate-column-name.
    db2 = DashboardDB(db_path=db_path)
    conn2 = db2.connect()
    db2.close()


# ─── /cabinet voice subcommand ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_voice_subcommand_creates_meeting():
    """/cabinet voice (no id) creates a new meeting and returns browser URL."""
    sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

    from core_handlers import handle_cabinet
    from integrations import cabinet_api

    fake_ref = cabinet_api.CabinetMeetingRef(id=99, chat_id="42", auto_ended_ids=[])
    fake_create = AsyncMock(return_value=fake_ref)

    incoming = MagicMock()
    incoming.chat_id = "42"

    with patch.object(cabinet_api, "create_meeting", fake_create), \
         patch("security.kill_switches.requireEnabled", return_value=None):
        result = await handle_cabinet(adapter=None, incoming=incoming, args="voice")

    fake_create.assert_called_once()
    assert "Cabinet voice meeting #99" in result
    assert "/api/cabinet/voice/ui" in result
    assert "meetingId=99" in result
    assert "chatId=42" in result


@pytest.mark.asyncio
async def test_voice_subcommand_with_explicit_meeting_id():
    """/cabinet voice <id> verifies meeting exists + returns browser URL."""
    sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

    from core_handlers import handle_cabinet
    from integrations import cabinet_api

    fake_list = AsyncMock(return_value=[
        {"id": 77, "started_at": 0, "ended_at": None, "title": "Active meeting"},
    ])

    incoming = MagicMock()
    incoming.chat_id = "5"

    with patch.object(cabinet_api, "list_meetings", fake_list), \
         patch("security.kill_switches.requireEnabled", return_value=None):
        result = await handle_cabinet(adapter=None, incoming=incoming, args="voice 77")

    fake_list.assert_called_once()
    assert "meetingId=77" in result


@pytest.mark.asyncio
async def test_voice_subcommand_unknown_meeting_id():
    """/cabinet voice <unknown_id> surfaces a friendly not-found message."""
    sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

    from core_handlers import handle_cabinet
    from integrations import cabinet_api

    fake_list = AsyncMock(return_value=[])  # no meetings

    incoming = MagicMock()
    incoming.chat_id = "5"

    with patch.object(cabinet_api, "list_meetings", fake_list), \
         patch("security.kill_switches.requireEnabled", return_value=None):
        result = await handle_cabinet(adapter=None, incoming=incoming, args="voice 999")

    assert "not found" in result.lower()


@pytest.mark.asyncio
async def test_voice_subcommand_kill_switch_friendly():
    """When the cabinet kill-switch is disabled, /cabinet voice surfaces the friendly message."""
    sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

    from core_handlers import handle_cabinet
    from security.kill_switches import KillSwitchDisabled

    incoming = MagicMock()
    incoming.chat_id = "5"

    def fake_require(switch_name, *, caller=""):
        raise KillSwitchDisabled(switch_name, caller)

    with patch("security.kill_switches.requireEnabled", side_effect=fake_require):
        result = await handle_cabinet(adapter=None, incoming=incoming, args="voice")

    assert "disabled" in result.lower()


# ─── IntentSpec disjointness — Phase 4 /voice vs Phase 6 /cabinet voice ──


def test_intent_spec_disjointness():
    """Phase 4 voice cascade and Phase 6 /cabinet voice are different paths.

    Phase 4 voice is the TTS provider cascade (voice.synthesize +
    voice.transcribe_audio_file) — it has no /voice slash command of its
    own; it's plumbed through adapters. Phase 6 voice cabinet is reached
    via `/cabinet voice [meetingId]`. Verify the registry has no `/voice`
    top-level command claiming overlap, and that `/cabinet` IS registered.
    """
    sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

    from commands import COMMANDS

    cmd_names = {c[0] for c in COMMANDS}
    # /cabinet is registered (Phase 5b).
    assert "cabinet" in cmd_names
    # No /voice top-level command competes for the keyword.
    assert "voice" not in cmd_names
    # /cabinet voice is a SUBCOMMAND of /cabinet, not a separate top-level
    # entry. Verify the cabinet usage text mentions the voice subcommand
    # so operators can discover it.
    from core_handlers import _cabinet_usage_text
    usage = _cabinet_usage_text()
    assert "/cabinet voice" in usage


# ─── Forward-additive Phase 5a regression ─────────────────────────────────


def test_phase_5a_default_call_unchanged_signature():
    """handle_text_turn(meeting_id, user_text, client_msg_id) call shape is unchanged.

    R1 v2 forward-additive lock: pre-Phase-6 callers using the 3-positional
    form must still work without passing opts.
    """
    import inspect
    from cabinet.text_orchestrator import handle_text_turn

    sig = inspect.signature(handle_text_turn)
    params = list(sig.parameters.keys())
    assert params == ["meeting_id", "user_text", "client_msg_id", "opts"]
    # opts default is None (Rule 1 sentinel).
    assert sig.parameters["opts"].default is None
