"""PRD-8 Phase 4 (WS1) — voice.py cascade port tests.

Asserts the STT + TTS cascades match voice.ts:262-276 / voice.ts:443-479
verbatim, Hermes extras providers exist with the right config, char-limit
dict ports Hermes tts_tool.py:132-156, and back-compat surface preserved.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure chat/ on path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

import voice  # noqa: E402


# ─── Module-level cascade entrypoint tests ────────────────────────────────


def test_module_has_cascade_entrypoints():
    """transcribe_audio_file + synthesize + voice_capabilities are module-level."""
    assert hasattr(voice, "transcribe_audio_file")
    assert hasattr(voice, "synthesize")
    assert hasattr(voice, "voice_capabilities")
    # transcribe_audio_file accepts str or Path
    sig = inspect.signature(voice.transcribe_audio_file)
    assert "file_path" in sig.parameters
    # synthesize accepts text
    sig2 = inspect.signature(voice.synthesize)
    assert "text" in sig2.parameters
    # voice_capabilities returns dict
    assert callable(voice.voice_capabilities)


# ─── STT cascade order ─────────────────────────────────────────────────────


def test_stt_cascade_order():
    """STT cascade order matches voice.ts:262-276 + Hermes extras."""
    expected = ("groq", "faster_whisper", "whisper_cpp", "mistral", "openai")
    assert voice._STT_CASCADE_ORDER == expected


# ─── TTS cascade order ─────────────────────────────────────────────────────


def test_tts_cascade_order():
    """TTS cascade order matches voice.ts:443-479 + Hermes extras."""
    expected = (
        "elevenlabs", "gradium", "mistral", "gemini", "openai",
        "kokoro", "kittentts", "edge", "macos_say",
    )
    assert voice._TTS_CASCADE_ORDER == expected


# ─── Provider config tests ────────────────────────────────────────────────


def test_elevenlabs_config():
    """ElevenLabs defaults match voice.ts:293-297."""
    p = voice._ElevenLabsProvider(api_key="k", voice_id="v")
    assert p.model_id == "eleven_turbo_v2_5"
    assert p.stability == 0.5
    assert p.similarity_boost == 0.75


def test_gradium_config():
    """Gradium config matches voice.ts:321-348 — output_format='opus' + only_audio + x-api-key."""
    # The fixed payload is sent inside synthesize(); we verify via source
    # inspection that the URL + headers + payload match voice.ts.
    src = inspect.getsource(voice._GradiumProvider.synthesize)
    assert "eu.api.gradium.ai/api/post/speech/tts" in src
    assert '"output_format": "opus"' in src
    assert '"only_audio": True' in src
    assert "x-api-key" in src


def test_kokoro_config():
    """Kokoro defaults match voice.ts:358-396."""
    p = voice._KokoroProvider()
    assert p.base_url == "http://localhost:8880"
    assert p.voice == "af_heart"
    assert p.model == "kokoro"
    src = inspect.getsource(voice._KokoroProvider.synthesize)
    assert "/v1/audio/speech" in src
    assert '"response_format": "opus"' in src


def test_macos_say_darwin_guard():
    """macOS-say raises RuntimeError when not on Darwin."""
    p = voice._MacOsSayProvider()

    async def _run():
        with patch("voice.platform.system", return_value="Linux"):
            with pytest.raises(RuntimeError, match="Local TTS only available on macOS"):
                await p.synthesize("hello")

    asyncio.run(_run())


def test_groq_whisper_config():
    """Groq Whisper config matches voice.ts:157-223."""
    p = voice._GroqWhisperProvider(api_key="k")
    assert p.model == "whisper-large-v3"
    src = inspect.getsource(voice._GroqWhisperProvider.transcribe)
    assert "https://api.groq.com/openai/v1/audio/transcriptions" in src
    assert '"response_format": "json"' in src
    assert "Authorization" in src
    assert "Bearer" in src


def test_whisper_cpp_local():
    """whisper-cpp provider uses ffmpeg → 16kHz mono WAV → whisper-cpp --output-json."""
    src = inspect.getsource(voice._WhisperCppProvider.transcribe)
    assert "ffmpeg" in src
    assert '"-ar"' in src and '"16000"' in src
    assert '"--output-json"' in src
    assert '"--no-timestamps"' in src


# ─── Hermes extras providers ──────────────────────────────────────────────


def test_faster_whisper_lazy_import(monkeypatch):
    """faster-whisper is imported INSIDE the method body (lazy)."""
    src = inspect.getsource(voice._FasterWhisperProvider.transcribe)
    # Lazy import inside the method body, not at module top.
    assert "from faster_whisper import WhisperModel" in src


def test_faster_whisper_in_cascade(monkeypatch, tmp_path):
    """Cascade tries faster-whisper after Groq (when GROQ_API_KEY missing)."""
    # Force ImportError on faster_whisper by stubbing find_spec → None
    monkeypatch.setattr(voice, "_faster_whisper_installed", lambda: False)
    # No env vars set → cascade falls all the way through to "all failed"
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("WHISPER_MODEL_PATH", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    fake_audio = tmp_path / "x.ogg"
    fake_audio.write_bytes(b"fake")

    async def _run():
        with pytest.raises(RuntimeError, match="All STT providers failed"):
            await voice.transcribe_audio_file(str(fake_audio))

    asyncio.run(_run())


def test_kittentts_lazy_import():
    """KittenTTS imported INSIDE method body (Rule 3)."""
    src = inspect.getsource(voice._KittenTtsProvider.synthesize)
    assert "from kittentts import KittenTTS" in src


def test_kittentts_cross_platform():
    """KittenTTS provider has no Darwin guard — runs anywhere."""
    src = inspect.getsource(voice._KittenTtsProvider.synthesize)
    assert "platform.system" not in src
    assert "Darwin" not in src
    # Default voice is 'Jasper' (matches Hermes default)
    p = voice._KittenTtsProvider()
    assert p.voice == "Jasper"


def test_mistral_shared_api_key(monkeypatch):
    """Mistral STT and TTS both read MISTRAL_API_KEY (single env var)."""
    src_stt = inspect.getsource(voice._MistralVoxtralSttProvider.transcribe)
    src_tts = inspect.getsource(voice._MistralVoxtralTtsProvider.synthesize)
    assert "self.api_key" in src_stt
    assert "self.api_key" in src_tts


def test_mistral_voxtral_stt():
    """Mistral STT model = voxtral-mini-latest."""
    p = voice._MistralVoxtralSttProvider(api_key="k")
    assert p.model == "voxtral-mini-latest"


def test_mistral_voxtral_tts():
    """Mistral TTS model = voxtral-mini-tts-2603 with native Opus output."""
    p = voice._MistralVoxtralTtsProvider(api_key="k")
    assert p.model == "voxtral-mini-tts-2603"
    src = inspect.getsource(voice._MistralVoxtralTtsProvider.synthesize)
    assert '"opus"' in src or "response_format='opus'" in src


def test_gemini_tts_reuses_google_key(monkeypatch):
    """Gemini TTS provider takes api_key arg; cascade prefers GEMINI_API_KEY then GOOGLE_API_KEY."""
    p = voice._GeminiTtsProvider(api_key="k")
    assert p.model == "gemini-2.5-flash-preview-tts"
    # Cascade resolution: GEMINI_API_KEY first then GOOGLE_API_KEY
    src = inspect.getsource(voice.synthesize)
    assert 'GEMINI_API_KEY' in src
    assert 'GOOGLE_API_KEY' in src
    # The "or" pattern proves preference.
    assert 'GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")' in src
    # No google.generativeai SDK — uses httpx (R1 NM1).
    src_synth = inspect.getsource(voice._GeminiTtsProvider.synthesize)
    assert "import httpx" in src_synth
    assert "google.generativeai" not in src_synth
    assert "google_genai" not in src_synth


def test_edge_in_cascade():
    """EdgeTtsProvider wired into synthesize() cascade after KittenTTS."""
    src = inspect.getsource(voice.synthesize)
    assert "EdgeTtsProvider" in src
    # Edge appears AFTER KittenTTS in source order.
    assert src.index("KittenTTS") < src.index("EdgeTtsProvider")


def test_openai_tts_in_cascade():
    """OpenAITtsProvider wired into synthesize() cascade between Gemini and Kokoro."""
    src = inspect.getsource(voice.synthesize)
    assert "OpenAITtsProvider" in src
    # Verify cascade ORDER constant (source-text indexing is fragile because
    # docstrings mention provider names too).
    expected = ("elevenlabs", "gradium", "mistral", "gemini", "openai",
                "kokoro", "kittentts", "edge", "macos_say")
    assert voice._TTS_CASCADE_ORDER == expected
    assert "OpenAITtsProvider(api_key=openai_key)" in src
    assert "_KokoroProvider(" in src


# ─── voice_capabilities() shape + two-layer test (R1 M4) ──────────────────


def _clear_voice_env(monkeypatch):
    """Helper — unset all voice-related env vars."""
    for var in (
        "GROQ_API_KEY", "WHISPER_MODEL_PATH", "OPENAI_API_KEY",
        "MISTRAL_API_KEY", "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
        "GRADIUM_API_KEY", "GRADIUM_VOICE_ID", "KOKORO_URL",
        "GEMINI_API_KEY", "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_voice_capabilities_shape(monkeypatch):
    """voice_capabilities() returns dict with stt/tts boolean keys."""
    _clear_voice_env(monkeypatch)
    monkeypatch.setattr(voice, "_faster_whisper_installed", lambda: False)
    monkeypatch.setattr(voice, "_kittentts_installed", lambda: False)
    monkeypatch.setattr(voice, "_edge_tts_installed", lambda: False)
    monkeypatch.setattr(voice.platform, "system", lambda: "Linux")
    caps = voice.voice_capabilities()
    assert set(caps.keys()) == {"stt", "tts"}
    assert isinstance(caps["stt"], bool)
    assert isinstance(caps["tts"], bool)
    # All env vars unset + no installed packages + non-Darwin = both false
    assert caps == {"stt": False, "tts": False}


def test_voice_capabilities_upstream_parity_layer(monkeypatch):
    """Layer 1 — voice.ts:487-503 upstream-parity behavior."""
    _clear_voice_env(monkeypatch)
    monkeypatch.setattr(voice, "_faster_whisper_installed", lambda: False)
    monkeypatch.setattr(voice, "_kittentts_installed", lambda: False)
    monkeypatch.setattr(voice, "_edge_tts_installed", lambda: False)
    monkeypatch.setattr(voice.platform, "system", lambda: "Linux")

    # GROQ_API_KEY → STT true (upstream parity)
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert voice.voice_capabilities()["stt"] is True
    monkeypatch.delenv("GROQ_API_KEY")

    # WHISPER_MODEL_PATH → STT true (upstream parity)
    monkeypatch.setenv("WHISPER_MODEL_PATH", "/x")
    assert voice.voice_capabilities()["stt"] is True
    monkeypatch.delenv("WHISPER_MODEL_PATH")

    # ElevenLabs requires BOTH env vars (upstream parity)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    assert voice.voice_capabilities()["tts"] is False  # voice_id missing
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v")
    assert voice.voice_capabilities()["tts"] is True
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    monkeypatch.delenv("ELEVENLABS_VOICE_ID")

    # Gradium requires BOTH (upstream parity)
    monkeypatch.setenv("GRADIUM_API_KEY", "x")
    assert voice.voice_capabilities()["tts"] is False
    monkeypatch.setenv("GRADIUM_VOICE_ID", "v")
    assert voice.voice_capabilities()["tts"] is True
    monkeypatch.delenv("GRADIUM_API_KEY")
    monkeypatch.delenv("GRADIUM_VOICE_ID")

    # KOKORO_URL alone enables TTS
    monkeypatch.setenv("KOKORO_URL", "http://localhost:8880")
    assert voice.voice_capabilities()["tts"] is True
    monkeypatch.delenv("KOKORO_URL")

    # Darwin platform → TTS true even with no env
    monkeypatch.setattr(voice.platform, "system", lambda: "Darwin")
    assert voice.voice_capabilities()["tts"] is True


def test_voice_capabilities_homie_extension_layer(monkeypatch):
    """Layer 2 — Homie extension (OpenAI / Mistral / Google / KittenTTS / Edge)."""
    _clear_voice_env(monkeypatch)
    monkeypatch.setattr(voice, "_faster_whisper_installed", lambda: False)
    monkeypatch.setattr(voice, "_kittentts_installed", lambda: False)
    monkeypatch.setattr(voice, "_edge_tts_installed", lambda: False)
    monkeypatch.setattr(voice.platform, "system", lambda: "Linux")

    # OPENAI_API_KEY → both STT + TTS true (Homie extension)
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    caps = voice.voice_capabilities()
    assert caps["stt"] is True
    assert caps["tts"] is True
    monkeypatch.delenv("OPENAI_API_KEY")

    # MISTRAL_API_KEY → both STT + TTS true (Homie extension)
    monkeypatch.setenv("MISTRAL_API_KEY", "x")
    caps = voice.voice_capabilities()
    assert caps["stt"] is True
    assert caps["tts"] is True
    monkeypatch.delenv("MISTRAL_API_KEY")

    # GEMINI_API_KEY → TTS true (Homie extension)
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert voice.voice_capabilities()["tts"] is True
    monkeypatch.delenv("GEMINI_API_KEY")

    # GOOGLE_API_KEY → TTS true (Homie extension)
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    assert voice.voice_capabilities()["tts"] is True
    monkeypatch.delenv("GOOGLE_API_KEY")

    # KittenTTS installed → TTS true
    monkeypatch.setattr(voice, "_kittentts_installed", lambda: True)
    assert voice.voice_capabilities()["tts"] is True
    monkeypatch.setattr(voice, "_kittentts_installed", lambda: False)

    # Edge TTS installed → TTS true
    monkeypatch.setattr(voice, "_edge_tts_installed", lambda: True)
    assert voice.voice_capabilities()["tts"] is True
    monkeypatch.setattr(voice, "_edge_tts_installed", lambda: False)

    # faster-whisper installed → STT true
    monkeypatch.setattr(voice, "_faster_whisper_installed", lambda: True)
    assert voice.voice_capabilities()["stt"] is True


# ─── Per-provider char limits dict ────────────────────────────────────────


def test_per_provider_char_limits():
    """PROVIDER_MAX_TEXT_LENGTH matches Hermes tts_tool.py:132-142 BYTE-FOR-BYTE.

    R1 B2 / R4 NB1 lock: gemini=5000 (NOT 3000), kittentts=2000 (NOT 5000).
    """
    expected = {
        "edge": 5000,
        "openai": 4096,
        "xai": 15000,
        "minimax": 10000,
        "mistral": 4000,
        "gemini": 5000,       # NOT 3000
        "elevenlabs": 10000,
        "neutts": 2000,
        "kittentts": 2000,    # NOT 5000
    }
    assert voice.PROVIDER_MAX_TEXT_LENGTH == expected


def test_truncate_before_provider_call():
    """Cascade truncates text BEFORE provider.synthesize via _try_provider helper."""
    src = inspect.getsource(voice._try_provider)
    assert "resolve_max_text_length" in src
    assert "text[:cap]" in src or "text_to_send = text[:cap]" in src


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("elevenlabs", 10000),  # Hermes-known — NEVER re-mapped through Homie extension
        ("gradium", 4000),      # Homie-extension-only — returned via wrapper
    ],
)
def test_resolve_max_text_length_wrapper_parametrized(provider, expected):
    """R4 NM1 fix — public wrapper resolves Hermes-known + Homie-extension correctly."""
    assert voice.resolve_max_text_length(provider) == expected


def test_elevenlabs_model_table():
    """ELEVENLABS_MODEL_MAX_TEXT_LENGTH matches Hermes tts_tool.py:145-154."""
    expected = {
        "eleven_v3": 5000,
        "eleven_ttv_v3": 5000,
        "eleven_multilingual_v2": 10000,
        "eleven_multilingual_v1": 10000,
        "eleven_english_sts_v2": 10000,
        "eleven_english_sts_v1": 10000,
        "eleven_flash_v2": 30000,
        "eleven_flash_v2_5": 40000,
    }
    assert voice.ELEVENLABS_MODEL_MAX_TEXT_LENGTH == expected
    assert voice.FALLBACK_MAX_TEXT_LENGTH == 4000


def test_resolve_falls_through_to_fallback():
    """Unknown provider falls through to FALLBACK_MAX_TEXT_LENGTH (4000)."""
    assert voice._resolve_max_text_length("unknown_provider") == 4000
    assert voice.resolve_max_text_length("unknown_provider") == 4000
    assert voice._resolve_max_text_length(None) == 4000


def test_elevenlabs_model_aware_resolution():
    """ElevenLabs model_id determines cap (model-aware lookup)."""
    cfg = {"elevenlabs": {"model_id": "eleven_flash_v2_5"}}
    assert voice._resolve_max_text_length("elevenlabs", cfg) == 40000


def test_user_override_resolution():
    """User override via tts_config trumps everything."""
    cfg = {"openai": {"max_text_length": 99}}
    assert voice._resolve_max_text_length("openai", cfg) == 99


def test_user_override_negative_falls_through():
    """Non-positive override falls through to default (defensive)."""
    cfg = {"openai": {"max_text_length": -5}}
    assert voice._resolve_max_text_length("openai", cfg) == 4096


# ─── ffmpeg cached probe ─────────────────────────────────────────────────


def test_has_ffmpeg_cached(monkeypatch):
    """_has_ffmpeg() caches result — shutil.which probed exactly once."""
    # Reset module state
    monkeypatch.setattr(voice, "_ffmpeg_available", None)
    call_count = {"n": 0}

    def fake_which(cmd):
        call_count["n"] += 1
        return "/usr/bin/ffmpeg" if cmd == "ffmpeg" else None

    monkeypatch.setattr(voice.shutil, "which", fake_which)

    async def _run():
        for _ in range(5):
            r = await voice._has_ffmpeg()
            assert r is True

    asyncio.run(_run())
    assert call_count["n"] == 1, f"expected 1 probe across 5 calls, got {call_count['n']}"


# ─── Back-compat (R1 B6) ─────────────────────────────────────────────────


def test_back_compat_legacy_and_new_canonical():
    """BOTH legacy 3-arg transcribe(bytes,key,model) AND new transcribe_audio_file are callable."""
    # Both symbols exist
    assert callable(voice.transcribe)  # legacy
    assert callable(voice.transcribe_audio_file)  # new canonical

    # Distinct symbols (no collision)
    assert voice.transcribe is not voice.transcribe_audio_file

    # Legacy signature: (audio_bytes, api_key, model="whisper-1")
    sig_legacy = inspect.signature(voice.transcribe)
    params = list(sig_legacy.parameters.keys())
    assert params == ["audio_bytes", "api_key", "model"]
    assert sig_legacy.parameters["model"].default == "whisper-1"

    # New canonical signature: (file_path)
    sig_new = inspect.signature(voice.transcribe_audio_file)
    params_new = list(sig_new.parameters.keys())
    assert params_new == ["file_path"]

    # Legacy callable shape — patch the OpenAI provider so we don't hit network
    async def _fake_transcribe(self, audio):
        return "hi"

    with patch.object(voice.OpenAIWhisperProvider, "transcribe", _fake_transcribe):
        async def _run():
            result = await voice.transcribe(b"audio_bytes", "fake_key", "whisper-1")
            assert result == "hi"

        asyncio.run(_run())


def test_synthesize_edge_and_openai_back_compat():
    """synthesize_edge / synthesize_openai legacy helpers preserved."""
    assert callable(voice.synthesize_edge)
    assert callable(voice.synthesize_openai)
    sig_edge = inspect.signature(voice.synthesize_edge)
    assert "text" in sig_edge.parameters
    assert "voice" in sig_edge.parameters
    sig_openai = inspect.signature(voice.synthesize_openai)
    assert "text" in sig_openai.parameters
    assert "api_key" in sig_openai.parameters


def test_build_voice_provider_set_back_compat():
    """build_voice_provider_set legacy helper still callable."""
    assert callable(voice.build_voice_provider_set)
    out = voice.build_voice_provider_set(openai_api_key="", tts_engine="edge")
    assert out.stt is None
    assert isinstance(out.tts, voice.EdgeTtsProvider)
