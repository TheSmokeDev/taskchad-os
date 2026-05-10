"""PRD-8 Phase 6 / WS0 — voice.synthesize voice_overrides backport tests.

Verifies the new ``voice_overrides: dict[str, str] | None = None`` kwarg added
to ``voice.synthesize`` per Phase 6 §"WS0 — Phase 4 voice_overrides backport".
This is a forward-additive Phase 4 extension: voice_overrides=None (default)
preserves existing Phase 4 behavior verbatim.

Cabinet voice (Phase 6) HomieTTS computes ``voice_overrides`` per persona turn
from ``<profile>/config.yaml.cabinet.voice_id`` + ``cabinet.voice_provider`` so
voice persona X speaks with voice id A and persona Y speaks with voice id B in
the same WebSocket session.

Coverage:
  * Signature accepts the new kwarg (test_synthesize_signature_voice_overrides_kwarg)
  * voice_overrides=None preserves Phase 4 behavior
    (test_synthesize_voice_overrides_none_preserves_phase_4_behavior)
  * voice_overrides={} preserves Phase 4 behavior (sentinel-equivalent)
  * ElevenLabs reads voice_id from voice_overrides["elevenlabs"]
    (test_synthesize_voice_overrides_elevenlabs)
  * Edge reads voice from voice_overrides["edge"]
    (test_edge_provider_voice_override)
  * voice_overrides has only "elevenlabs" key, ELEVENLABS_API_KEY unset →
    cascade falls to Edge with env default voice (graceful fallback)
    (test_synthesize_voice_overrides_edge_fallback)
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

import voice  # noqa: E402


# ─── Signature ────────────────────────────────────────────────────────────


def test_synthesize_signature_voice_overrides_kwarg():
    """``voice.synthesize`` accepts ``voice_overrides`` kwarg with sentinel default.

    Required by contract criterion ``phase_4_synthesize_accepts_voice_overrides_kwarg``.
    """
    sig = inspect.signature(voice.synthesize)
    assert "voice_overrides" in sig.parameters, (
        "voice.synthesize must accept voice_overrides kwarg per Phase 6 WS0"
    )
    param = sig.parameters["voice_overrides"]
    assert param.default is None, (
        "voice_overrides default must be None (Rule 1 sentinel — resolve at call time)"
    )


def test_synthesize_signature_three_kwargs():
    """Full signature is ``synthesize(text, tts_config=None, voice_overrides=None)``."""
    sig = inspect.signature(voice.synthesize)
    params = list(sig.parameters.keys())
    assert params == ["text", "tts_config", "voice_overrides"], (
        f"Expected exact signature, got {params}"
    )


# ─── Forward-additive lock — None / {} preserve Phase 4 behavior ─────────


@pytest.mark.asyncio
async def test_synthesize_voice_overrides_none_preserves_phase_4_behavior():
    """voice_overrides=None (default) — ElevenLabs called with env voice id verbatim.

    Forward-additive lock: Phase 4 callers passing ``synthesize(text)`` or
    ``synthesize(text, tts_config)`` must produce IDENTICAL behavior to
    pre-WS0. Required by contract criterion ``phase_4_voice_overrides_backward_compat``.
    """
    # Phase 4 ElevenLabs default: env-driven voice_id.
    sentinel_audio = b"\xfa\xfb\xfc\xfdENV_VOICE_AUDIO"

    # Mock kill-switch (always allow), env, and ElevenLabs provider.
    fake_provider_constructor = MagicMock()
    fake_instance = MagicMock()
    fake_instance.synthesize = AsyncMock(return_value=sentinel_audio)
    fake_provider_constructor.return_value = fake_instance

    env = {
        "ELEVENLABS_API_KEY": "k_env",
        "ELEVENLABS_VOICE_ID": "v_env_default",
    }
    with patch.dict(os.environ, env, clear=True), \
         patch("voice._ElevenLabsProvider", fake_provider_constructor), \
         patch("security.kill_switches.requireEnabled", return_value=None):
        # Both signatures must work and produce env-default behavior.
        result_2arg = await voice.synthesize("hello world")
        result_3arg = await voice.synthesize("hello world", None)
        result_explicit_none = await voice.synthesize("hello world", None, None)

    assert result_2arg == sentinel_audio
    assert result_3arg == sentinel_audio
    assert result_explicit_none == sentinel_audio
    # All 3 calls used the env-default voice_id.
    for call_args in fake_provider_constructor.call_args_list:
        assert call_args.kwargs.get("voice_id") == "v_env_default", (
            f"Expected env-default voice_id, got {call_args}"
        )


@pytest.mark.asyncio
async def test_synthesize_voice_overrides_empty_dict_preserves_phase_4_behavior():
    """voice_overrides={} — empty dict equivalent to None (sentinel-equivalent)."""
    sentinel_audio = b"\xea\xeb\xec\xedEMPTY_DICT_AUDIO"

    fake_provider_constructor = MagicMock()
    fake_instance = MagicMock()
    fake_instance.synthesize = AsyncMock(return_value=sentinel_audio)
    fake_provider_constructor.return_value = fake_instance

    env = {
        "ELEVENLABS_API_KEY": "k_env",
        "ELEVENLABS_VOICE_ID": "v_env_default",
    }
    with patch.dict(os.environ, env, clear=True), \
         patch("voice._ElevenLabsProvider", fake_provider_constructor), \
         patch("security.kill_switches.requireEnabled", return_value=None):
        result = await voice.synthesize("hi", None, {})

    assert result == sentinel_audio
    fake_provider_constructor.assert_called_once()
    assert fake_provider_constructor.call_args.kwargs.get("voice_id") == "v_env_default"


# ─── Per-provider override paths ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_voice_overrides_elevenlabs():
    """voice_overrides={'elevenlabs': X} — ElevenLabs constructor receives override voice_id.

    The override beats env. Required by contract criterion
    ``phase_4_synthesize_accepts_voice_overrides_kwarg``.
    """
    sentinel_audio = b"\xab\xcd\xefELEVEN_OVERRIDE_AUDIO"
    override_voice = "EXAMPLE_VOICE_ID_OVERRIDE"

    fake_provider_constructor = MagicMock()
    fake_instance = MagicMock()
    fake_instance.synthesize = AsyncMock(return_value=sentinel_audio)
    fake_provider_constructor.return_value = fake_instance

    env = {
        "ELEVENLABS_API_KEY": "k_real",
        "ELEVENLABS_VOICE_ID": "v_env_should_be_ignored",
    }
    with patch.dict(os.environ, env, clear=True), \
         patch("voice._ElevenLabsProvider", fake_provider_constructor), \
         patch("security.kill_switches.requireEnabled", return_value=None):
        result = await voice.synthesize(
            "speak this",
            None,
            voice_overrides={"elevenlabs": override_voice},
        )

    assert result == sentinel_audio
    fake_provider_constructor.assert_called_once()
    assert fake_provider_constructor.call_args.kwargs.get("voice_id") == override_voice, (
        f"Expected override {override_voice}, got {fake_provider_constructor.call_args}"
    )
    assert fake_provider_constructor.call_args.kwargs.get("api_key") == "k_real"


@pytest.mark.asyncio
async def test_edge_provider_voice_override():
    """voice_overrides={'edge': X} — Edge constructor receives override voice.

    Required by contract criterion ``phase_4_voice_overrides_non_elevenlabs_branch_test``.
    """
    sentinel_audio = b"EDGE_OVERRIDE_AUDIO"
    override_voice = "en-US-AriaNeural"

    fake_edge_constructor = MagicMock()
    fake_edge_instance = MagicMock()
    fake_edge_instance.synthesize = AsyncMock(return_value=sentinel_audio)
    fake_edge_constructor.return_value = fake_edge_instance

    # Strip earlier providers' env so Edge actually fires.
    with patch.dict(os.environ, {}, clear=True), \
         patch("voice.EdgeTtsProvider", fake_edge_constructor), \
         patch("voice._edge_tts_installed", return_value=True), \
         patch("security.kill_switches.requireEnabled", return_value=None):
        result = await voice.synthesize(
            "edge speech",
            None,
            voice_overrides={"edge": override_voice},
        )

    assert result == sentinel_audio
    fake_edge_constructor.assert_called_once()
    assert fake_edge_constructor.call_args.kwargs.get("voice") == override_voice


@pytest.mark.asyncio
async def test_synthesize_voice_overrides_edge_fallback():
    """voice_overrides has only 'elevenlabs' + ELEVENLABS_API_KEY unset → cascade
    falls to Edge with env-default voice (graceful fallback).

    Verifies the cascade keeps marching when an override targets a provider that
    isn't configured: only the matching provider consumes its own override; other
    providers continue to read env defaults.
    """
    edge_audio = b"EDGE_FALLBACK_AUDIO"

    fake_edge_constructor = MagicMock()
    fake_edge_instance = MagicMock()
    fake_edge_instance.synthesize = AsyncMock(return_value=edge_audio)
    fake_edge_constructor.return_value = fake_edge_instance

    env = {
        # ELEVENLABS_API_KEY intentionally absent so ElevenLabs branch skips.
        "EDGE_TTS_VOICE": "en-US-GuyNeural-DEFAULT",
    }
    with patch.dict(os.environ, env, clear=True), \
         patch("voice.EdgeTtsProvider", fake_edge_constructor), \
         patch("voice._edge_tts_installed", return_value=True), \
         patch("security.kill_switches.requireEnabled", return_value=None):
        # Override targets ElevenLabs but the cascade should march to Edge.
        result = await voice.synthesize(
            "fallback speech",
            None,
            voice_overrides={"elevenlabs": "v_owner_unused"},
        )

    assert result == edge_audio
    # Edge used env default voice (NOT the elevenlabs override key).
    fake_edge_constructor.assert_called_once()
    assert fake_edge_constructor.call_args.kwargs.get("voice") == "en-US-GuyNeural-DEFAULT"


@pytest.mark.asyncio
async def test_synthesize_voice_overrides_gemini():
    """voice_overrides={'gemini': X} — Gemini constructor receives override voice.

    Strip ElevenLabs/Gradium/Mistral env so Gemini branch fires.
    """
    audio = b"GEMINI_OVERRIDE_AUDIO"

    fake_gemini_constructor = MagicMock()
    fake_gemini_instance = MagicMock()
    fake_gemini_instance.synthesize = AsyncMock(return_value=audio)
    fake_gemini_constructor.return_value = fake_gemini_instance

    env = {"GEMINI_API_KEY": "g_key"}
    with patch.dict(os.environ, env, clear=True), \
         patch("voice._GeminiTtsProvider", fake_gemini_constructor), \
         patch("security.kill_switches.requireEnabled", return_value=None):
        result = await voice.synthesize(
            "gemini speech",
            None,
            voice_overrides={"gemini": "Kore"},
        )

    assert result == audio
    fake_gemini_constructor.assert_called_once()
    assert fake_gemini_constructor.call_args.kwargs.get("voice") == "Kore"


@pytest.mark.asyncio
async def test_synthesize_voice_overrides_openai():
    """voice_overrides={'openai': X} — OpenAI constructor receives override voice."""
    audio = b"OPENAI_OVERRIDE_AUDIO"

    fake_constructor = MagicMock()
    fake_instance = MagicMock()
    fake_instance.synthesize = AsyncMock(return_value=audio)
    fake_constructor.return_value = fake_instance

    env = {"OPENAI_API_KEY": "o_key"}
    with patch.dict(os.environ, env, clear=True), \
         patch("voice.OpenAITtsProvider", fake_constructor), \
         patch("security.kill_switches.requireEnabled", return_value=None):
        result = await voice.synthesize(
            "openai speech",
            None,
            voice_overrides={"openai": "shimmer"},
        )

    assert result == audio
    fake_constructor.assert_called_once()
    assert fake_constructor.call_args.kwargs.get("voice") == "shimmer"


# ─── Forward-additive Phase 4 backward-compat lock ────────────────────────


def test_voice_overrides_does_not_affect_non_default_call_pattern():
    """test_voice_cascade.py's signature checks must still pass.

    Sanity test: signature still has ``text`` as first param and accepts the
    Phase 4 2-arg + 3-arg call patterns.
    """
    sig = inspect.signature(voice.synthesize)
    params = list(sig.parameters.keys())
    assert params[0] == "text"
    # voice_overrides default is None (Rule 1).
    assert sig.parameters["voice_overrides"].default is None
    # tts_config default is None (Phase 4 lock — unchanged).
    assert sig.parameters["tts_config"].default is None
