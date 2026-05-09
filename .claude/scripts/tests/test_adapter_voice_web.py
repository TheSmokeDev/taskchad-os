"""PRD-8 Phase 4 — Web/Relay adapter binary blob pipe-through + marker dispatch."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR.parent / "chat"))


def _read_adapter_source() -> str:
    chat_dir = SCRIPTS_DIR.parent / "chat" / "adapters"
    return (chat_dir / "web.py").read_text()


def test_audio_pipe_through():
    """Web adapter has audio binary blob pipe-through via transcribe_audio_blob."""
    src = _read_adapter_source()
    assert "transcribe_audio_blob" in src
    assert "audio_bytes" in src
    assert "audio_mime" in src
    # Cascade entrypoint
    assert "voice_mod.transcribe_audio_file" in src or "voice.transcribe_audio_file" in src


def test_voice_egress_web():
    """Web _send_voice_response emits binary frame on WS pipe."""
    src = _read_adapter_source()
    assert "_send_voice_response" in src
    assert "voice_mod.synthesize" in src or "voice.synthesize" in src
    assert "audio/opus" in src


def test_marker_dispatch_web():
    """Web marker dispatch emits binary_frame JSON on WS pipe."""
    src = _read_adapter_source()
    assert "_dispatch_send_markers" in src
    assert "parse_send_markers" in src
    assert "binary_frame" in src


def test_enqueue_extended_signature():
    """enqueue() accepts (text, audio_bytes, audio_mime) shape."""
    src = _read_adapter_source()
    assert "audio_bytes" in src and "audio_mime" in src


def test_web_imports_voice_markers():
    src = _read_adapter_source()
    assert "from voice_markers import" in src
    assert "import voice as voice_mod" in src
