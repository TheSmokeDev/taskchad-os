"""PRD-8 Phase 4 — anti-pattern audit tests for voice.py.

Rule 1: no tunable config in default args.
Rule 2: _ffmpeg_available is the ONLY module-state cache.
Rule 3: lazy imports for all optional deps.
Plus regression gates for Phase 7a security/patterns.py SECRET_PREFIXES
and runtime/subprocess_env.py whitelist.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure paths
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))

import voice  # noqa: E402

VOICE_PY = Path(voice.__file__)


# ─── Rule 1 — no tunable config in default args ───────────────────────────


def test_rule1_no_tunable_default_args():
    """Functions in voice.py do not bind module-level config constants as default args.

    Allowed defaults: literals, None, simple types. Forbidden: any reference to
    a module-level constant that could be runtime-overridden.
    """
    src = VOICE_PY.read_text()
    tree = ast.parse(src)

    forbidden_names = {
        "PROVIDER_MAX_TEXT_LENGTH",
        "ELEVENLABS_MODEL_MAX_TEXT_LENGTH",
        "FALLBACK_MAX_TEXT_LENGTH",
        "_HOMIE_PROVIDER_CHAR_LIMITS_EXTENSION",
    }

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for default in (args.defaults or []) + (args.kw_defaults or []):
                if default is None:
                    continue
                # Walk the default expression for forbidden Name nodes
                for sub in ast.walk(default):
                    if isinstance(sub, ast.Name) and sub.id in forbidden_names:
                        violations.append(f"{node.name} binds {sub.id} as default")
                    elif isinstance(sub, ast.Attribute):
                        # e.g. config.X — flag if attribute is on a forbidden constant
                        if isinstance(sub.value, ast.Name) and sub.value.id in forbidden_names:
                            violations.append(f"{node.name} binds {sub.value.id}.{sub.attr} as default")

    assert not violations, "Rule 1 violations: " + "; ".join(violations)


def test_rule1_resolvers_consult_dict_at_call_time():
    """resolve_max_text_length and _resolve_max_text_length consult dict in body, not as default."""
    # Both functions take a None-equivalent default for tts_config
    sig_priv = inspect.signature(voice._resolve_max_text_length)
    sig_pub = inspect.signature(voice.resolve_max_text_length)
    assert sig_priv.parameters["tts_config"].default is None
    assert sig_pub.parameters["tts_config"].default is None
    # Body references PROVIDER_MAX_TEXT_LENGTH (proves call-time lookup)
    src = inspect.getsource(voice._resolve_max_text_length)
    assert "PROVIDER_MAX_TEXT_LENGTH" in src


# ─── Rule 2 — _ffmpeg_available is the ONLY module-state cache ─────────────


def test_rule2_no_provider_module_cache():
    """No provider class is instantiated at module level (no module-state cache)."""
    src = VOICE_PY.read_text()
    tree = ast.parse(src)

    # Provider class names that must not be instantiated at module level
    provider_classes = {
        "_GroqWhisperProvider", "_FasterWhisperProvider", "_MistralVoxtralSttProvider",
        "_WhisperCppProvider", "OpenAIWhisperProvider",
        "_ElevenLabsProvider", "_GradiumProvider", "_MistralVoxtralTtsProvider",
        "_GeminiTtsProvider", "_KokoroProvider", "_KittenTtsProvider",
        "_MacOsSayProvider", "EdgeTtsProvider", "OpenAITtsProvider",
    }

    violations: list[str] = []
    for node in tree.body:  # only module-level statements
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None:
                continue
            for sub in ast.walk(value):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    name = None
                    if isinstance(func, ast.Name):
                        name = func.id
                    elif isinstance(func, ast.Attribute):
                        name = func.attr
                    if name in provider_classes:
                        violations.append(f"module-level instantiation of {name}")

    assert not violations, "Rule 2 violations: " + "; ".join(violations)


def test_rule2_only_ffmpeg_available_module_state():
    """_ffmpeg_available is the only module-state cache (typed as bool | None)."""
    # Check that the module exposes _ffmpeg_available
    assert hasattr(voice, "_ffmpeg_available")
    # And that no other _*_cache or provider-instance cache exists at module level
    for name in dir(voice):
        if name.startswith("_") and "cache" in name.lower():
            assert name == "_ffmpeg_available" or name.startswith("__"), (
                f"Unexpected module-level cache: {name}"
            )


# ─── Rule 3 — lazy imports for optional deps ───────────────────────────────


def test_rule3_lazy_imports_in_method_bodies():
    """Optional deps imported INSIDE method body, not at module top.

    Lazy deps: faster_whisper, kittentts, edge_tts, mistralai, openai, soundfile.
    httpx is NOT lazy — hard dep used by ElevenLabs/Gradium/Kokoro/Gemini/Groq.
    """
    src = VOICE_PY.read_text()
    tree = ast.parse(src)

    # Module-level imports should NOT include optional deps
    module_top_imports: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_top_imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_top_imports.add(node.module.split(".")[0])

    forbidden_module_top = {"faster_whisper", "kittentts", "edge_tts", "mistralai", "openai", "soundfile"}
    leaked = forbidden_module_top & module_top_imports
    assert not leaked, f"Optional deps leaked to module top: {leaked}"

    # httpx is allowed as a hard dep — should NOT be at module top either
    # (used inside provider method bodies for clarity); but if it WAS at top,
    # that would still be acceptable since httpx is not optional. We don't
    # enforce its placement.


def test_rule3_lazy_imports_inside_specific_methods():
    """Each lazy dep import lives inside its provider's method body."""
    pairs = [
        (voice._FasterWhisperProvider.transcribe, "from faster_whisper import"),
        (voice._KittenTtsProvider.synthesize, "from kittentts import"),
        (voice.EdgeTtsProvider.synthesize, "import edge_tts"),
        (voice._MistralVoxtralSttProvider.transcribe, "from mistralai.client import"),
        (voice._MistralVoxtralTtsProvider.synthesize, "from mistralai.client import"),
    ]
    for fn, expected in pairs:
        src = inspect.getsource(fn)
        assert expected in src, f"{fn.__qualname__} missing lazy import: {expected}"


def test_cascade_falls_through_on_importerror(monkeypatch, tmp_path):
    """If faster_whisper import fails, cascade continues to next provider."""
    # No env vars → cascade falls all the way through
    for var in ("GROQ_API_KEY", "WHISPER_MODEL_PATH", "MISTRAL_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(voice, "_faster_whisper_installed", lambda: False)

    fake_audio = tmp_path / "x.ogg"
    fake_audio.write_bytes(b"fake")

    import asyncio

    async def _run():
        with pytest.raises(RuntimeError, match="All STT providers failed"):
            await voice.transcribe_audio_file(str(fake_audio))

    asyncio.run(_run())


# ─── Phase 7a SECRET_PREFIXES regression ─────────────────────────────────


def test_phase7a_voice_provider_prefixes_regression():
    """Phase 7a SECRET_PREFIXES still includes sk_, gsk_, gr_ for voice keys."""
    from security.patterns import SECRET_PREFIXES

    assert "sk_" in SECRET_PREFIXES, "ElevenLabs prefix missing"
    assert "gsk_" in SECRET_PREFIXES, "Groq prefix missing"
    assert "gr_" in SECRET_PREFIXES, "Gradium prefix missing"


# ─── Phase 4 subprocess_env extension (R1 B5) ────────────────────────────


def test_subprocess_env_phase4_voice_keys_preserved(tmp_path):
    """Phase 4 R1 B5 — MISTRAL_ AND GOOGLE_ added to scrub whitelist."""
    from runtime.subprocess_env import get_scrubbed_sdk_env

    parent = {
        "MISTRAL_API_KEY": "ms-x",
        "GOOGLE_API_KEY": "AIzaXXX",
        "GROQ_API_KEY": "gsk_x",
        "GEMINI_API_KEY": "gem-x",
        "ELEVENLABS_API_KEY": "sk_eleven",
        "GRADIUM_API_KEY": "gr_x",
    }
    out = get_scrubbed_sdk_env(parent_env=parent, profile_root=tmp_path)
    for key, expected in parent.items():
        assert out.get(key) == expected, f"{key} dropped by scrub (Phase 4 regression)"
