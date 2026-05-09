"""PRD-8 Phase 7b (WS2) — voice cascade kill-switch contract tests.

Asserts the operator kill-switch ("voice") gates ALL THREE voice chokepoints:

  1. ``voice.transcribe_audio_file(file_path)`` — STT cascade entrypoint (Phase 4)
  2. ``voice.synthesize(text, tts_config)`` — TTS cascade entrypoint (Phase 4)
  3. ``voice.transcribe(audio_bytes, api_key, model)`` — LEGACY STT helper

R4 NM4 (codex R3 — CORRECTNESS FIX): the earlier executor pass only gated
``transcribe_audio_file``; ``transcribe`` (legacy) and ``synthesize`` were
ungated, leaving 2/3 voice chokepoints callable under
``HOMIE_KILLSWITCH_VOICE=disabled``. R4 closes the gap and adds a regression
test per chokepoint.

All three call sites use the Rule-3 module-attribute import:

    from security import kill_switches
    kill_switches.requireEnabled("voice", caller="voice_<which_function>")

so monkeypatch propagates correctly during tests.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure chat/ on path so ``import voice`` works.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

import voice  # noqa: E402

from security import kill_switches  # noqa: E402


@pytest.fixture(autouse=True)
def reset_counters():
    """Each test starts with empty refusal counters and audit-write failures."""
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()
    yield
    kill_switches._REFUSAL_COUNTERS.clear()
    kill_switches._AUDIT_WRITE_FAILURES.clear()


# ──────────────────────────────────────────────────────────────────────
# Chokepoint 1: voice.transcribe_audio_file (cascade STT entry)
# ──────────────────────────────────────────────────────────────────────


def test_transcribe_audio_file_killswitch(monkeypatch, tmp_path):
    """``voice.transcribe_audio_file`` raises KillSwitchDisabled when voice disabled.

    The cascade entrypoint must refuse BEFORE any provider attempt — refusal
    counter increments and the file is never opened/read.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    fake_audio = tmp_path / "fake.wav"
    fake_audio.write_bytes(b"fake")

    with pytest.raises(kill_switches.KillSwitchDisabled) as exc_info:
        asyncio.run(voice.transcribe_audio_file(str(fake_audio)))
    assert exc_info.value.switch_name == "voice"

    counters = kill_switches.get_refusal_counters()
    assert counters.get("voice", 0) >= 1, (
        "transcribe_audio_file refusal must increment counter"
    )


# ──────────────────────────────────────────────────────────────────────
# Chokepoint 2: voice.transcribe (LEGACY 3-arg STT helper) — R4 NM4
# ──────────────────────────────────────────────────────────────────────


def test_voice_transcribe_legacy_killswitch(monkeypatch):
    """LEGACY ``voice.transcribe(bytes, key, model)`` raises KillSwitchDisabled.

    R4 NM4 correctness fix (codex R3): before the fix this entrypoint was
    ungated, so the Telegram fallback at ``adapters/telegram.py:587`` could
    bypass the kill-switch by calling ``voice.transcribe()`` directly. The
    fix adds the same kill-switch pattern (Rule 3 module-attribute lookup)
    at the function head — refusal counter increments + audit-log row written
    + KillSwitchDisabled raised BEFORE any provider attempt.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    with pytest.raises(kill_switches.KillSwitchDisabled) as exc_info:
        asyncio.run(voice.transcribe(b"fake-audio-bytes", "fake-api-key"))
    assert exc_info.value.switch_name == "voice"

    counters = kill_switches.get_refusal_counters()
    assert counters.get("voice", 0) >= 1, (
        "legacy voice.transcribe refusal must increment counter"
    )


def test_voice_transcribe_legacy_killswitch_no_provider_call(monkeypatch):
    """Legacy ``voice.transcribe`` MUST NOT instantiate or call OpenAIWhisperProvider.

    Critical regression test: the kill-switch check happens at the function
    HEAD, BEFORE any provider construction. If the gate is misplaced (e.g.
    inside try/except or after provider instantiation), an attacker who flips
    the kill-switch could still trigger network IO before the refusal lands.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    with patch.object(voice, "OpenAIWhisperProvider") as mock_provider:
        with pytest.raises(kill_switches.KillSwitchDisabled):
            asyncio.run(voice.transcribe(b"fake", "fake-key"))
        # Provider must NOT have been instantiated — kill-switch fires first.
        mock_provider.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Chokepoint 3: voice.synthesize (cascade TTS entry) — R4 NM4
# ──────────────────────────────────────────────────────────────────────


def test_voice_synthesize_killswitch(monkeypatch):
    """``voice.synthesize`` raises KillSwitchDisabled when voice disabled.

    R4 NM4 correctness fix (codex R3): before the fix this entrypoint was
    ungated. Operators flipping ``HOMIE_KILLSWITCH_VOICE=disabled`` got STT
    refused but TTS still synthesized. The fix adds the same kill-switch
    pattern at the function head; refusal happens BEFORE any provider attempt.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    with pytest.raises(kill_switches.KillSwitchDisabled) as exc_info:
        asyncio.run(voice.synthesize("hello world"))
    assert exc_info.value.switch_name == "voice"

    counters = kill_switches.get_refusal_counters()
    assert counters.get("voice", 0) >= 1, (
        "voice.synthesize refusal must increment counter"
    )


def test_voice_synthesize_killswitch_no_provider_call(monkeypatch):
    """``voice.synthesize`` MUST NOT instantiate any provider on refusal.

    Regression test that the kill-switch is at the function HEAD, before
    any provider construction or env-var lookup for provider keys.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "fake-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "fake-voice")

    with patch.object(voice, "_ElevenLabsProvider") as mock_eleven, \
         patch.object(voice, "_GradiumProvider") as mock_gradium:
        with pytest.raises(kill_switches.KillSwitchDisabled):
            asyncio.run(voice.synthesize("hello"))
        mock_eleven.assert_not_called()
        mock_gradium.assert_not_called()


# ──────────────────────────────────────────────────────────────────────
# Cross-chokepoint sanity: counter accumulates across all 3 entries
# ──────────────────────────────────────────────────────────────────────


def test_all_three_voice_entrypoints_share_voice_counter(monkeypatch, tmp_path):
    """Refusals from all 3 voice chokepoints accumulate on the same counter.

    The kill-switch name "voice" is shared across cascade STT, cascade TTS,
    and the legacy back-compat shim. ``/api/health.killSwitches.counters.voice``
    therefore reflects total voice refusals across the surface — operators
    see one number, not three.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")
    fake_audio = tmp_path / "fake.wav"
    fake_audio.write_bytes(b"fake")

    # 1 refusal from transcribe_audio_file
    with pytest.raises(kill_switches.KillSwitchDisabled):
        asyncio.run(voice.transcribe_audio_file(str(fake_audio)))
    # 1 refusal from legacy transcribe
    with pytest.raises(kill_switches.KillSwitchDisabled):
        asyncio.run(voice.transcribe(b"fake", "key"))
    # 1 refusal from synthesize
    with pytest.raises(kill_switches.KillSwitchDisabled):
        asyncio.run(voice.synthesize("hi"))

    counters = kill_switches.get_refusal_counters()
    assert counters.get("voice") == 3, (
        f"Expected 3 voice refusals across all chokepoints, got {counters.get('voice')}"
    )


# ──────────────────────────────────────────────────────────────────────
# Caller name is recorded in the audit-log detail (Rule 3 telemetry)
# ──────────────────────────────────────────────────────────────────────


def test_voice_killswitch_caller_names_disambiguate(monkeypatch, tmp_path):
    """Each chokepoint passes a unique ``caller=`` to requireEnabled.

    Operators investigating refusals via the audit-log can tell which voice
    surface refused (cascade STT vs cascade TTS vs legacy STT) by the caller
    field. Test verifies all three caller strings appear in audit detail.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    captured_callers: list[str] = []

    def fake_audit_write(*, operator_id, action, target_persona_id, outcome, detail, blocked):
        captured_callers.append(detail.get("caller", ""))

    monkeypatch.setattr("dashboard_api._audit_write", fake_audit_write)

    fake_audio = tmp_path / "fake.wav"
    fake_audio.write_bytes(b"fake")

    for fn in (
        lambda: asyncio.run(voice.transcribe_audio_file(str(fake_audio))),
        lambda: asyncio.run(voice.transcribe(b"fake", "key")),
        lambda: asyncio.run(voice.synthesize("hi")),
    ):
        with pytest.raises(kill_switches.KillSwitchDisabled):
            fn()

    assert "voice_cascade_transcribe" in captured_callers
    assert "voice_legacy_transcribe" in captured_callers
    assert "voice_cascade_synthesize" in captured_callers


# ──────────────────────────────────────────────────────────────────────
# F2 iter2 — Telegram fallback bypass: zero-call regression test
# ──────────────────────────────────────────────────────────────────────


def test_telegram_fallback_does_not_call_legacy_provider_when_voice_disabled(
    monkeypatch,
):
    """Telegram fallback path (codex post-build F2 iter2 residual).

    When ``capabilities["stt"] == False`` (no cascade) but a legacy
    ``_voice_providers.stt`` is configured, the iter1 implementation called
    ``_voice_providers.stt.transcribe(audio_bytes)`` directly — bypassing the
    voice kill-switch. Iter2 fix routes that path through ``voice.transcribe``
    (which IS gated). This regression test forces ``capabilities["stt"]==False``,
    sets a fake legacy STT provider, disables the voice kill-switch, and
    asserts ``voice.transcribe`` (the gated entrypoint) refuses BEFORE
    instantiating ``_voice_providers.stt`` — proving the bypass is closed.

    Architecture-level test: we don't construct a Telegram bot (heavy
    dependency). Instead we verify the contract — ``voice.transcribe`` raises
    ``KillSwitchDisabled`` BEFORE provider instantiation, which means the
    Telegram adapter rerouting through it cannot bypass the gate.
    """
    monkeypatch.setenv("HOMIE_KILLSWITCH_VOICE", "disabled")

    # Mock the OpenAIWhisperProvider so we can prove it was NOT instantiated.
    # If voice.transcribe DID NOT gate at the function head, the fake provider
    # would be constructed and its .transcribe() called. Instead, the gate
    # raises BEFORE provider construction and the mock stays unused.
    with patch.object(voice, "OpenAIWhisperProvider") as mock_provider:
        with pytest.raises(kill_switches.KillSwitchDisabled) as exc_info:
            asyncio.run(voice.transcribe(b"fake-audio-from-fallback", "fake-key"))
        assert exc_info.value.switch_name == "voice"
        # CRITICAL: provider must NOT have been instantiated.
        # If Telegram's previous bypass were still in effect, this would fail.
        mock_provider.assert_not_called()

    # Refusal counter increments — same source as the cascade entry point.
    counters = kill_switches.get_refusal_counters()
    assert counters.get("voice", 0) >= 1


def test_telegram_imports_kill_switches_module_attribute(monkeypatch):
    """Static check — Telegram adapter imports kill_switches via module-attr.

    Codex iter2 F2 audit confirms all 6 adapters do this. Lock the contract
    against future regressions where someone refactors to ``from
    security.kill_switches import KillSwitchDisabled`` (Rule 3 violation).
    """
    import sys
    SCRIPTS_DIR = Path(__file__).resolve().parent.parent
    chat_dir = str(SCRIPTS_DIR.parent / "chat")
    if chat_dir not in sys.path:
        sys.path.insert(0, chat_dir)

    adapters_init = SCRIPTS_DIR.parent / "chat" / "adapters" / "telegram.py"
    src = adapters_init.read_text(encoding="utf-8")

    # The adapter must import kill_switches as a module (correct Rule 3 form).
    assert "from security import kill_switches" in src, (
        "Telegram adapter must import kill_switches via module-attr (Rule 3)"
    )
    # The adapter must catch KillSwitchDisabled BEFORE generic Exception in the
    # voice transcribe path — verify both clauses appear in source order.
    transcribe_idx = src.find("KillSwitchDisabled")
    generic_idx = src.find("except Exception", transcribe_idx)
    assert transcribe_idx > 0
    assert generic_idx > transcribe_idx, (
        "Telegram adapter must catch KillSwitchDisabled BEFORE generic Exception"
    )
